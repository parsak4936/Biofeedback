# src/fusion.py
"""
Stress fusion engine.

Turns the three smoothed signals (EDA microsiemens, HR BPM, HRV milliseconds)
into the canonical stress index S_t plus a categorical state (calm / stressed
/ ultra_stressed) and a 0-100 operator score. The thresholds are derived once
from the resting baseline and stay frozen for the session.

This module no longer computes a balloon altitude. Unity owns altitude
control: we expose the state, Unity converts that into "increase / hold /
decrease" balloon movement on its side. Keeping the altitude math out of
Python means the same stress signal can drive any other VR exposure
paradigm without changes here.
"""

import collections
import numpy as np
from config import Config


class FusionEngine:
    """Stress index, classification, and 0-100 display score."""

    # Fallback sigma when baseline computation degenerates (e.g. a sensor was
    # flat for the whole 120 s). Keeps the system in a usable state instead
    # of locking thresholds at 0, which would label every tick ultra-stressed.
    SIGMA_FALLBACK = 1.5

    def __init__(self):
        # 50-sample rolling buffer to smooth S_instant into S_t (1 second at 50 Hz)
        self.s_instant_buffer = collections.deque(maxlen=int(Config.PIPELINE_RATE))

        # Frozen at the end of the 120 s baseline; zero until then.
        self.thresh_mild = 0.0
        self.thresh_high = 0.0

    def reset(self):
        """Discard thresholds and the rolling buffer. Used on operator reset."""
        self.s_instant_buffer.clear()
        self.thresh_mild = 0.0
        self.thresh_high = 0.0
        print("[FUSION] Reset: thresholds cleared, buffer emptied.")

    def set_thresholds(self, sigma_baseline: float):
        """
        Locks in the statistical boundaries for the session based on resting
        variance. Multipliers are configured in Config (math-pipeline Step 8).
        """
        if sigma_baseline is None or sigma_baseline <= 1e-6:
            print(f"[FUSION] WARN: sigma_baseline={sigma_baseline} is degenerate. "
                  f"Using fallback sigma={self.SIGMA_FALLBACK}.")
            sigma_baseline = self.SIGMA_FALLBACK

        self.thresh_mild = Config.THRESH_MILD_K * sigma_baseline
        self.thresh_high = Config.THRESH_HIGH_K * sigma_baseline
        print(f"[FUSION] Thresholds locked -> mild: {self.thresh_mild:.2f} | "
              f"high: {self.thresh_high:.2f}")

    def calculate_baseline_sigma(self, cleaned_buffers: dict, personal_averages: dict) -> float:
        """
        Compute the true noise-floor sigma of S_t from the resting baseline.
        Runs compute_s_instant on every cleaned baseline sample, applies the
        same 50-sample rolling mean used live, and returns the standard
        deviation of the resulting S_t series.
        """
        eda_arr = cleaned_buffers.get('eda')
        hr_arr = cleaned_buffers.get('hr')
        hrv_arr = cleaned_buffers.get('hrv')

        if eda_arr is None or hr_arr is None or hrv_arr is None:
            print("[FUSION] WARN: cleaned buffers missing; falling back to sigma=1.5")
            return 1.5

        n = min(len(eda_arr), len(hr_arr), len(hrv_arr))
        if n < int(Config.PIPELINE_RATE):
            print("[FUSION] WARN: cleaned buffers too small; falling back to sigma=1.5")
            return 1.5

        window = int(Config.PIPELINE_RATE)
        s_instant_series = np.empty(n, dtype=np.float64)
        for i in range(n):
            s_instant_series[i] = self.compute_s_instant(
                [float(eda_arr[i]), float(hr_arr[i]), float(hrv_arr[i])],
                personal_averages
            )

        kernel = np.ones(window, dtype=np.float64) / window
        s_t_series = np.convolve(s_instant_series, kernel, mode='valid')

        sigma = float(np.std(s_t_series))
        print(f"[FUSION] Baseline S_t sigma (dynamic) = {sigma:.4f}")
        return sigma

    def compute_deltas(self, live_vector: list, baseline_averages: dict) -> dict:
        """
        Step 5: per-signal percentage deviations from the personal baseline.
        HRV is inverted because lower HRV means higher arousal. Returns a
        dict so callers can log/display each delta independently.
        """
        eda, hr, hrv = live_vector
        base_eda = baseline_averages['eda'] or 1e-6
        base_hr = baseline_averages['hr'] or 1e-6
        base_hrv = baseline_averages['hrv'] or 1e-6
        return {
            'eda': ((eda - base_eda) / base_eda) * 100.0,
            'hr':  ((hr - base_hr) / base_hr) * 100.0,
            'hrv': ((base_hrv - hrv) / base_hrv) * 100.0,
        }

    def compute_s_instant(self, live_vector: list, baseline_averages: dict) -> float:
        """
        Steps 5 + 6: deviations and weighted fusion. Wraps compute_deltas so
        the deltas are computed in one place.
        """
        d = self.compute_deltas(live_vector, baseline_averages)
        return (Config.WEIGHT_EDA * d['eda']
                + Config.WEIGHT_HRV * d['hrv']
                + Config.WEIGHT_HR * d['hr'])

    def evaluate_state(self, s_instant: float) -> tuple:
        """
        Steps 7 + 9 + 10: apply the 1-second rolling mean to get S_t,
        classify into one of three states, compute the 0-100 display score.

        Returns:
            (S_t, state_label, operator_dashboard_score)
        """
        self.s_instant_buffer.append(s_instant)

        # Wait until the buffer has 1 full second of data before evaluating
        if len(self.s_instant_buffer) < int(Config.PIPELINE_RATE):
            return 0.0, "calm", 0.0

        s_t = sum(self.s_instant_buffer) / len(self.s_instant_buffer)

        # State classification
        if s_t > self.thresh_high:
            state = "ultra_stressed"
        elif s_t > self.thresh_mild:
            state = "stressed"
        else:
            state = "calm"

        # 0-100 dashboard mapping for the operator
        if s_t <= self.thresh_mild:
            display_score = 50.0 * (s_t / self.thresh_mild) if self.thresh_mild > 0 else 0.0
        elif s_t <= self.thresh_high:
            span = self.thresh_high - self.thresh_mild
            display_score = 50.0 + 50.0 * ((s_t - self.thresh_mild) / span) if span > 0 else 50.0
        else:
            display_score = 100.0

        display_score = max(0.0, min(100.0, display_score))
        return s_t, state, display_score


if __name__ == "__main__":
    # Quick standalone sanity check.
    print("[TEST] Initializing Fusion Engine...")
    fusion = FusionEngine()
    fake_baseline = {'eda': 5.0, 'hr': 75.0, 'hrv': 40.0}
    fusion.set_thresholds(1.5)

    stressed_vector = [8.0, 110.0, 20.0]
    for _ in range(50):
        s_inst = fusion.compute_s_instant(stressed_vector, fake_baseline)
        s_t, state, score = fusion.evaluate_state(s_inst)

    deltas = fusion.compute_deltas(stressed_vector, fake_baseline)
    print(f"Vector: {stressed_vector}")
    print(f"Deltas: EDA {deltas['eda']:+.1f}%  HR {deltas['hr']:+.1f}%  HRV {deltas['hrv']:+.1f}%")
    print(f"S_t={s_t:.2f}  state={state.upper()}  score={score:.0f}/100")
