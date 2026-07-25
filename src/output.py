# src/output.py
"""
LSL output bridges for the operator dashboard.

Two LSL streams are published so the dashboard sees everything it needs:

  1. `Biofeedback_State` (float32, 25 fixed channels) — physiology,
     session state, thresholds, Unity command stats. Channel 24
     carries `height_m` from the Acrophobia scene only (NaN otherwise);
     kept for backward compatibility with the existing height chart /
     session_review analytics.

  2. `Biofeedback_Telemetry` (string, 1 channel) — scenario-agnostic
     per-tick JSON envelope from the VR scene. String format so any
     future scenario can add / remove fields without changing the LSL
     channel schema.

Unity does not subscribe to either stream — it receives its own
"increase / decrease / start / stop" decisions over the UDP bridge in
`unity_bridge.py`. These streams are for the operator dashboard, the
audit pipeline, and any post-hoc analysis tools.
"""

import json
from pylsl import StreamInfo, StreamOutlet, cf_string
from config import Config


class UnityBridge:
    """Per-tick broadcast on LSL stream `Biofeedback_State`."""

    # Channel layout (index -> meaning). 25 channels.
    # Channel 24 carries the balloon height telemetry that Unity streams
    # back to Python (UDP port 5006, see height_receiver.py). NaN when
    # no telemetry has arrived yet (Unity not running, or telemetry
    # disabled in the Unity .cs file).
    CHANNELS = [
        's_t',                      # 0  smoothed stress index
        'state_enum',               # 1  0=calm, 1=stressed, 2=ultra_stressed
        'dashboard_score',          # 2  0-100 operator display
        'eda',                      # 3  smoothed EDA microsiemens
        'hr',                       # 4  smoothed HR BPM
        'hrv',                      # 5  smoothed RMSSD ms
        'delta_eda',                # 6  percentage deviation from baseline
        'delta_hr',                 # 7  percentage deviation from baseline
        'delta_hrv',                # 8  percentage deviation from baseline (inverted)
        'avg_eda',                  # 9  personal baseline EDA
        'avg_hr',                   # 10 personal baseline HR
        'avg_hrv',                  # 11 personal baseline RMSSD
        'thresh_mild',              # 12 mild/calm boundary, locked after baseline
        'thresh_high',              # 13 high/ultra boundary, locked after baseline
        'baseline_status',          # 14 0 during baseline, 1 once locked
        'elapsed_baseline_sec',     # 15 seconds in the BASELINE state (frozen at lock)
        'qa_invalid_count',         # 16 NaN/Inf samples rejected (running total)
        'qa_out_of_range_count',    # 17 out-of-physiological-range samples rejected
        'qa_disconnect_warnings',   # 18 electrode-disconnect episodes flagged
        'udp_gate_open',            # 19 0 while waiting for first calm, 1 once open
        'session_state',            # 20 0=IDLE, 1=BASELINE, 2=BASELINE_DONE, 3=LIVE, 4=STOPPED
        'elapsed_live_sec',         # 21 seconds in the LIVE state (frozen at stop)
        'unity_last_command_code',  # 22 0=none, 1=increase, 2=decrease, 3=start, 4=stop
        'unity_commands_sent',      # 23 running total of UDP packets actually sent
        'height_m',                 # 24 balloon altitude from Unity (m); NaN if not streaming
    ]

    def __init__(self):
        # ---- Primary numeric stream (unchanged 25-channel layout) ----
        self.info = StreamInfo(
            name=Config.OUT_STREAM_NAME,
            type=Config.OUT_STREAM_TYPE,
            channel_count=len(self.CHANNELS),
            nominal_srate=Config.PIPELINE_RATE,
            channel_format='float32',
            source_id='python_fusion_engine'
        )
        self.outlet = StreamOutlet(self.info)
        print(f"[OUTPUT] LSL outlet '{Config.OUT_STREAM_NAME}' opened "
              f"({len(self.CHANNELS)} channels).")

        # ---- Secondary telemetry stream (scenario-agnostic JSON) ----
        # One channel per sample, string format, one JSON envelope per
        # tick. Empty string when the receiver has no fresh packet.
        self.telemetry_info = StreamInfo(
            name=Config.TELEMETRY_LSL_STREAM_NAME,
            type=Config.TELEMETRY_LSL_STREAM_TYPE,
            channel_count=1,
            nominal_srate=Config.PIPELINE_RATE,
            channel_format=cf_string,
            source_id='python_fusion_engine_telemetry'
        )
        self.telemetry_outlet = StreamOutlet(self.telemetry_info)
        print(f"[OUTPUT] LSL outlet '{Config.TELEMETRY_LSL_STREAM_NAME}' "
              f"opened (1 string channel).")

    def broadcast_state(self, s_t: float, state_label: str, dashboard_score: float,
                        eda: float, hr: float, hrv: float,
                        delta_eda: float = 0.0, delta_hr: float = 0.0, delta_hrv: float = 0.0,
                        avg_eda: float = 0.0, avg_hr: float = 0.0, avg_hrv: float = 0.0,
                        thresh_mild: float = 0.0, thresh_high: float = 0.0,
                        baseline_locked: bool = False,
                        elapsed_baseline_sec: float = 0.0,
                        qa_invalid: int = 0, qa_out_of_range: int = 0,
                        qa_disconnects: int = 0,
                        udp_gate_open: bool = False,
                        session_state: int = 0,
                        elapsed_live_sec: float = 0.0,
                        unity_last_command_code: int = 0,
                        unity_commands_sent: int = 0,
                        height_m=None):
        """Encode and push one 25-channel sample. height_m=None becomes
        NaN on the wire (LSL float32) — downstream consumers should
        treat NaN as "no Unity telemetry yet"."""
        if state_label == "calm":
            state_enum = 0.0
        elif state_label == "stressed":
            state_enum = 1.0
        else:  # ultra_stressed
            state_enum = 2.0

        vector = [
            float(s_t), state_enum, float(dashboard_score),
            float(eda), float(hr), float(hrv),
            float(delta_eda), float(delta_hr), float(delta_hrv),
            float(avg_eda), float(avg_hr), float(avg_hrv),
            float(thresh_mild), float(thresh_high),
            1.0 if baseline_locked else 0.0,
            float(elapsed_baseline_sec),
            float(qa_invalid), float(qa_out_of_range), float(qa_disconnects),
            1.0 if udp_gate_open else 0.0,
            float(int(session_state)),
            float(elapsed_live_sec),
            float(int(unity_last_command_code)),
            float(int(unity_commands_sent)),
            float('nan') if height_m is None else float(height_m),
        ]
        self.outlet.push_sample(vector)

    def broadcast_telemetry(self, scenario: str, data: dict):
        """Publish one JSON envelope on the telemetry string stream.

        Called once per tick alongside broadcast_state. When the receiver
        has no fresh packet (Unity not running yet, or telemetry stale),
        an empty string is pushed so the dashboard sees "no telemetry"
        instead of a stale value.

        The envelope shape mirrors the Unity → Python contract exactly
        (see UNITY_TELEMETRY_CONTRACT.md), so a downstream tool that
        already knows how to parse a Unity packet can consume the LSL
        stream identically.
        """
        if not scenario:
            self.telemetry_outlet.push_sample([""])
            return
        envelope = {
            Config.TELEMETRY_SCENARIO_KEY: scenario,
            Config.TELEMETRY_DATA_KEY: data or {},
        }
        try:
            payload = json.dumps(envelope,
                                  separators=(',', ':'), ensure_ascii=True)
        except (TypeError, ValueError):
            payload = ""
        self.telemetry_outlet.push_sample([payload])
