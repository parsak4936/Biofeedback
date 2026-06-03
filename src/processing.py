# src/processing.py
"""
Signal processing: smoothing, baseline buffering, and artifact cleaning.

The pipeline's "middle layer". Takes the raw (EDA, HR, HRV) sample on each
tick, applies an exponential moving average to suppress high-frequency
noise (math-pipeline Step 1), accumulates 120 seconds of smoothed samples
during the BASELINE state (Step 2), then on completion runs a 3-sigma
filter to throw out motion-spike artifacts (Step 3) and computes the
patient's personal averages (Step 4a). The cleaned baseline arrays are
kept so the fusion layer can derive sigma_baseline from them.

State management of "are we in baseline or live" is done implicitly via
the `baseline_complete` flag. The new operator-controlled state machine
in main.py wraps this — proc.reset() is called when the operator clicks
Reset, which clears the buffers and lets the next baseline start fresh.
"""

import collections
import math
import numpy as np
import warnings
from config import Config

# NeuroKit2 is the reference library for EDA decomposition (PDF §7 Cause 1).
# Falls back to a no-op if unavailable so the rest of the pipeline keeps
# running, but the EDA z-score will then sit around 0 and the score will
# be HR/HRV-driven only.
try:
    import neurokit2 as nk
    _NEUROKIT_AVAILABLE = True
except ImportError:
    _NEUROKIT_AVAILABLE = False


class SignalProcessor:
    """
    EMA smoothing + 120 s baseline buffer + 3-sigma artifact rejection.

    Each tick goes through `process_sample(raw_vector)`, which returns the
    smoothed vector and a flag indicating whether the baseline has just
    completed. The first time that flag flips to True, the personal
    averages are stored in `self.personal_averages` and the cleaned
    per-signal arrays are in `self.cleaned_baseline_buffers` for the
    fusion layer to consume.
    """
    def __init__(self):
        # No smoothing layer. Per the PDF (vret_server.py: "EDA is
        # intentionally NOT smoothed"; HR and RMSSD are ZOH-held by the
        # data source). The previous EMA-with-alpha=1.0 pass-through was
        # removed in this commit — process_sample now forwards the input
        # vector unchanged. Baseline buffers still accumulate raw values
        # during BASELINE so personal_averages and the per-signal stats
        # can be computed at the 120 s lock.

        # Baseline Buffers
        self.buffers = {
            'eda': [],
            'hr': [],
            'hrv': []
        }

        # Gate that the state machine flips on entry to BASELINE / off otherwise.
        # Without this, _buffer_sample fires every tick while baseline_complete
        # is False — including during IDLE — so the buffer fills before the
        # operator has even clicked Start Baseline.
        self.accumulate_baseline = False
        self.baseline_complete = False
        # Phase string used in the processing audit log. main.py sets this
        # via set_phase() on every state transition; "IDLE" is the safe
        # default before the operator clicks anything.
        self.current_phase = "IDLE"
        self.personal_averages = {}
        self.artifacts_removed = {
            'eda': 0,
            'hr': 0,
            'hrv': 0
        }
        # Holds the 3-sigma-cleaned arrays so downstream code can compute
        # a true noise-floor sigma against the personal baseline.
        self.cleaned_baseline_buffers = {
            'eda': None,
            'hr': None,
            'hrv': None
        }
        self.target_buffer_size = int(Config.BASELINE_SEC * Config.PIPELINE_RATE)

        # ---- Phasic EDA infrastructure (PDF §7 Cause 1) ----
        # Rolling RAW (un-smoothed) EDA buffer fed to nk.eda_phasic so the
        # tonic SCL drift can be subtracted out and only the phasic (SCR)
        # component reaches the fusion engine.
        self._phasic_window_n = int(Config.EDA_PHASIC_WINDOW_SEC
                                    * Config.PIPELINE_RATE)
        self._raw_eda_window = collections.deque(maxlen=self._phasic_window_n)
        self._phasic_update_interval_n = max(
            1, int(Config.EDA_PHASIC_UPDATE_INTERVAL_SEC * Config.PIPELINE_RATE)
        )
        self._tick_counter = 0
        self.current_phasic_eda = 0.0
        # Per-tick phasic-EDA values captured during BASELINE so fusion can
        # compute the phasic mean + sigma used to z-score live phasic values.
        self.phasic_baseline_buffer = []
        self.cleaned_phasic_buffer = None

        # The per-tick processing_log_*.csv that used to live here was
        # merged with the per-tick acquisition_log_*.csv into a single
        # diagnostic.csv inside the session folder. main.py now writes
        # the combined row via session.log_diagnostic_row() and uses
        # session.discard_diagnostic_log() for the partial-baseline
        # discard path. SignalProcessor no longer owns a file handle.

    def set_phase(self, phase: str):
        """Set the label written to the processing audit log (IDLE / BASELINE /
        BASELINE_DONE / LIVE / STOPPED). Called by main.py on state transitions
        so the per-tick log reflects the actual session state, not a derived
        proxy."""
        self.current_phase = phase

    def reset(self):
        """
        Discard everything and start over. Used when the operator clicks
        Reset on the baseline panel — we want the next baseline_start to
        begin from a completely clean state, not pick up where we stopped.
        """
        # Recreate the baseline buffers (they get nulled out after the
        # first successful computation, so they may not be lists right now).
        self.buffers = {'eda': [], 'hr': [], 'hrv': []}
        self.accumulate_baseline = False
        self.baseline_complete = False
        self.personal_averages = {}
        self.artifacts_removed = {'eda': 0, 'hr': 0, 'hrv': 0}
        self.cleaned_baseline_buffers = {'eda': None, 'hr': None, 'hrv': None}
        self._raw_eda_window.clear()
        self.current_phasic_eda = 0.0
        self.phasic_baseline_buffer = []
        self.cleaned_phasic_buffer = None
        self._tick_counter = 0
        print("[PROCESSOR] Reset: baseline buffers cleared, EMA reseeded.")

    def process_sample(self, raw_vector: list) -> tuple:
        """
        Main entry point for each 50Hz tick.

        Per the PDF (vret_server.py): no smoothing on any of the three
        signals. EDA is published raw and decomposed into phasic on a
        rolling window; HR and RMSSD are already held by the data source
        between recomputes (zero-order hold), so an EMA on top would
        over-smooth them. The function just does the bookkeeping that
        needs to happen at the tick rate:

          1. Append the raw EDA sample to the rolling decomposition
             window; trigger nk.eda_phasic every EDA_PHASIC_UPDATE_INTERVAL_SEC.
          2. During BASELINE, accumulate raw samples into baseline buffers
             so personal averages + per-signal stats can be computed at
             the 120 s lock.
          3. Return the same vector that came in, plus the baseline-done
             flag. Caller treats the result as the "post-processing"
             signal vector even though processing is a pass-through.

        Returns:
            tuple: (signal_vector, is_baseline_complete)
        """
        raw_eda, raw_hr, raw_hrv = raw_vector

        # Pass-through. (Variable kept for the same name as the caller's
        # `smoothed_vector` so downstream code reads naturally.)
        signal_vector = [raw_eda, raw_hr, raw_hrv]

        # 1. Phasic EDA pipeline (PDF §7 Cause 1). Append RAW EDA to the
        # rolling window fed to nk.eda_phasic. Decomposition is expensive
        # so we only recompute every EDA_PHASIC_UPDATE_INTERVAL_SEC; the
        # current phasic value is held between recomputes (ZOH).
        self._raw_eda_window.append(float(raw_eda))
        self._tick_counter += 1
        if (self._tick_counter % self._phasic_update_interval_n == 0
                and len(self._raw_eda_window) >= int(Config.PIPELINE_RATE) * 5):
            self._update_phasic_eda()

        # 2. Handle Baseline Phase. accumulate_baseline is True only while
        # the state machine is in BASELINE; baseline_complete short-circuits
        # so the 120 s lock fires exactly once.
        if self.accumulate_baseline and not self.baseline_complete:
            self._buffer_sample(signal_vector)
            # Capture the current phasic value per-tick so fusion can derive
            # phasic mean + sigma at lock. Held value is fine between the
            # ~25 ticks separating decompositions.
            self.phasic_baseline_buffer.append(self.current_phasic_eda)

        return signal_vector, self.baseline_complete

    def _update_phasic_eda(self):
        """
        Decompose the rolling raw EDA window into tonic + phasic and pick the
        most recent phasic value. Updated at EDA_PHASIC_UPDATE_INTERVAL_SEC
        cadence; held between updates.

        Mirrors compute_phasic_eda() in vret_server.py:
          1. Resample to EDA_DECOMP_RATE_HZ (10 Hz; plenty for SCRs).
          2. nk.eda_clean.
          3. nk.eda_phasic; take the LAST EDA_Phasic sample.
          4. Plausibility ceiling: real phasic SCRs are tenths of a µS;
             |phasic| > EDA_PHASIC_MAX_US is filter ringing from a
             resampler edge artifact, not arousal. Reject (set to 0.0)
             so the garbage never reaches the score. This caps the blast
             radius of a rare (~0.5% of ticks) decomposition glitch.

        Returns silently on Exception (the last good value is held — better
        than synthesising a zero on a transient failure).
        """
        if not _NEUROKIT_AVAILABLE:
            return
        try:
            arr = np.asarray(self._raw_eda_window, dtype=float)
            src_rate = Config.PIPELINE_RATE
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                ds = nk.signal_resample(
                    arr, sampling_rate=src_rate,
                    desired_sampling_rate=Config.EDA_DECOMP_RATE_HZ,
                )
                ds = nk.eda_clean(ds, sampling_rate=Config.EDA_DECOMP_RATE_HZ)
                phasic = nk.eda_phasic(
                    ds, sampling_rate=Config.EDA_DECOMP_RATE_HZ,
                )["EDA_Phasic"].values
            if phasic.size == 0:
                return
            val = float(phasic[-1])
            # Plausibility ceiling (vret_server.py): reject filter-ringing
            # artifacts from the resampler edge. Real SCRs stay well below 1 µS.
            if not math.isfinite(val) or abs(val) > Config.EDA_PHASIC_MAX_US:
                # Don't update — hold the last good value (or 0.0 initial).
                # Optionally surface it for visibility; throttle to avoid
                # spamming during a sustained artifact window.
                self._phasic_reject_count = getattr(self, '_phasic_reject_count', 0) + 1
                if self._phasic_reject_count == 1 or self._phasic_reject_count % 20 == 0:
                    print(f"[PROCESSOR] EDA-CLAMP: implausible phasic "
                          f"{val:+.2f} µS rejected "
                          f"(> {Config.EDA_PHASIC_MAX_US} µS = decomposition "
                          f"artifact, not a real SCR). Count: "
                          f"{self._phasic_reject_count}.")
                return
            self.current_phasic_eda = val
        except Exception:
            # Decomposition failed on this window — quietly hold the last
            # good value rather than poisoning S_t with a synthetic zero.
            pass

    def _buffer_sample(self, smoothed_vector: list):
        """Appends to the baseline array and triggers computation when full."""
        self.buffers['eda'].append(smoothed_vector[0])
        self.buffers['hr'].append(smoothed_vector[1])
        self.buffers['hrv'].append(smoothed_vector[2])
        
        # Check if the buffer has reached exactly 120 seconds (6000 samples)
        if len(self.buffers['eda']) == self.target_buffer_size:
            self._compute_personal_baselines()

    def finalize_baseline_now(self) -> bool:
        """
        Force the 3-sigma cleaning + averages to run on whatever samples
        are currently in the buffer. Returns True if it actually ran.

        Called by main.py when wall-clock baseline time hits 120 s but the
        per-tick loop ran below nominal 50 Hz (Windows time.sleep slop,
        per-tick CSV file I/O, etc.) so the buffer hasn't quite reached
        the 6000-sample target. Without this, the duration label hits
        02:00 but the auto-lock never fires.

        Idempotent: a second call after the buffer has already been
        finalized is a no-op. Refuses to run if there's almost nothing in
        the buffer, in which case the math would just be noise.
        """
        if self.baseline_complete:
            return False
        if not self.buffers or len(self.buffers.get('eda') or []) < int(Config.PIPELINE_RATE * 5):
            # Less than 5 s of data — refuse to compute, the personal
            # averages would be meaningless.
            return False
        have = len(self.buffers['eda'])
        target = self.target_buffer_size
        print(f"\n[PROCESSOR] Wall-clock 120 s reached with {have}/{target} samples "
              f"({100.0 * have / target:.1f}%). Finalizing baseline on what we have.")
        self._compute_personal_baselines()
        return True

    def _compute_personal_baselines(self):
        """
        Executes the 3-sigma artifact removal and computes resting averages.
        This runs exactly once at T = 120s.
        """
        print(f"\n[PROCESSOR] 120-Second Buffer Full. Executing 3-Sigma Cleaning...")
        
        for signal in ['eda', 'hr', 'hrv']:
            arr_full = np.array(self.buffers[signal], dtype=np.float64)

            # NaN warm-up exclusion (PDF §3): for HR and HRV, the data source
            # emits NaN until the R-peak detector / RMSSD window has filled.
            # We drop NaN here so personal_averages reflect only real
            # measurements. EDA never has NaN (it's a direct hardware signal).
            n_nan = int(np.sum(~np.isfinite(arr_full)))
            arr = arr_full[np.isfinite(arr_full)]
            if n_nan > 0:
                if len(arr) >= int(Config.PIPELINE_RATE * 5):  # ≥5 s of real samples
                    print(f"[PROCESSOR] {signal.upper()}: dropped {n_nan} "
                          f"warm-up samples (NaN); averaging {len(arr)} real "
                          f"samples ({len(arr)/Config.PIPELINE_RATE:.1f} s).")
                else:
                    print(f"[PROCESSOR] WARN: {signal.upper()} baseline has "
                          f"{n_nan} NaN warm-up samples and only {len(arr)} "
                          f"real samples — baseline ran too short. For HRV, "
                          f"you need ≥60 s of baseline after the 60 s RMSSD "
                          f"warm-up (so ≥120 s total). Increase BASELINE_SEC "
                          f"or re-run with the participant settled earlier.")
                    if len(arr) == 0:
                        # Pure-NaN buffer: avoid empty-array NaN math by
                        # falling back to a sentinel. The detector-failure
                        # check in main.py will surface this loudly.
                        arr = np.array([float('nan')])

            # Calculate raw mean and standard deviation
            mu = float(np.nanmean(arr)) if len(arr) > 0 else float('nan')
            sigma = float(np.nanstd(arr)) if len(arr) > 0 else float('nan')

            # Guard: if the signal is perfectly flat (σ=0, e.g. electrode pinned),
            # the 3σ window collapses to a single point and nothing passes the
            # filter. Fall back to the uncleaned buffer with a logged warning so
            # downstream math doesn't blow up. This is a defensive path; the
            # disconnect detector in acquisition.py should have caught this earlier.
            if sigma == 0.0:
                print(f"[PROCESSOR] WARN: {signal.upper()} baseline sigma=0 "
                      f"(signal flat at {mu:.3f}). Skipping 3-sigma filter "
                      f"for this signal.")
                clean_arr = arr.copy()
            else:
                # Outlier filter: math-pipeline Step 3. Multiplier in Config.
                k = Config.ARTIFACT_SIGMA_MULTIPLIER
                lower_bound = mu - (k * sigma)
                upper_bound = mu + (k * sigma)
                clean_arr = arr[(arr >= lower_bound) & (arr <= upper_bound)]

                # Guard: if the filter rejected every sample (numerical edge
                # case), fall back to the raw buffer rather than producing NaN.
                if len(clean_arr) == 0:
                    print(f"[PROCESSOR] WARN: {signal.upper()} 3σ filter "
                          f"rejected all samples; reverting to raw buffer.")
                    clean_arr = arr.copy()

            # Persist the cleaned arrays so fusion can compute a true sigma floor
            self.cleaned_baseline_buffers[signal] = clean_arr

            # Calculate final personal average from the cleaned data
            self.personal_averages[signal] = float(np.mean(clean_arr))

            # Track artifacts removed
            artifacts_count = len(arr) - len(clean_arr)
            self.artifacts_removed[signal] = artifacts_count
            
            # Diagnostic reporting
            print(f"  -> {signal.upper()}: Removed {artifacts_count} artifacts. Baseline Avg = {self.personal_averages[signal]:.2f}")
            
        # Phasic baseline buffer: cleaned the same way as the others so the
        # per-signal stats fusion derives from it aren't contaminated by
        # decomposition artifacts at window edges. If the buffer is empty
        # (e.g. NeuroKit unavailable for the whole baseline) we leave it
        # None — fusion will warn and treat the EDA term as ~0.
        if self.phasic_baseline_buffer:
            ph_arr = np.asarray(self.phasic_baseline_buffer, dtype=np.float64)
            ph_mu = float(np.mean(ph_arr))
            ph_sigma = float(np.std(ph_arr))
            if ph_sigma == 0.0:
                self.cleaned_phasic_buffer = ph_arr.copy()
            else:
                k = Config.ARTIFACT_SIGMA_MULTIPLIER
                lo, hi = ph_mu - k * ph_sigma, ph_mu + k * ph_sigma
                ph_clean = ph_arr[(ph_arr >= lo) & (ph_arr <= hi)]
                self.cleaned_phasic_buffer = (ph_clean if len(ph_clean) > 0
                                              else ph_arr.copy())
            print(f"  -> EDA PHASIC: kept {len(self.cleaned_phasic_buffer)}"
                  f"/{len(ph_arr)} samples; "
                  f"mean={float(np.mean(self.cleaned_phasic_buffer)):+.4f} uS")

        self.baseline_complete = True

        # Free up memory (we don't need the 18,000 floats anymore)
        self.buffers = None
        print("[PROCESSOR] Calibration Complete. Switching to Live Therapy Mode.\n")

if __name__ == "__main__":
    # Standalone Test: Feed 6000 synthetic noisy arrays to watch the 3-Sigma cleaning work
    import random
    
    print("[TEST] Initializing SignalProcessor...")
    processor = SignalProcessor()
    
    for i in range(processor.target_buffer_size):
        # Generate fake data: Base + Noise. Occasionally inject a massive artifact to test 3-Sigma
        fake_eda = 5.0 + random.uniform(-0.1, 0.1)
        fake_hr = 75.0 + random.uniform(-2.0, 2.0)
        fake_hrv = 40.0 + random.uniform(-1.0, 1.0)
        
        # Inject an artifact at tick 3000
        if i == 3000:
            fake_eda += 50.0  # Massive spike
            
        vector = [fake_eda, fake_hr, fake_hrv]
        smooth_vec, is_ready = processor.process_sample(vector)
        
        if is_ready:
            break