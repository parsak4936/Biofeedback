# src/output.py
"""
LSL output bridge for the operator dashboard.

Publishes one stream named `Biofeedback_State` at 50 Hz. The dashboard
subscribes and draws from it. Unity does not need this stream: it gets
its decisions over the separate UDP bridge in `unity_bridge.py`. The
LSL stream is for the dashboard, the audit pipeline, and any future
analysis tools.

The channel layout is fixed and lives below as a class attribute. Any
consumer can rely on it by index. 24 channels total.
"""

from pylsl import StreamInfo, StreamOutlet
from config import Config


class UnityBridge:
    """Per-tick broadcast on LSL stream `Biofeedback_State`."""

    # Channel layout (index -> meaning). 24 channels.
    # No balloon altitude, no mode (Unity handles both on its side).
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
    ]

    def __init__(self):
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
                        unity_commands_sent: int = 0):
        """Encode and push one 24-channel sample."""
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
        ]
        self.outlet.push_sample(vector)
