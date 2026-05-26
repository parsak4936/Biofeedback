# src/fusion.py

import collections
import numpy as np
from config import Config

class FusionEngine:
    """
    Transforms physiological signals into a unified therapeutic stress index
    and calculates kinematic states for the VR simulation.
    """
    def __init__(self):
        # 50-sample rolling buffer to smooth S_instant into S_t (1 second at 50Hz)
        self.s_instant_buffer = collections.deque(maxlen=int(Config.PIPELINE_RATE))

        # Boundaries set dynamically after the 120s baseline
        self.thresh_mild = 0.0
        self.thresh_high = 0.0

        # Session mode is currently read from Config. MODULAR HOOK: when the Unity
        # handshake exists, call set_mode(name) from main.py once the handshake
        # arrives — everything downstream (y_t kinematics, dashboard label) will
        # pick up the new range without any other code change.
        self.mode = Config.SESSION_MODE
        self._apply_mode(self.mode)

    def _apply_mode(self, mode_name: str):
        """Configure altitude range and rate constants for a given mode."""
        params = Config.MODE_RANGES.get(mode_name)
        if params is None:
            print(f"[FUSION] WARN: unknown mode '{mode_name}', falling back to 'easy'")
            mode_name = 'easy'
            params = Config.MODE_RANGES['easy']
        self.mode = mode_name
        self.y_low = params['y_low']
        self.y_high = params['y_high']
        self.y_mid = params['y_mid']
        self.y_t = self.y_mid
        span = self.y_high - self.y_low
        self.k_down = Config.C_DOWN * span
        self.k_up = Config.C_UP * span
        print(f"[FUSION] Mode = {self.mode} | y_low={self.y_low}m, "
              f"y_high={self.y_high}m, y_mid={self.y_mid}m, "
              f"k_down={self.k_down:.3f} m/s, k_up={self.k_up:.3f} m/s")

    def set_mode(self, mode_name: str):
        """Public hook for runtime mode changes (e.g., from a Unity LSL handshake)."""
        self._apply_mode(mode_name)

    # Fallback σ when baseline computation degenerates (e.g. sensor was flat
    # for the whole 120 s). Keeps the system in a usable state instead of
    # locking thresholds at 0 (which would label every tick "ultra_stressed").
    SIGMA_FALLBACK = 1.5

    def set_thresholds(self, sigma_baseline: float):
        """
        Locks in the statistical boundaries for the session based on resting variance.
        Multipliers are configured in Config (math-pipeline Step 8).
        """
        # Guard: degenerate baseline (σ=0 or near-zero) would make every live
        # tick read as ultra-stressed. Fall back to a conservative default and
        # log loudly so the operator knows the calibration was unreliable.
        if sigma_baseline is None or sigma_baseline <= 1e-6:
            print(f"[FUSION] WARN: σ_baseline={sigma_baseline} is degenerate. "
                  f"Using fallback σ={self.SIGMA_FALLBACK}.")
            sigma_baseline = self.SIGMA_FALLBACK

        self.thresh_mild = Config.THRESH_MILD_K * sigma_baseline
        self.thresh_high = Config.THRESH_HIGH_K * sigma_baseline
        print(f"[FUSION] Thresholds Locked -> Mild: {self.thresh_mild:.2f} | High: {self.thresh_high:.2f}")

    def calculate_baseline_sigma(self, cleaned_buffers: dict, personal_averages: dict) -> float:
        """
        Computes the true noise-floor sigma of S_t from the resting baseline.
        Runs compute_s_instant on every cleaned baseline sample, applies the same
        50-sample rolling mean used live, and returns the standard deviation of the
        resulting S_t series.
        """
        eda_arr = cleaned_buffers.get('eda')
        hr_arr = cleaned_buffers.get('hr')
        hrv_arr = cleaned_buffers.get('hrv')

        if eda_arr is None or hr_arr is None or hrv_arr is None:
            print("[FUSION] WARN: cleaned buffers missing; falling back to sigma=1.5")
            return 1.5

        # 3-sigma cleaning can yield slightly different lengths per signal.
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

        # 50-sample rolling mean to simulate S_t
        kernel = np.ones(window, dtype=np.float64) / window
        s_t_series = np.convolve(s_instant_series, kernel, mode='valid')

        sigma = float(np.std(s_t_series))
        print(f"[FUSION] Baseline S_t sigma (dynamic) = {sigma:.4f}")
        return sigma

    def compute_s_instant(self, live_vector: list, baseline_averages: dict) -> float:
        """
        Step 5 & 6: Percentage deviation and weighted instantaneous fusion.
        """
        eda, hr, hrv = live_vector
        
        # Protect against ZeroDivisionError if a baseline scalar somehow locked at absolute zero
        base_eda = baseline_averages['eda'] or 1e-6
        base_hr = baseline_averages['hr'] or 1e-6
        base_hrv = baseline_averages['hrv'] or 1e-6

        # Step 5: Calculate Percentage Deviations (HRV inverted)
        delta_eda = ((eda - base_eda) / base_eda) * 100.0
        delta_hr = ((hr - base_hr) / base_hr) * 100.0
        delta_hrv = ((base_hrv - hrv) / base_hrv) * 100.0

        # Step 6: Apply Physiological Weights (configurable in Config)
        s_instant = (Config.WEIGHT_EDA * delta_eda
                     + Config.WEIGHT_HRV * delta_hrv
                     + Config.WEIGHT_HR * delta_hr)

        return s_instant

    def evaluate_state(self, s_instant: float) -> tuple:
        """
        Step 7, 9 & VR kinematics: Applies the rolling mean to output S_t,
        determines the stress state, computes the dashboard score, and updates
        the VR balloon altitude y_t at a rate proportional to state.

        Returns:
            tuple: (S_t, state_label, operator_dashboard_score, y_t)
        """
        self.s_instant_buffer.append(s_instant)

        # Wait until the buffer has 1 full second of data before evaluating
        if len(self.s_instant_buffer) < int(Config.PIPELINE_RATE):
            return 0.0, "calm", 0.0, self.y_t

        s_t = sum(self.s_instant_buffer) / len(self.s_instant_buffer)

        # Step 9: State Classification
        if s_t > self.thresh_high:
            state = "ultra_stressed"
        elif s_t > self.thresh_mild:
            state = "stressed"
        else:
            state = "calm"

        # Step 11: 0-100 Dashboard Mapping for the Operator
        if s_t <= self.thresh_mild:
            display_score = 50.0 * (s_t / self.thresh_mild) if self.thresh_mild > 0 else 0.0
        elif s_t <= self.thresh_high:
            range_span = self.thresh_high - self.thresh_mild
            display_score = 50.0 + 50.0 * ((s_t - self.thresh_mild) / range_span) if range_span > 0 else 50.0
        else:
            display_score = 100.0

        display_score = max(0.0, min(100.0, display_score))

        # VR balloon kinematic update (per 50Hz tick)
        dt = 1.0 / Config.PIPELINE_RATE
        if state == "ultra_stressed":
            self.y_t -= self.k_down * dt
        elif state == "calm":
            self.y_t += self.k_up * dt
        # "stressed" holds altitude steady
        self.y_t = max(self.y_low, min(self.y_high, self.y_t))

        return s_t, state, display_score, self.y_t

if __name__ == "__main__":
    # A quick standalone test to watch the math work
    print("[TEST] Initializing Fusion Engine...")
    fusion = FusionEngine()
    
    # Fake baseline from processing.py
    fake_baseline = {'eda': 5.0, 'hr': 75.0, 'hrv': 40.0}
    fake_sigma = 1.5 
    
    fusion.set_thresholds(fake_sigma)
    
    # Simulate a highly stressed tick (EDA spiked to 8.0, HR to 110, HRV dropped to 20)
    stressed_vector = [8.0, 110.0, 20.0]
    
    # We loop it 50 times to fill the 1-second rolling buffer
    for _ in range(50):
        s_inst = fusion.compute_s_instant(stressed_vector, fake_baseline)
        s_t, state, dashboard, y_t = fusion.evaluate_state(s_inst)

    print(f"Raw Vector: {stressed_vector}")
    print(f"Computed Instant Stress: {s_inst:.2f}% above baseline")
    print(f"Final S_t: {s_t:.2f} | VR State: {state.upper()} | Dashboard Score: {dashboard:.0f}/100 | y_t: {y_t:.3f}m")