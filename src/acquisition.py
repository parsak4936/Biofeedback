# src/acquisition.py

import time
from pylsl import resolve_stream, StreamInlet
from config import Config
import csv
import datetime
import os
class BiofeedbackAcquisition:
    """
    Manages the Lab Streaming Layer (LSL) connection and data ingestion.
    Resolves multi-rate asynchronous biological signals into a synchronous 50Hz vector 
    using a Zero-Order Hold strategy.
    """
    
    def __init__(self):
        self.inlet = self._connect_to_stream()
        self.tick_counter = 0
        self.stale_tick_counter = 0  # NEW: Tracks consecutive empty pulls
        # State variables for Zero-Order Hold (ZOH)
        # These hold the most recent value if the stream has no new data on a given tick
        self.latest_eda = 0.0
        self.latest_hr = 0.0
        self.latest_hrv = 0.0
        
        # Diagnostic tracking
        self.tick_counter = 0
        session_time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        current_dir = os.path.dirname(os.path.abspath(__file__))
        filename = f'acquisition_log_{session_time}.csv'
        project_root = os.path.dirname(current_dir)
        data_dir = os.path.join(project_root, 'data')
       # Setup Auditing Log
        os.makedirs(data_dir, exist_ok=True)
        self.log_file = open(os.path.join(data_dir, filename), mode='w', newline='')
        self.csv_writer = csv.writer(self.log_file)
        self.csv_writer.writerow(['timestamp', 'status', 'raw_eda', 'raw_hr', 'raw_hrv'])
    def _connect_to_stream(self) -> StreamInlet:
        """
        Locates the target LSL stream on the local network and establishes an inlet.
        Blocks execution until the stream is found.
        """
        print(f"[SYSTEM] Scanning network for LSL Stream: {Config.STREAM_NAME}...")
        
        # Resolve stream by name matching our config
        streams = resolve_stream("name", Config.STREAM_NAME)
        
        if not streams:
            raise RuntimeError("Failed to resolve the LSL stream. Is mock_streamer.py running?")
            
        # Create an inlet to receive signal samples from the first matched stream
        inlet = StreamInlet(streams[0])
        print(f"[SUCCESS] Connected to '{Config.STREAM_NAME}'. Inlet established.")
        
        return inlet

    def get_synchronized_sample(self) -> list:
        """
        Pulls data from the LSL stream. 
        If new data is present, updates the state and resets the stale counter. 
        If no data is present, relies on Zero-Order Hold and increments the stale counter.
        Throws an error if the stream goes silent beyond the configured threshold.
        """
        sample, timestamp = self.inlet.pull_sample(timeout=0.0)
        
        if sample:
            # New data arrived; update our holding variables
            self.latest_eda = sample[0]
            self.latest_hr = sample[1]
            self.latest_hrv = sample[2]
            
            self.stale_tick_counter = 0  # Reset the deadman's switch
            status = "NEW_DATA "
        else:
            # No new data; rely on the Zero-Order Hold
            self.stale_tick_counter += 1
            status = "HOLD_LAST"
            
            # The Deadman's Switch Evaluation
            max_stale_ticks = Config.STREAM_TIMEOUT_SEC * Config.PIPELINE_RATE
            if self.stale_tick_counter > max_stale_ticks:
                raise ConnectionError(
                    f"\n[CRITICAL ERROR] Stream Lost: No new data received for {Config.STREAM_TIMEOUT_SEC} seconds. "
                    f"Check PLUX device connection and battery."
                )

        # Diagnostic Printing
        if self.tick_counter % int(Config.PIPELINE_RATE) == 0:
            print(f"[TICK {self.tick_counter:05d}] {status} | "
                  f"EDA: {self.latest_eda:>6.2f} μS | "
                  f"HR: {self.latest_hr:>6.2f} BPM | "
                  f"HRV: {self.latest_hrv:>6.2f} ms")

        self.tick_counter += 1
        # Write to audit log
        current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        self.csv_writer.writerow([
            current_time, status.strip(), 
            round(self.latest_eda, 4), round(self.latest_hr, 4), round(self.latest_hrv, 4)
        ])
        return [self.latest_eda, self.latest_hr, self.latest_hrv]

    def run_standalone_test(self):
        """
        A diagnostic loop to verify ingestion before integrating into the main pipeline.
        Maintains a rigid 50 Hz cycle.
        """
        print("\n[ACQUISITION] Starting 50Hz ingestion test. Press Ctrl+C to stop.\n")
        
        # Calculate the exact time per tick (e.g., 50Hz = 0.02 seconds)
        tick_duration = 1.0 / Config.PIPELINE_RATE
        
        try:
            while True:
                start_time = time.time()
                
                # Fetch the synchronized vector
                vector = self.get_synchronized_sample()
                
                # Calculate how long the fetch took
                elapsed = time.time() - start_time
                
                # Sleep exactly the remainder of the 20ms window to enforce 50 Hz
                sleep_time = tick_duration - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                else:
                    # If processing took longer than 20ms, log a frame drop warning
                    print(f"[WARNING] Processing lag detected. Frame drop at tick {self.tick_counter}.")
                    
        except KeyboardInterrupt:
            print("\n[SYSTEM] Ingestion test terminated.")

if __name__ == "__main__":
    # Diagnostic tracking
    
    acq = BiofeedbackAcquisition()
    acq.run_standalone_test()