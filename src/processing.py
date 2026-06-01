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
import csv
import datetime
import os

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
        # EMA State (holds previous smoothed values: y_{t-1})
        self.ema_state = {
            'eda': None,
            'hr': None,
            'hrv': None
        }

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

        self.log_path = None
        self.log_file = None
        self.csv_writer = None
        self._open_log()

    def _open_log(self):
        """(Re-)open the processing audit log with a fresh timestamp."""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        data_dir = os.path.join(project_root, 'data')
        session_time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f'processing_log_{session_time}.csv'
        os.makedirs(data_dir, exist_ok=True)

        self.log_path = os.path.join(data_dir, filename)
        self.log_file = open(self.log_path, mode='w', newline='')
        self.csv_writer = csv.writer(self.log_file)
        self.csv_writer.writerow(['timestamp', 'phase', 'smooth_eda', 'smooth_hr', 'smooth_hrv'])

    def discard_log(self, final: bool = False):
        """Close and delete the current processing log.

        Default (final=False): immediately open a fresh log so the next
        baseline attempt has somewhere to write — used on Stop-during-
        baseline so the next Start finds a clean slate.

        final=True: do NOT re-open. Used by the shutdown path; otherwise
        we'd recreate the very file the operator just asked to discard."""
        path = self.log_path
        try:
            if self.log_file is not None:
                self.log_file.flush()
                self.log_file.close()
                self.log_file = None
        except Exception:
            pass
        try:
            if path and os.path.exists(path):
                os.remove(path)
                print(f"[PROCESSOR] Discarded partial log: {os.path.basename(path)}")
        except OSError as e:
            print(f"[PROCESSOR] WARN: could not delete log {path}: {e}")
        if not final:
            self._open_log()

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
        self.ema_state = {'eda': None, 'hr': None, 'hrv': None}
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
        1. Smooths the raw vector.
        2. Maintains the rolling RAW EDA window and recomputes phasic EDA
           periodically.
        3. Routes to baseline buffers if baseline is incomplete.

        Returns:
            tuple: (smoothed_vector, is_baseline_complete)
        """
        raw_eda, raw_hr, raw_hrv = raw_vector

        # 1. Apply EMA Smoothing
        smooth_eda = self._apply_ema('eda', raw_eda, Config.EMA_ALPHA_EDA)
        smooth_hr = self._apply_ema('hr', raw_hr, Config.EMA_ALPHA_HR)
        smooth_hrv = self._apply_ema('hrv', raw_hrv, Config.EMA_ALPHA_HRV)

        smoothed_vector = [smooth_eda, smooth_hr, smooth_hrv]

        # 2. Phasic EDA pipeline (PDF §7 Cause 1). We append the RAW
        # (un-smoothed) EDA value to the rolling window — nk.eda_phasic
        # wants its own EDA, not our EMA-smoothed display version. The
        # decomposition is expensive enough that we only recompute every
        # EDA_PHASIC_UPDATE_INTERVAL_SEC seconds; the phasic value is
        # held between recomputes (ZOH).
        self._raw_eda_window.append(float(raw_eda))
        self._tick_counter += 1
        if (self._tick_counter % self._phasic_update_interval_n == 0
                and len(self._raw_eda_window) >= int(Config.PIPELINE_RATE) * 5):
            self._update_phasic_eda()

        # 3. Handle Baseline Phase
        # accumulate_baseline is controlled by the state machine in main.py;
        # it's True only while state == BASELINE. baseline_complete still
        # short-circuits so the 120 s lock fires once and only once.
        if self.accumulate_baseline and not self.baseline_complete:
            self._buffer_sample(smoothed_vector)
            # Capture the current phasic value per-tick so fusion can compute
            # the phasic mean + sigma at baseline lock. Held value is fine
            # for the ~25 ticks between decompositions; the values are then
            # averaged by fusion anyway.
            self.phasic_baseline_buffer.append(self.current_phasic_eda)

        # Write to audit log every tick regardless of state, with the actual
        # session phase set by main.py via set_phase(). The pre-fix label was
        # derived from baseline_complete and showed "BASELINE" during IDLE +
        # "LIVE" during BASELINE_DONE/STOPPED, which confused post-hoc review.
        current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        self.csv_writer.writerow([
            current_time, self.current_phase,
            round(smooth_eda, 4), round(smooth_hr, 4), round(smooth_hrv, 4)
        ])
        return smoothed_vector, self.baseline_complete

    def _update_phasic_eda(self):
        """
        Decompose the rolling raw EDA window into tonic + phasic and pick the
        most recent phasic value. Updated at EDA_PHASIC_UPDATE_INTERVAL_SEC
        cadence; held between updates.

        Mirrors compute_phasic_eda() in vret_server_v2.py: resample to
        EDA_DECOMP_RATE_HZ, clean, run nk.eda_phasic, take the last
        EDA_Phasic sample. Returns silently on failure (the last good value
        is held).
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
            if phasic.size > 0:
                self.current_phasic_eda = float(phasic[-1])
        except Exception:
            # Decomposition failed on this window — quietly hold the last
            # good value rather than poisoning S_t with a synthetic zero.
            pass

    def _apply_ema(self, signal_name: str, current_value: float, alpha: float) -> float:
        """
        Applies the one-pole exponential moving average.
        y_t = α · x_t + (1 − α) · y_{t−1}

        NaN handling (PDF §3 HR/HRV warm-up): the data source emits NaN for
        HR until the R-peak detector has seen a few beats (~2 s) and for
        HRV until the 60 s RMSSD window has filled. We handle NaN by:
          * NaN input + EMA never initialised  →  return NaN (still warming)
          * NaN input + EMA already initialised →  hold previous value (do
                                                   not update state — this
                                                   prevents a transient
                                                   detector failure from
                                                   contaminating the EMA)
          * Real input + EMA never initialised  →  initialise to this value
                                                   (avoids the ramp-from-
                                                   zero artifact)
          * Real input + EMA already initialised →  standard EMA blend
        """
        if isinstance(current_value, float) and math.isnan(current_value):
            previous_value = self.ema_state[signal_name]
            return previous_value if previous_value is not None else float('nan')

        previous_value = self.ema_state[signal_name]
        if previous_value is None:
            # Initialise filter with the first valid sample so the smoothed
            # trace doesn't ramp up from zero.
            self.ema_state[signal_name] = current_value
            return current_value

        smoothed_value = (alpha * current_value) + ((1.0 - alpha) * previous_value)
        self.ema_state[signal_name] = smoothed_value
        return smoothed_value

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