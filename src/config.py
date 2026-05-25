# src/config.py

class Config:
    # ============================================
    # DATA SOURCE SELECTION (CRITICAL FOR MODULARITY)
    # ============================================
    # Options: 'mock' | 'real_plux'
    # When real PLUX device is connected, simply change to 'real_plux'
    # All other code remains unchanged
    DATA_SOURCE = 'mock'  # Change to 'mock' to use synthetic data
    
    # ============================================
    # LSL NETWORK SETTINGS
    # ============================================
    STREAM_NAME = "OpenSignals"
    STREAM_TYPE = "00:07:80:0F:31:9C"  # Hardware MAC or identifier
    NUM_CHANNELS = 3
    
    # ============================================
    # SYSTEM LIMITS
    # ============================================
    STREAM_TIMEOUT_SEC = 5.0  # Maximum seconds of silence before declaring stream dead
    
    # ============================================
    # FREQUENCIES (Hz)
    # ============================================
    PIPELINE_RATE = 50.0  # Core loop speed for processing
    HR_RATE = 1.0         # Heart rate updates roughly once per second
    HRV_RATE = 0.1        # RMSSD updates roughly every 10 seconds
    
    # ============================================
    # UNITY LSL OUTPUT SETTINGS
    # ============================================
    OUT_STREAM_NAME = "Biofeedback_State"
    OUT_STREAM_TYPE = "Control"
    
    # ============================================
    # MOCK DATA BASELINES (Synthetic Generation)
    # ============================================
    MOCK_EDA_BASE = 5.0   # microsiemens
    MOCK_HR_BASE = 75.0   # BPM
    MOCK_HRV_BASE = 40.0  # ms
    
    # ============================================
    # PIPELINE MATH & BASELINE PARAMETERS
    # ============================================
    EMA_ALPHA_EDA = 0.05
    EMA_ALPHA_HR = 0.10
    EMA_ALPHA_HRV = 0.05
    BASELINE_SEC = 120  # 2 minutes of silent buffering
    
    # ============================================
    # MOCK DATA FILE PATH (when DATA_SOURCE='mock')
    # ============================================
    MOCK_DATA_FILE = "data/fake_opensignals_2026-05-13_15-24-44.txt"