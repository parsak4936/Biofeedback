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
    # EMA smoothing per math-pipeline Step 1. Lower alpha = stronger smoothing.
    EMA_ALPHA_EDA = 0.05
    EMA_ALPHA_HR = 0.10
    EMA_ALPHA_HRV = 0.05

    BASELINE_SEC = 120  # math-pipeline Step 2

    # Math-pipeline Step 3: outlier rejection radius. 3.0 = Gaussian 99.7% interval.
    ARTIFACT_SIGMA_MULTIPLIER = 3.0

    # Math-pipeline Step 6: weighted fusion of percentage deviations.
    # Higher weight = stronger contribution to S_instant. Must reflect signal
    # specificity to sympathetic arousal (see walkthrough Step 6 rationale).
    WEIGHT_EDA = 0.5
    WEIGHT_HRV = 0.3
    WEIGHT_HR = 0.2

    # Math-pipeline Step 8: threshold multipliers against frozen σ_baseline.
    # 1.33 ≈ z=1.282 (~90th pct), 2.28 ≈ z=2.326 (~99th pct) under Gaussian assumption.
    THRESH_MILD_K = 1.33
    THRESH_HIGH_K = 2.28

    # ============================================
    # SESSION MODE (per math-pipeline Step 9b)
    # ============================================
    # Selects the VR balloon altitude range.
    # MODULAR: set here for now. Later, Unity will override at session start
    # via an LSL handshake (see FusionEngine.set_mode()). The launcher could
    # also prompt for this — flip CALL SITE to either keep this constant or
    # read an env var like PATIENT_NAME does.
    SESSION_MODE = 'easy'  # 'easy' | 'moderate' | 'intense'

    MODE_RANGES = {
        'easy':     {'y_low': 20.0, 'y_high': 30.0, 'y_mid': 25.0},
        'moderate': {'y_low': 30.0, 'y_high': 45.0, 'y_mid': 37.5},
        'intense':  {'y_low': 45.0, 'y_high': 65.0, 'y_mid': 55.0},
    }

    # Rate scaling constants from math-pipeline Step 9c (per second).
    # k_down = C_DOWN * (y_high - y_low),  k_up = C_UP * (y_high - y_low).
    C_DOWN = 0.010
    C_UP = 0.005

    # ============================================
    # SESSION END POLICY
    # ============================================
    # Operator can Ctrl+C at any time. If they don't, the LIVE phase auto-finishes
    # after this many seconds (default 5 min). Set to None to disable the cap.
    LIVE_PHASE_MAX_SEC = 300

    # ============================================
    # ECG -> HR / HRV (mock data; real PLUX delivers these directly)
    # ============================================
    # Bandpass cutoffs for QRS detection on a 1000 Hz ECG stream.
    ECG_BANDPASS_LOW_HZ = 5.0
    ECG_BANDPASS_HIGH_HZ = 15.0
    # Minimum spacing between R-peaks (refractory). 250 ms = max ~240 BPM.
    ECG_MIN_RR_MS = 250
    # RMSSD window — math-pipeline Step 0 says ~10 s rolling.
    RMSSD_WINDOW_SEC = 10

    # ============================================
    # DASHBOARD VISUAL SETTINGS
    # ============================================
    # How many of the most recent samples to retain in each chart's buffer.
    # 500 ≈ 10 s at 50 Hz. Larger = more memory + slower redraw.
    DASHBOARD_MAX_HISTORY = 500
    # Width (in samples) of the auto-scrolling view window on each chart.
    # 300 ≈ 6 s of visible history at 50 Hz.
    DASHBOARD_VIEW_WIDTH = 300

    # ============================================
    # MOCK DATA FILE PATH (when DATA_SOURCE='mock')
    # ============================================
    MOCK_DATA_FILE = "data/fake_opensignals_2026-05-13_15-24-44.txt"