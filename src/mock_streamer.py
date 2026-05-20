# src/mock_streamer.py

import time
import math
import random
import os
import csv
import datetime
from pylsl import StreamInfo, StreamOutlet
from config import Config

class MockOpenSignalsStreamer:
    def __init__(self):
        # 1. Define the LSL Stream Info [cite: 430, 581]
        self.info = StreamInfo(
            name=Config.STREAM_NAME,
            type=Config.STREAM_TYPE,
            channel_count=Config.NUM_CHANNELS,
            nominal_srate=Config.PIPELINE_RATE,
            channel_format='float32',
            source_id='mock_plux_001'
        )
        
        # 2. Create the LSL Outlet
        self.outlet = StreamOutlet(self.info)
        print(f"Mock Streamer Initialized: Broadcasting '{Config.STREAM_NAME}' at {Config.PIPELINE_RATE}Hz")


# 3. State variables for multi-rate tracking
        self.current_hr = Config.MOCK_HR_BASE
        self.current_hrv = Config.MOCK_HRV_BASE
        self.tick_counter = 0
        session_time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f'mock_streamer_log_{session_time}.csv'

        # 4. Setup Ground Truth Log
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        data_dir = os.path.join(project_root, 'data')
        os.makedirs(data_dir, exist_ok=True)
        
        self.log_file = open(os.path.join(data_dir, filename), mode='w', newline='')
        self.csv_writer = csv.writer(self.log_file)
        self.csv_writer.writerow(['timestamp', 'mock_eda', 'mock_hr', 'mock_hrv'])
    def generate_synthetic_data(self, time_t):
        """Generates physiological-looking sine waves with slight noise."""
        # EDA updates continuously at 50Hz (slow drifting wave)
        eda = Config.MOCK_EDA_BASE + math.sin(time_t * 0.1) + random.uniform(-0.05, 0.05)
        
        # HR updates at 1Hz (every 50 ticks)
        if self.tick_counter % int(Config.PIPELINE_RATE / Config.HR_RATE) == 0:
            self.current_hr = Config.MOCK_HR_BASE + (math.sin(time_t * 0.05) * 10) + random.uniform(-2, 2)
            
        # HRV updates at 0.1Hz (every 500 ticks)
        if self.tick_counter % int(Config.PIPELINE_RATE / Config.HRV_RATE) == 0:
            self.current_hrv = Config.MOCK_HRV_BASE + (math.cos(time_t * 0.02) * 15) + random.uniform(-5, 5)

        return [eda, self.current_hr, self.current_hrv]

    def run(self):
        """The main 50Hz broadcast loop."""
        print("Streaming data... Press Ctrl+C to stop.")
        start_time = time.time()
        
        try:
            while True:
                time_t = time.time() - start_time
                
                # Get the current biological frame
                sample = self.generate_synthetic_data(time_t)
                
                # Push the array to the LSL network
                self.outlet.push_sample(sample)
                
                # Write to the Ground Truth log
                current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                self.csv_writer.writerow([
                    current_time, 
                    round(sample[0], 4), 
                    round(sample[1], 4), 
                    round(sample[2], 4)
                ])
                
                self.tick_counter += 1
                
                # Enforce the rigid 50Hz loop
                time.sleep(1.0 / Config.PIPELINE_RATE)
                
        except KeyboardInterrupt:
            print("\nMock Streamer terminated.")
            self.log_file.close()
if __name__ == "__main__":
    streamer = MockOpenSignalsStreamer()
    streamer.run()