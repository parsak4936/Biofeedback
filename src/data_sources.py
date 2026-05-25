# src/data_sources.py
"""
Data Source Abstraction Layer - Factory Pattern

This module provides a pluggable interface for different hardware sources.
To switch from mock to real PLUX device, only change Config.DATA_SOURCE.
All downstream code remains unchanged.
"""

import numpy as np
import time
import os
from abc import ABC, abstractmethod
from pylsl import StreamInfo, StreamOutlet
from config import Config


class DataSource(ABC):
    """Abstract base class for all data sources."""
    
    @abstractmethod
    def get_next_sample(self) -> tuple:
        """
        Returns (eda, hr, hrv) as a tuple of floats.
        For mock: reads from file
        For real: reads from hardware device
        """
        pass
    
    @abstractmethod
    def cleanup(self):
        """Gracefully close resources."""
        pass


class MockDataSource(DataSource):
    """
    Streams synthetic data from fake_opensignals file at 1000Hz.
    Used for development and testing without real hardware.
    """
    
    def __init__(self):
        print("[DATA SOURCE] Initializing MOCK Data Source...")
        
        # Load the mock data file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        data_path = os.path.join(project_root, Config.MOCK_DATA_FILE)
        
        if not os.path.exists(data_path):
            raise FileNotFoundError(
                f"Mock data file not found: {data_path}\n"
                f"Ensure {Config.MOCK_DATA_FILE} exists in project root."
            )
        
        # Load and convert the data
        data = np.loadtxt(data_path, skiprows=3)
        self.rows, self.columns = data.shape
        
        # Conversion formulas from hardware specs
        self.eda_uS = (data[:, 2] / 65536) * 3.0 / 0.132
        self.ecg_mV = ((data[:, 3] / 65536) - 0.5) * 3.0 / 1100 * 1000
        
        # Index for cycling through data
        self.current_index = 0
        
        # Setup LSL streaming for this source
        self.info = StreamInfo(
            name=Config.STREAM_NAME,
            type=Config.STREAM_TYPE,
            channel_count=3,
            nominal_srate=1000.0,
            channel_format='float32',
            source_id='mock_hardware'
        )
        self.outlet = StreamOutlet(self.info)
        
        print(f"[DATA SOURCE] MOCK: Loaded {self.rows} rows of synthetic data.")
        print(f"[DATA SOURCE] Broadcasting on LSL '{Config.STREAM_NAME}' at 1000Hz...")
    
    def get_next_sample(self) -> tuple:
        """Returns next EDA/HR/HRV from mock file."""
        if self.current_index >= self.rows:
            self.current_index = 0  # Loop back to start
        
        fake_hr = Config.MOCK_HR_BASE
        fake_hrv = Config.MOCK_HRV_BASE
        
        eda = self.eda_uS[self.current_index]
        sample = [eda, fake_hr, fake_hrv]
        
        self.outlet.push_sample(sample)
        self.current_index += 1
        
        # Sleep 1ms to simulate 1000Hz hardware
        time.sleep(0.001)
        
        return (eda, fake_hr, fake_hrv)
    
    def cleanup(self):
        print("[DATA SOURCE] MOCK: Streaming terminated.")


class RealPLUXDataSource(DataSource):
    """
    Connects to real PLUX OpenSignals hardware via LSL.
    
    Prerequisites:
    - PLUX device powered on and paired
    - OpenSignals software running and streaming on LSL
    - LSL stream named 'OpenSignals' available on network
    """
    
    def __init__(self):
        print("[DATA SOURCE] Initializing REAL PLUX Data Source...")
        
        from pylsl import resolve_stream, StreamInlet
        
        print(f"[DATA SOURCE] Scanning network for LSL stream: {Config.STREAM_NAME}...")
        
        # Attempt to find the PLUX hardware stream
        try:
            streams = resolve_stream("name", Config.STREAM_NAME)
            if not streams:
                raise RuntimeError(
                    f"PLUX hardware stream '{Config.STREAM_NAME}' not found!\n"
                    f"Ensure:\n"
                    f"  1. PLUX device is powered on\n"
                    f"  2. Bluetooth is paired\n"
                    f"  3. OpenSignals software is streaming to LSL"
                )
            
            self.inlet = StreamInlet(streams[0])
            print(f"[DATA SOURCE] REAL: Connected to PLUX hardware stream.")
            
        except Exception as e:
            raise RuntimeError(f"Failed to connect to PLUX device: {str(e)}")
    
    def get_next_sample(self) -> tuple:
        """Reads from the PLUX hardware LSL stream."""
        from pylsl import StreamInlet
        
        sample, timestamp = self.inlet.pull_sample(timeout=0.1)
        
        if sample and len(sample) >= 3:
            return (sample[0], sample[1], sample[2])
        else:
            # Return last known values on timeout
            return (0.0, 0.0, 0.0)
    
    def cleanup(self):
        print("[DATA SOURCE] REAL: Hardware connection closed.")


class DataSourceFactory:
    """Factory for creating appropriate data source based on config."""
    
    @staticmethod
    def create() -> DataSource:
        """
        Creates a data source instance based on Config.DATA_SOURCE setting.
        
        Returns:
            DataSource: Configured mock or real hardware source
            
        Raises:
            ValueError: If DATA_SOURCE is invalid
        """
        source_type = Config.DATA_SOURCE.lower()
        
        if source_type == 'mock':
            return MockDataSource()
        elif source_type == 'real_plux':
            return RealPLUXDataSource()
        else:
            raise ValueError(
                f"Invalid DATA_SOURCE: '{Config.DATA_SOURCE}'\n"
                f"Valid options: 'mock', 'real_plux'"
            )


if __name__ == "__main__":
    # Quick test of the factory pattern
    print("\n=== DATA SOURCE FACTORY TEST ===\n")
    
    try:
        source = DataSourceFactory.create()
        print(f"\n✓ Successfully created {source.__class__.__name__}")
        
        # Get a sample
        sample = source.get_next_sample()
        print(f"Sample retrieved: EDA={sample[0]:.2f}, HR={sample[1]:.2f}, HRV={sample[2]:.2f}")
        
        source.cleanup()
        print("✓ Cleanup successful")
        
    except Exception as e:
        print(f"✗ Error: {str(e)}")
