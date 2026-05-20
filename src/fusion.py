# src/fusion.py

import collections
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

    def set_thresholds(self, sigma_baseline: float):
        """
        Locks in the statistical boundaries for the session based on resting variance.
        """
        self.thresh_mild = 1.33 * sigma_baseline
        self.thresh_high = 2.28 * sigma_baseline
        print(f"[FUSION] Thresholds Locked -> Mild: {self.thresh_mild:.2f} | High: {self.thresh_high:.2f}")

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

        # Step 6: Apply Physiological Weights
        s_instant = (0.5 * delta_eda) + (0.3 * delta_hrv) + (0.2 * delta_hr)
        
        return s_instant

    def evaluate_state(self, s_instant: float) -> tuple:
        """
        Step 7 & 9: Applies the rolling mean to output the canonical S_t, 
        and determines the kinematic state for the VR balloon.
        
        Returns:
            tuple: (S_t, state_label, operator_dashboard_score)
        """
        self.s_instant_buffer.append(s_instant)
        
        # Wait until the buffer has 1 full second of data before evaluating
        if len(self.s_instant_buffer) < int(Config.PIPELINE_RATE):
            return 0.0, "calm", 0.0
            
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
            
        # Clamp visual display score
        display_score = max(0.0, min(100.0, display_score))

        return s_t, state, display_score

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
        s_t, state, dashboard = fusion.evaluate_state(s_inst)
        
    print(f"Raw Vector: {stressed_vector}")
    print(f"Computed Instant Stress: {s_inst:.2f}% above baseline")
    print(f"Final S_t: {s_t:.2f} | VR State: {state.upper()} | Dashboard Score: {dashboard:.0f}/100")