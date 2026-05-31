# src/config.py
"""
Central configuration.

Every tunable parameter in the system lives here. The design intent is that
behavior changes should almost always be a `Config` edit, not a code edit.
The values are grouped by concern (data source, pipeline rates, smoothing,
fusion math, thresholds, physiological bounds, dashboard rendering, UDP
bridge, R-peak detection, etc.). Sections are headed by comment banners so
you can scan to the area you want.

For an overview of which constants are config-only versus which need a
code touch, see CODE_AUDIT.md.
"""


class Config:
    # ============================================
    # DATA SOURCE SELECTION (CRITICAL FOR MODULARITY)
    # ============================================
    # Options: 'mock' | 'mock2' | 'real_plux' | 'real_plux2'
    #   mock        - replays MOCK_DATA_FILE; HR/HRV via in-house adaptive
    #                 R-peak detector + per-beat 60/RR (see derive_hr_hrv_from_ecg).
    #   mock2       - replays MOCK_DATA_FILE; HR/HRV via the colleague's
    #                 vret_server_v2 chain (nk.ecg_clean -> ecg_peaks ->
    #                 ecg_rate / hrv_time) with 30 s HR window and 60 s
    #                 RMSSD window. Use for A/B-testing the two derivation
    #                 methods against the same recording.
    #   real_plux   - live PLUX over OpenSignals LSL, adaptive detector.
    #   real_plux2  - live PLUX over OpenSignals LSL, NeuroKit2 chain.
    # Switching is a one-line change here; nothing downstream needs editing.
    DATA_SOURCE = 'mock'
    
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
    # The mock streamer waits up to this long for the acquisition consumer to
    # subscribe before it begins pushing samples. This is the reproducibility
    # fix — without it the streamer races ahead while subprocesses spawn and
    # the same input produces different baselines run to run.
    STREAMER_CONSUMER_WAIT_SEC = 10.0
    
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
    # The session runs until the operator presses Ctrl+C or stops it from the
    # dashboard. There is no automatic time cap: the duration of a session is
    # a clinical decision, not something to be hardcoded.

    # Upper bound for the "Session number" field in the patient intake form.
    # Multi-session protocols (e.g. 5 weekly visits) should raise this.
    MAX_SESSION_NUMBER = 3

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
    # R-peak detection uses NeuroKit2 as primary (team-preferred reference
    # library). Method options: 'neurokit' (default, robust), 'pantompkins1985'
    # (classic gold standard), 'hamilton2002', 'christov2004', 'elgendi2010'.
    # If you change this, re-verify on your recordings — they don't all agree.
    NEUROKIT_RPEAK_METHOD = 'neurokit'
    # Fallback detector parameters, used only if NeuroKit2 fails on the input.
    # The fallback is a prominence-based two-pass algorithm; lower fractions
    # are more sensitive but more vulnerable to noise.
    ECG_PEAK_PROMINENCE_FRACTION = 0.5
    ECG_CANDIDATE_PROMINENCE_STD_FRAC = 0.5
    # RMSSD window — math-pipeline Step 0 says ~10 s rolling.
    RMSSD_WINDOW_SEC = 10

    # ============================================
    # DATA SOURCE 2 (NeuroKit2 sliding-window variant)
    # ============================================
    # Knobs for MockDataSource2 / RealPLUXDataSource2 only. The original
    # detectors above are untouched, so flipping DATA_SOURCE between
    # 'mock' and 'mock2' gives a clean A/B comparison on the same file.
    DS2_HR_WINDOW_SEC = 30          # trailing ECG fed to nk.ecg_rate (matches colleague)
    DS2_HRV_WINDOW_SEC = 60         # trailing ECG fed to nk.hrv_time   (matches colleague)
    DS2_UPDATE_INTERVAL_SEC = 0.5   # how often to recompute (HR_COMPUTE_INTERVAL=25 @ 50 Hz)
    DS2_MIN_PEAKS_HRV = 10          # require this many R-peaks before RMSSD is reported
    DS2_RMSSD_MIN_MS = 5            # reject RMSSD below this as a detector failure
    DS2_RMSSD_MAX_MS = 300          # reject RMSSD above this as a detector failure
    DS2_PLUX_ECG_BUFFER_SEC = 65    # live PLUX buffer must exceed DS2_HRV_WINDOW_SEC

    # ============================================
    # UNITY UDP BRIDGE
    # ============================================
    # Plain-text commands ("start", "stop", "increase", "decrease") are sent
    # to Unity's BioFeedbackMiddleware (see AcrophobiaBalloonFlightController.cs)
    # over UDP. Default targets localhost; set the host to Unity's IP if the
    # VR rig is on a separate machine. The port matches Unity's default
    # listenPort in BioFeedbackMiddleware.cs (5005).
    UNITY_UDP_HOST = '127.0.0.1'
    UNITY_UDP_PORT = 5005
    # Minimum gap between two consecutive state-driven commands. The Unity
    # middleware steps the balloon by stepAmount (default 1 m) per packet,
    # so flooding would move the balloon impossibly fast. One per second
    # gives 1 m/s under sustained state, which the visual lerp on the Unity
    # side then smooths. Tune lower for snappier reaction, higher for gentler.
    UNITY_COMMAND_INTERVAL_SEC = 1.0
    # Wait-for-calm gate: after "start" is sent, the bridge holds all
    # state-driven commands until the patient hits the calm state. This
    # window is the period we wait *before* checking for calm, so the
    # synthetic "calm" the fusion engine returns during its 1-second buffer
    # warmup doesn't trigger a false open. Default 1.5 s gives the buffer
    # plenty of time to fill with real samples.
    UDP_GATE_WARMUP_SEC = 1.5

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