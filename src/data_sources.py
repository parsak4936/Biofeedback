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
from scipy.signal import butter, filtfilt, find_peaks
from config import Config


def derive_hr_hrv_from_ecg(ecg_mv: np.ndarray, fs_hz: float) -> tuple:
    """
    Convert a 1000Hz ECG trace into per-sample HR (BPM) and RMSSD (ms) arrays.

    Pipeline per the math-pipeline document, Step 0:
      1. Bandpass 5-15 Hz (QRS energy band).
      2. find_peaks with minimum RR refractory.
      3. HR = 60_000 / RR_ms, held until the next beat (Zero-Order Hold).
      4. RMSSD = sqrt(mean(diff(RR)^2)) over a rolling 10 s RR window.
    """
    if len(ecg_mv) < int(fs_hz):
        # Too short to filter — return constants matching old defaults.
        n = len(ecg_mv)
        return (np.full(n, Config.MOCK_HR_BASE, dtype=np.float32),
                np.full(n, Config.MOCK_HRV_BASE, dtype=np.float32),
                np.array([], dtype=np.int64))

    nyq = 0.5 * fs_hz
    b, a = butter(2,
                  [Config.ECG_BANDPASS_LOW_HZ / nyq, Config.ECG_BANDPASS_HIGH_HZ / nyq],
                  btype='band')
    ecg_filt = filtfilt(b, a, ecg_mv)

    # Peak amplitude threshold: 60% of the 99th-percentile (robust to outliers).
    height = 0.6 * np.percentile(np.abs(ecg_filt), 99)
    distance = int(Config.ECG_MIN_RR_MS * fs_hz / 1000)
    peaks, _ = find_peaks(ecg_filt, height=height, distance=distance)

    n = len(ecg_mv)
    hr_series = np.full(n, Config.MOCK_HR_BASE, dtype=np.float32)
    hrv_series = np.full(n, Config.MOCK_HRV_BASE, dtype=np.float32)

    if len(peaks) < 3:
        # Not enough beats to be useful — keep defaults.
        return hr_series, hrv_series, peaks

    rr_ms = np.diff(peaks) * (1000.0 / fs_hz)
    # Drop RR intervals far from the median — missed or extra detections inflate
    # RMSSD wildly. Keep only RRs within 50% of the median.
    median_rr = float(np.median(rr_ms))
    rr_mask = np.abs(rr_ms - median_rr) <= 0.5 * median_rr
    rr_ms_clean = rr_ms.copy()
    rr_ms_clean[~rr_mask] = median_rr  # substitute median for bad RRs
    hr_per_beat = 60_000.0 / rr_ms_clean

    # RMSSD over a rolling 10 s window of RR intervals.
    window_n = max(2, int(Config.RMSSD_WINDOW_SEC * 1000 / median_rr))

    # Hold each beat's HR/RMSSD from that peak until the next.
    for i in range(len(peaks) - 1):
        start = peaks[i]
        end = peaks[i + 1]
        hr_series[start:end] = hr_per_beat[i]

        window_lo = max(0, i + 1 - window_n)
        window_rr = rr_ms_clean[window_lo:i + 1]
        if len(window_rr) >= 2:
            diffs = np.diff(window_rr)
            hrv_series[start:end] = float(np.sqrt(np.mean(diffs ** 2)))

    # Carry the final value forward to the end of the array.
    if len(peaks) >= 1:
        last = peaks[-1]
        hr_series[last:] = hr_series[last - 1] if last > 0 else Config.MOCK_HR_BASE
        hrv_series[last:] = hrv_series[last - 1] if last > 0 else Config.MOCK_HRV_BASE

    return hr_series, hrv_series, peaks


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

        # Conversion formulas from PLUX hardware spec sheet:
        #   EDA (μS) = (CH1 / 65536) * 3 / 0.132
        #   ECG (mV) = ((CH2 / 65536) - 0.5) * 3 / 1100 * 1000
        self.eda_uS = (data[:, 2] / 65536) * 3.0 / 0.132
        self.ecg_mV = ((data[:, 3] / 65536) - 0.5) * 3.0 / 1100 * 1000

        # Derive HR (BPM) and HRV/RMSSD (ms) from the ECG trace once at load time.
        # This matches the "Python Middleware" box in the framework diagram and
        # the math-pipeline Step 0 description.
        print("[DATA SOURCE] Deriving HR and HRV/RMSSD from ECG via R-peak detection...")
        self.hr_series, self.hrv_series, r_peaks = derive_hr_hrv_from_ecg(
            self.ecg_mV, fs_hz=1000.0
        )
        print(f"[DATA SOURCE] Detected {len(r_peaks)} R-peaks "
              f"(~{60.0 * len(r_peaks) / (self.rows / 1000.0):.1f} BPM avg)")

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
        """Returns next EDA/HR/HRV derived from the mock OpenSignals file."""
        if self.current_index >= self.rows:
            self.current_index = 0  # Loop back to start

        i = self.current_index
        eda = float(self.eda_uS[i])
        hr = float(self.hr_series[i])
        hrv = float(self.hrv_series[i])
        sample = [eda, hr, hrv]

        self.outlet.push_sample(sample)
        self.current_index += 1

        # Sleep 1ms to simulate 1000Hz hardware
        time.sleep(0.001)

        return (eda, hr, hrv)
    
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
