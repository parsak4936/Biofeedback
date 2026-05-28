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

    # Dedicated side-stream for the raw ECG voltage trace. The dashboard
    # subscribes best-effort: if absent (e.g. real PLUX mode without an ECG
    # publisher), the ECG chart simply stays empty.
    ECG_STREAM_NAME = "OpenSignals_ECG"
    ECG_STREAM_TYPE = "ECG"
    
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
    # SAMPLE VALIDATION (physiological sanity bounds)
    # ============================================
    # Any sample outside these bounds is considered an artifact and is replaced
    # by the most recent valid value. Bounds are deliberately wide so genuine
    # stress / exertion isn't rejected — these catch electrode disconnects,
    # ADC saturation, and obviously corrupt values, not mild abnormalities.
    EDA_MIN_uS = 0.0
    EDA_MAX_uS = 80.0     # severe sweating tops out ~50 μS
    HR_MIN_BPM = 30.0     # bradycardia floor
    HR_MAX_BPM = 220.0    # fight-or-flight ceiling for an adult
    HRV_MIN_MS = 0.0
    HRV_MAX_MS = 500.0    # extreme high-HRV / athlete resting

    # Electrode-disconnect detection. If the variance of a signal over the last
    # DISCONNECT_WINDOW_SEC seconds drops below DISCONNECT_VAR_THRESHOLD, log a
    # warning. Pinned-rail electrodes look exactly like this.
    DISCONNECT_WINDOW_SEC = 15
    DISCONNECT_VAR_THRESHOLD_EDA = 1e-6
    DISCONNECT_VAR_THRESHOLD_HR = 1e-4
    DISCONNECT_VAR_THRESHOLD_HRV = 1e-4

    # ============================================
    # SESSION END POLICY
    # ============================================
    # Operator can Ctrl+C at any time. If they don't, the LIVE phase auto-finishes
    # after this many seconds (default 5 min). Set to None to disable the cap.
    LIVE_PHASE_MAX_SEC = 300

    # ============================================
    # ECG -> HR / HRV  (same pipeline for mock + real PLUX)
    # ============================================
    # Bandpass cutoffs for QRS detection. Works at any sample rate.
    ECG_BANDPASS_LOW_HZ = 5.0
    ECG_BANDPASS_HIGH_HZ = 15.0
    # Minimum spacing between R-peaks (refractory). 300 ms = max 200 BPM.
    # Set to 300 (not 250) so we don't double-count the T-wave that follows
    # each QRS ~200-400 ms later as a second beat.
    ECG_MIN_RR_MS = 300
    # Adaptive R-peak detection. We detect on peak PROMINENCE (how far a peak
    # stands out from the local baseline), not raw height — prominence is
    # robust to baseline wander and to recordings with occasional big motion
    # artifacts. A two-pass approach estimates the typical R-peak prominence,
    # then keeps peaks at this fraction of it. Lower = more sensitive.
    ECG_PEAK_PROMINENCE_FRACTION = 0.5
    # The loose first pass uses this fraction of the filtered signal's std as a
    # provisional prominence floor, just to gather candidate peaks.
    ECG_CANDIDATE_PROMINENCE_STD_FRAC = 0.5
    # RMSSD window — math-pipeline Step 0 says ~10 s rolling.
    RMSSD_WINDOW_SEC = 10

    # ============================================
    # REAL PLUX OPENSIGNALS LSL CONFIG
    # ============================================
    # OpenSignals broadcasts RAW ADC values (not pre-derived HR/HRV). We have
    # to convert + detect peaks ourselves. These knobs tell us which LSL
    # channel holds which sensor — defaults match the most common PLUX setup
    # but override here if your OpenSignals configuration is different.
    REAL_PLUX_ECG_CHANNEL = 0   # 0-based LSL channel index for ECG
    REAL_PLUX_EDA_CHANNEL = 1   # 0-based LSL channel index for EDA
    # Streaming R-peak detection runs on a rolling ECG buffer. 5 s is plenty
    # for stable peak detection without consuming much memory.
    REAL_PLUX_ECG_BUFFER_SEC = 5

    # ============================================
    # DASHBOARD VISUAL SETTINGS
    # ============================================
    # How many of the most recent samples to retain in each chart's buffer.
    # 500 ≈ 10 s at 50 Hz. Larger = more memory + slower redraw.
    DASHBOARD_MAX_HISTORY = 500
    # Width (in samples) of the auto-scrolling view window on each chart.
    # 300 ≈ 6 s of visible history at 50 Hz.
    DASHBOARD_VIEW_WIDTH = 300

    # Y-axis bounds for the per-signal charts.
    # Before baseline locks: use the *_DEFAULT_RANGE.
    # After baseline locks:   recenter around the patient's baseline ± *_HALFRANGE.
    # This prevents pyqtgraph from auto-zooming to floating-point noise when the
    # signal is stable (the "90.497730–90.497738" effect on flat resting HR).
    EDA_PLOT_DEFAULT_RANGE = (0.0, 25.0)      # μS — covers typical resting range
    HR_PLOT_DEFAULT_RANGE = (40.0, 180.0)     # BPM — wide enough for stress excursions
    HRV_PLOT_DEFAULT_RANGE = (0.0, 200.0)     # ms — RMSSD healthy range
    EDA_PLOT_HALFRANGE = 3.0                  # μS around baseline once known
    HR_PLOT_HALFRANGE = 25.0                  # BPM
    HRV_PLOT_HALFRANGE = 30.0                 # ms

    # ECG waveform chart shows the last N raw samples. At 200 Hz, 1000 samples ≈ 5 s.
    ECG_PLOT_MAX_HISTORY = 1000

    # ============================================
    # MOCK DATA FILE PATH (when DATA_SOURCE='mock')
    # ============================================
    # MockDataSource auto-detects sampling rate AND channel order (ECG vs EDA)
    # from the OpenSignals header JSON — switch files freely, no other edits.
    MOCK_DATA_FILE = "data/14_minute_test_of_myself_2026-05-26_16-47-36.txt"  # 1000Hz, 14min, EDA=col2/ECG=col3
    # MOCK_DATA_FILE = "data/opensignals_2026-05-25_14-57-56.txt"            # 200Hz, 8.7min, ECG=col2/EDA=col3
    # MOCK_DATA_FILE = "data/fake_opensignals_2026-05-13_15-24-44.txt"       # 1000Hz, 42s, EDA=col2/ECG=col3