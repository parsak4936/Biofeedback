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

import numpy as np
from config import Config
import csv
import datetime
import os


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
        print("[PROCESSOR] Reset: baseline buffers cleared, EMA reseeded.")

    def process_sample(self, raw_vector: list) -> tuple:
        """
        Main entry point for each 50Hz tick.
        1. Smooths the raw vector.
        2. Routes to buffer if baseline is incomplete.
        
        Returns:
            tuple: (smoothed_vector, is_baseline_complete)
        """
        raw_eda, raw_hr, raw_hrv = raw_vector
        
        # 1. Apply EMA Smoothing
        smooth_eda = self._apply_ema('eda', raw_eda, Config.EMA_ALPHA_EDA)
        smooth_hr = self._apply_ema('hr', raw_hr, Config.EMA_ALPHA_HR)
        smooth_hrv = self._apply_ema('hrv', raw_hrv, Config.EMA_ALPHA_HRV)
        
        smoothed_vector = [smooth_eda, smooth_hr, smooth_hrv]

        # 2. Handle Baseline Phase
        # accumulate_baseline is controlled by the state machine in main.py;
        # it's True only while state == BASELINE. baseline_complete still
        # short-circuits so the 120 s lock fires once and only once.
        if self.accumulate_baseline and not self.baseline_complete:
            self._buffer_sample(smoothed_vector)
            # Write to audit log
        current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        phase_label = "LIVE" if self.baseline_complete else "BASELINE"
        self.csv_writer.writerow([
            current_time, phase_label, 
            round(smooth_eda, 4), round(smooth_hr, 4), round(smooth_hrv, 4)
        ])
        return smoothed_vector, self.baseline_complete

    def _apply_ema(self, signal_name: str, current_value: float, alpha: float) -> float:
        """
        Applies the one-pole exponential moving average.
        y_t = α · x_t + (1 − α) · y_{t−1}
        """
        previous_value = self.ema_state[signal_name]
        
        if previous_value is None:
            # Initialize filter with the first sample to prevent a ramp-up artifact
            self.ema_state[signal_name] = current_value
            return current_value
            
        # Compute new smoothed value
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
            arr = np.array(self.buffers[signal])

            # Calculate raw mean and standard deviation
            mu = float(np.mean(arr))
            sigma = float(np.std(arr))

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