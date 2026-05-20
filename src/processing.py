# src/processing.py

import numpy as np
from config import Config
import csv
import datetime
import os
class SignalProcessor:
    """
    Handles EMA smoothing, 120-second baseline buffering, and 3-sigma artifact rejection.
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
        
        self.baseline_complete = False
        self.personal_averages = {}
        self.target_buffer_size = int(Config.BASELINE_SEC * Config.PIPELINE_RATE)
        
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        data_dir = os.path.join(project_root, 'data')
        session_time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f'processing_log_{session_time}.csv'
        os.makedirs(data_dir, exist_ok=True)

        self.log_file = open(os.path.join(data_dir, filename), mode='w', newline='')
        self.csv_writer = csv.writer(self.log_file)
        self.csv_writer.writerow(['timestamp', 'phase', 'smooth_eda', 'smooth_hr', 'smooth_hrv'])

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
        if not self.baseline_complete:
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

    def _compute_personal_baselines(self):
        """
        Executes the 3-sigma artifact removal and computes resting averages.
        This runs exactly once at T = 120s.
        """
        print(f"\n[PROCESSOR] 120-Second Buffer Full. Executing 3-Sigma Cleaning...")
        
        for signal in ['eda', 'hr', 'hrv']:
            arr = np.array(self.buffers[signal])
            
            # Calculate raw mean and standard deviation
            mu = np.mean(arr)
            sigma = np.std(arr)
            
            # 3-Sigma Filter logic: Keep only samples within mu ± 3*sigma
            lower_bound = mu - (3 * sigma)
            upper_bound = mu + (3 * sigma)
            clean_arr = arr[(arr >= lower_bound) & (arr <= upper_bound)]
            
            # Calculate final personal average from the cleaned data
            self.personal_averages[signal] = float(np.mean(clean_arr))
            
            # Diagnostic reporting
            artifacts_removed = len(arr) - len(clean_arr)
            print(f"  -> {signal.upper()}: Removed {artifacts_removed} artifacts. Baseline Avg = {self.personal_averages[signal]:.2f}")
            
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