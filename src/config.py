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
    # All four paths use the same canonical NeuroKit2 chain
    # (nk.ecg_clean -> nk.ecg_peaks(correct_artifacts=True) ->
    # nk.ecg_rate / nk.hrv_time), per the VRET technical report design
    # principle: every number is computed by a validated library call.
    # The variants differ only in the windowing strategy on top of the
    # same chain:
    #   mock        - replays MOCK_DATA_FILE; per-beat HR (nk.ecg_rate
    #                 with desired_length=n, interpolated per sample) and
    #                 per-beat-rolling RMSSD over RMSSD_WINDOW_SEC.
    #   mock2       - replays MOCK_DATA_FILE; mean HR over a 30 s ECG
    #                 window and mean RMSSD over a 60 s ECG window,
    #                 recomputed every 0.5 s. Smoother / slower.
    #   real_plux   - live PLUX via OpenSignals LSL, per-beat semantics
    #                 as in 'mock'.
    #   real_plux2  - live PLUX via OpenSignals LSL, sliding-mean
    #                 semantics as in 'mock2'.
    # A prominence-based detector + sqrt(mean(diff(rr)^2)) is kept as
    # the fallback ONLY for the case where NeuroKit2 is unavailable or
    # raises on the input.
    # Switching is a one-line change here; nothing downstream needs editing.
    DATA_SOURCE = 'mock'
    
    # ============================================
    # LSL NETWORK SETTINGS
    # ============================================
    # `STREAM_NAME` is the INTERNAL derived 3-channel stream
    # (EDA µS, HR BPM, HRV ms) that the data source publishes and the
    # acquisition module subscribes to. The same stream name is used in
    # both mock and real_plux modes — flipping Config.DATA_SOURCE doesn't
    # change what acquisition.py reads.
    STREAM_NAME = "Biofeedback_Raw"
    STREAM_TYPE = "Derived"
    NUM_CHANNELS = 3

    # `PLUX_LSL_NAME` is the UPSTREAM raw-ADC stream that OpenSignals
    # publishes when its LSL integration is enabled. Only used in
    # real_plux mode. The mock path doesn't see this stream — it reads
    # the .txt file directly. Default matches OpenSignals' factory name.
    PLUX_LSL_NAME = "OpenSignals"
    
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
    
    # (MOCK_EDA_BASE / MOCK_HR_BASE / MOCK_HRV_BASE removed — these were
    #  seed sentinels the data source held while HR/HRV warmed up. The
    #  PDF-aligned data source now uses NaN as the warm-up sentinel, so
    #  no per-signal seed defaults are needed.)

    # ============================================
    # SESSION FILE OUTPUT RATES
    # ============================================
    # The LSL Biofeedback_State stream and the per-tick math run at
    # PIPELINE_RATE (50 Hz). The CSV files we write to disk don't need
    # to be that dense — physiology doesn't genuinely change faster
    # than ~1 Hz (HR is per beat, RMSSD updates every 0.5 s, EDA tonic
    # is seconds-scale, EDA phasic SCRs last 1-5 s, S_t is a 1-second
    # rolling mean). 50 Hz writes produce 30 000 rows per 10-minute
    # session for no clinical benefit. Both files decimate to the
    # rates below by skipping per-tick writes when less than 1/rate
    # wall-clock seconds have elapsed since the last write.
    #
    # samples.csv:    the clinical record. 1 Hz is plenty.
    # diagnostic.csv: the forensic raw + smoothed + status trace.
    #                 Same default; crank up if debugging a transient
    #                 detector glitch that you need finer than 1 s on.
    SAMPLES_CSV_RATE_HZ = 50
    DIAGNOSTIC_CSV_RATE_HZ = 50

    # ============================================
    # PIPELINE MATH & BASELINE PARAMETERS
    # ============================================
    # EMA smoothing — DISABLED per PDF.
    # Per vret_server.py: "EDA is intentionally NOT smoothed. It is an
    # inherently slow signal, and its real processing is the phasic
    # decomposition (compute_phasic_eda) used by the score. A display-only
    # EMA was previously applied but contributed nothing to S_t and was
    # visually indistinguishable from raw, so it has been removed. Raw EDA
    # is shown directly."
    # HR and RMSSD are likewise emitted as recompute-and-hold (ZOH between
    # 0.5 s recomputes), so an EMA on top would over-smooth them. Setting
    # alpha = 1.0 makes the EMA layer a pass-through (output = input).
    EMA_ALPHA_EDA = 1.0     # pass-through (no smoothing — PDF)
    EMA_ALPHA_HR = 1.0      # pass-through (already ZOH-held by data source)
    EMA_ALPHA_HRV = 1.0     # pass-through (already ZOH-held by data source)

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
    # 1.28 = z for 90th percentile (top 10% tail), 2.33 = z for 99th percentile
    # (top 1% tail). Per the VRET Biofeedback Pipeline technical report §2.
    # Applied as thresh = mean_baseline + K * sigma_baseline (Bug 6 fix:
    # thresholds are centred on the resting S_t mean, not on zero — resting
    # S_t is generally not zero so K*sigma against zero flags calm patients).
    THRESH_MILD_K = 1.28
    THRESH_HIGH_K = 2.33

    # ============================================
    # EDA PHASIC DECOMPOSITION (PDF Cause 1 fix)
    # ============================================
    # Raw EDA is ~99% slow tonic drift (electrode hydration, temperature) on
    # the reference recording, which dominated S_t when fed at weight 0.5.
    # Per VRET technical report §7, we decompose EDA into tonic + phasic via
    # nk.eda_phasic and feed only the PHASIC component (event-driven sympathetic
    # responses, tonic drift removed) into the score.
    EDA_PHASIC_WINDOW_SEC = 60          # rolling raw-EDA window fed to nk.eda_phasic
    EDA_DECOMP_RATE_HZ = 10             # internal resample rate for decomposition
    EDA_PHASIC_UPDATE_INTERVAL_SEC = 0.5  # recompute phasic at this cadence

    # ============================================
    # Z-SCORE FLOOR (PDF Cause 2 fix)
    # ============================================
    # When a baseline sigma collapses to ~0 the z-score divisor would blow up.
    # Clamped to this floor so a degenerate per-signal sigma doesn't make the
    # z-score Inf. Surfaces as a much smaller (but finite) z, instead of
    # poisoning the whole live phase.
    SIGMA_FLOOR = 1e-6

    # ============================================
    # PER-SIGNAL PHYSIOLOGICAL SIGMA FLOORS (vret_server.py guardrail)
    # ============================================
    # The z-score divides a live deviation by the signal's baseline spread
    # (sigma). If a short or unusually flat baseline yields an implausibly
    # tiny sigma, a trivial live change becomes a huge z-score and the
    # score false-alarms (a 1% HR wobble was reading "ultra-stressed"
    # before these floors were added). These floors refuse to believe a
    # resting signal varies less than its real physiological minimum, so
    # they only bite on broken / flukey baselines and leave normal ones
    # untouched. Tune these once more real recordings are available.
    HR_SIGMA_FLOOR_PCT = 2.0      # resting HR spread is realistically ≥ ~2%
    HRV_SIGMA_FLOOR_PCT = 5.0     # resting RMSSD deviation spread ≥ ~5%
    EDA_PHASIC_SIGMA_FLOOR = 0.02 # phasic-EDA spread floor in µS

    # Physiological ceiling for an instantaneous phasic-EDA value (µS).
    # Real phasic skin-conductance responses are tenths of a µS; even a
    # large startle SCR rarely exceeds ~1 µS. Larger magnitudes are filter
    # ringing from a resampler edge artifact in the phasic decomposition
    # (the resampler intermittently appends a spurious ~0 final sample;
    # nk's zero-phase filter then rings backward across the tail, reaching
    # 4-15 µS in rare ticks). We do NOT try to repair the decomposition
    # (heuristic repair risks misfiring on real data); we simply reject an
    # implausible reading so a known-garbage value never reaches the score.
    EDA_PHASIC_MAX_US = 1.0

    # ============================================
    # RR-INTERVAL ARTIFACT GATE (vret_server.py Malik 20% rule)
    # ============================================
    # NeuroKit's Kubios correction (correct_artifacts=True) runs first and
    # RECONSTRUCTS missed beats by interpolation — which keeps the heart-rate
    # trace smooth but leaves interpolated intervals in the series, and those
    # still inflate a time-domain RMSSD. For RMSSD specifically we therefore
    # additionally apply the Malik / Task Force (1996) rule below: reject a
    # beat whose RR change relative to the previous RR exceeds the threshold.
    # A relative rule is sounder than an absolute-ms one because the
    # tolerable beat-to-beat change scales with heart rate.
    #
    # Reference: Malik M. et al. / Task Force of ESC and NASPE (1996), Heart
    # rate variability: standards of measurement. Eur Heart J 17(3):354-381.
    # Applied identically to baseline and live RMSSD so the two are measured
    # the same way.
    RR_MIN_MS = 300                    # 200 BPM ceiling
    RR_MAX_MS = 1500                   # 40 BPM floor
    RR_MAX_RELATIVE_CHANGE = 0.20      # Malik 20% rule

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
    # ECG -> HR / HRV  (PDF-aligned canonical NeuroKit2 chain)
    # ============================================
    # Per the VRET Biofeedback Pipeline technical report §2-3, HR and RMSSD
    # are derived by:
    #     nk.ecg_clean -> nk.ecg_peaks(correct_artifacts=True)
    #                  -> nk.ecg_rate   (HR over trailing HR_WINDOW_SEC)
    #                  -> nk.hrv_time["HRV_RMSSD"]
    #                                   (RMSSD over trailing RMSSD_WINDOW_SEC)
    # Both are recomputed every HR_COMPUTE_INTERVAL_SEC and held between
    # updates (zero-order hold). The same chain runs in both mock and live
    # paths — there is no v1/v2 split anymore.

    # Trailing ECG window fed to nk.ecg_rate for HR. PDF §3: "a memory cap,
    # not a smoothing target — HR appears within a few seconds of beats
    # being detected." 30 s is plenty for a stable per-window mean.
    HR_WINDOW_SEC = 30

    # Trailing ECG window fed to nk.hrv_time for RMSSD. PDF §3 chooses 60 s
    # as the minimum where the estimator standard deviation is acceptable
    # (~12 ms; a 10 s window gives ~56 ms — too noisy). Before 60 s of ECG
    # has been buffered, RMSSD is emitted as NaN (PDF: "no valid live
    # RMSSD in the first minute").
    RMSSD_WINDOW_SEC = 60

    # How often HR and RMSSD are recomputed (per PDF §3: "HR and RMSSD are
    # recomputed every 25 ticks (about every 0.5 s)"). Between recomputes
    # the values are held with zero-order hold.
    HR_COMPUTE_INTERVAL_SEC = 0.5

    # Physiological plausibility band for RMSSD (PDF §4 Bug 3). Values
    # outside this range are detector failures (the R-peak detector is
    # missing or inventing beats on a poor-quality ECG), not real
    # measurements. They are rejected and the previous valid value is held.
    RMSSD_MIN_MS = 5
    RMSSD_MAX_MS = 300

    # Minimum R-peak count in the RMSSD window before nk.hrv_time is
    # called. Below this, RMSSD stays NaN (the window is too sparse for a
    # meaningful estimate).
    MIN_PEAKS_HRV = 10

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
    # Bluetooth-dropout watchdog. In real_plux mode the PLUX hub streams
    # over Bluetooth; if it drops mid-session, RealPLUXDataSource would
    # silently hold its last value forever (acquisition.py's deadman
    # reads from our OUTLET, not the PLUX inlet, so it wouldn't trip).
    # The watchdog prints one WARN after WARN_SEC of silence and raises
    # at STREAM_TIMEOUT_SEC, matching how the acquisition-side deadman
    # fails the session.
    REAL_PLUX_WATCHDOG_WARN_SEC = 2.0

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
    MOCK_DATA_FILE = "data/5_2026-05-29_14-52-05.txt"  # 1000Hz, 6.1min, ECG=col2/EDA=col3
  # MOCK_DATA_FILE = "data/opensignals_2026-05-25_14-57-56.txt"            # 200Hz, 8.7min, ECG=col2/EDA=col3
    # MOCK_DATA_FILE = "data/fake_opensignals_2026-05-13_15-24-44.txt"       # 1000Hz, 42s, EDA=col2/ECG=col3
    # When False (default), MockDataSource stops publishing once the file is
    # exhausted — the acquisition deadman fires after STREAM_TIMEOUT_SEC and
    # the session ends cleanly with whatever was captured. Matches what a
    # real PLUX device would do at end of recording. Set True for endless
    # dev testing on short files, but expect the HR/HRV signal to jump
    # discontinuously at every wrap-around (the file's end and beginning
    # don't connect physiologically).
    MOCK_LOOP = False
  