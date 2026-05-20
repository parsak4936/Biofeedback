# src/config.py

class Config:
    # LSL Network Settings
    STREAM_NAME = "OpenSignals"
    STREAM_TYPE = "Biosignals"
    NUM_CHANNELS = 3
    # System Limits
    STREAM_TIMEOUT_SEC = 5.0  # Maximum seconds of silence before declaring stream dead
    # Frequencies (in Hz)
    PIPELINE_RATE = 50.0  # The core loop speed for EDA and the fusion engine
    HR_RATE = 1.0         # Heart rate updates roughly once per second
    HRV_RATE = 0.1        # RMSSD updates roughly every 10 seconds
# Unity LSL Output Settings
    OUT_STREAM_NAME = "Biofeedback_State"
    OUT_STREAM_TYPE = "Control"
    # Mock Data Baselines (Synthetic Generation)
    MOCK_EDA_BASE = 5.0   # microsiemens
    MOCK_HR_BASE = 75.0   # BPM
    MOCK_HRV_BASE = 40.0  # ms
# Pipeline Math & Baseline Parameters
    EMA_ALPHA_EDA = 0.05
    EMA_ALPHA_HR = 0.10
    EMA_ALPHA_HRV = 0.05
    
    BASELINE_SEC = 120  # 2 minutes of silent buffering
