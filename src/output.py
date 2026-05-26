# src/output.py

from pylsl import StreamInfo, StreamOutlet
from config import Config

class UnityBridge:
    """
    Creates an LSL Outlet to broadcast the final therapeutic vector to the Unity VR engine
    and the clinical dashboard.
    """
    # Channel layout (index → meaning). Matches math-pipeline "Step ✓" output list
    # plus three diagnostic counters for the operator dashboard's QA panel.
    CHANNELS = [
        's_t', 'state_enum', 'dashboard_score', 'y_t',
        'eda', 'hr', 'hrv',
        'avg_eda', 'avg_hr', 'avg_hrv',
        'thresh_mild', 'thresh_high',
        'baseline_status',          # 0.0 during baseline, 1.0 once locked
        'elapsed_baseline_sec',     # seconds since session start
        'mode_enum',                # 0=easy, 1=moderate, 2=intense
        'qa_invalid_count',         # NaN/Inf samples rejected
        'qa_out_of_range_count',    # samples outside physiological bounds
        'qa_disconnect_warnings',   # electrode-disconnect episodes flagged
    ]
    # Mode → enum mapping for the mode_enum channel.
    MODE_ENUM = {'easy': 0.0, 'moderate': 1.0, 'intense': 2.0}

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
        print(f"[OUTPUT] Unity Bridge Initialized. Broadcasting '{Config.OUT_STREAM_NAME}' "
              f"({len(self.CHANNELS)} channels).")

    def broadcast_state(self, s_t: float, state_label: str, dashboard_score: float,
                        y_t: float, eda: float, hr: float, hrv: float,
                        avg_eda: float = 0.0, avg_hr: float = 0.0, avg_hrv: float = 0.0,
                        thresh_mild: float = 0.0, thresh_high: float = 0.0,
                        baseline_locked: bool = False,
                        elapsed_baseline_sec: float = 0.0,
                        mode: str = 'easy',
                        qa_invalid: int = 0, qa_out_of_range: int = 0,
                        qa_disconnects: int = 0):
        """
        Encodes the state label into a float and pushes the full vector to the network.
        Vector layout matches CHANNELS class attribute.
        """
        if state_label == "calm":
            state_enum = 0.0
        elif state_label == "stressed":
            state_enum = 1.0
        else:  # ultra_stressed
            state_enum = 2.0

        vector = [
            float(s_t), state_enum, float(dashboard_score), float(y_t),
            float(eda), float(hr), float(hrv),
            float(avg_eda), float(avg_hr), float(avg_hrv),
            float(thresh_mild), float(thresh_high),
            1.0 if baseline_locked else 0.0,
            float(elapsed_baseline_sec),
            self.MODE_ENUM.get(mode, 0.0),
            float(qa_invalid), float(qa_out_of_range), float(qa_disconnects),
        ]
        self.outlet.push_sample(vector)