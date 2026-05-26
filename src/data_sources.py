# src/data_sources.py
"""
Data Source Abstraction Layer - Factory Pattern

This module provides a pluggable interface for different hardware sources.
To switch from mock to real PLUX device, only change Config.DATA_SOURCE.
All downstream code remains unchanged.
"""

import collections
import json
import numpy as np
import time
import os
from abc import ABC, abstractmethod
from pylsl import StreamInfo, StreamOutlet
from scipy.signal import butter, filtfilt, find_peaks
from config import Config


def parse_opensignals_header(file_path: str) -> dict:
    """
    Read an OpenSignals .txt file's JSON header and return key metadata:
      {
        'fs_hz': float,                # sampling rate
        'sensor_order': [str, ...],    # e.g. ['ECG', 'EDA'] in file column order
        'column_index': {'ECG': int, 'EDA': int},  # absolute column in data matrix
        'columns': [str, ...],         # raw column names from header
      }

    The header looks like:
      # OpenSignals Text File Format. Version 1
      # {"<MAC>": {... "sampling rate": 200, "sensor": ["ECG","EDA"],
      #            "column": ["nSeq","DI","CH1","CH2"], ...}}
      # EndOfHeader
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = [next(f) for _ in range(3)]

    json_line = lines[1].lstrip('#').strip()
    meta = json.loads(json_line)
    # First (only) device key
    device_meta = next(iter(meta.values()))

    sensors = device_meta['sensor']           # e.g. ['ECG', 'EDA']
    columns = device_meta['column']           # e.g. ['nSeq','DI','CH1','CH2']
    fs_hz = float(device_meta['sampling rate'])

    # nSeq + DI are the first two columns; each "CHn" maps to sensors[n-1].
    column_index = {}
    sensor_pos = 0
    for col_idx, col_name in enumerate(columns):
        if col_name.startswith('CH'):
            sensor_name = sensors[sensor_pos]
            column_index[sensor_name] = col_idx
            sensor_pos += 1

    return {
        'fs_hz': fs_hz,
        'sensor_order': sensors,
        'column_index': column_index,
        'columns': columns,
    }


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

        # Parse the OpenSignals header to discover sampling rate and which
        # column carries ECG vs EDA. Different exports have different orderings:
        #   fake_opensignals_2026-05-13_*.txt: 1000 Hz, sensor=[EDA, ECG]
        #   opensignals_2026-05-25_*.txt    :  200 Hz, sensor=[ECG, EDA]
        # Hardcoding either would silently break the other — so we read both.
        header = parse_opensignals_header(data_path)
        self.fs_hz = header['fs_hz']
        col_eda = header['column_index'].get('EDA')
        col_ecg = header['column_index'].get('ECG')
        if col_eda is None or col_ecg is None:
            raise RuntimeError(
                f"OpenSignals file is missing EDA or ECG sensor. "
                f"Found sensors: {header['sensor_order']}"
            )

        # Load the numeric data. Some OpenSignals exports terminate with a
        # truncated trailing row, so we use genfromtxt with invalid_raise=False
        # to skip malformed lines instead of crashing.
        cols_to_read = tuple(range(len(header['columns'])))
        data = np.genfromtxt(
            data_path, skip_header=3, usecols=cols_to_read,
            invalid_raise=False
        )
        # Belt-and-braces: drop any rows that ended up with NaN.
        data = data[np.all(np.isfinite(data), axis=1)]
        self.rows = data.shape[0]

        # PLUX hardware conversions (transducer spec sheet):
        #   EDA (μS) = (raw / 65536) * 3 / 0.132
        #   ECG (mV) = ((raw / 65536) - 0.5) * 3 / 1100 * 1000
        self.eda_uS = (data[:, col_eda] / 65536) * 3.0 / 0.132
        self.ecg_mV = ((data[:, col_ecg] / 65536) - 0.5) * 3.0 / 1100 * 1000

        duration_sec = self.rows / self.fs_hz
        print(f"[DATA SOURCE] File: {os.path.basename(data_path)}")
        print(f"[DATA SOURCE]   fs={self.fs_hz:.0f} Hz  | "
              f"EDA col {col_eda}  | ECG col {col_ecg}  | "
              f"{self.rows} samples ({duration_sec:.1f} s)")

        # Derive HR (BPM) and HRV/RMSSD (ms) from the ECG trace once at load time.
        print("[DATA SOURCE] Deriving HR and HRV/RMSSD from ECG via R-peak detection...")
        self.hr_series, self.hrv_series, r_peaks = derive_hr_hrv_from_ecg(
            self.ecg_mV, fs_hz=self.fs_hz
        )
        avg_bpm = 60.0 * len(r_peaks) / duration_sec if duration_sec > 0 else 0.0
        print(f"[DATA SOURCE]   {len(r_peaks)} R-peaks detected (~{avg_bpm:.1f} BPM avg)")

        # Index for cycling through data
        self.current_index = 0
        # Per-sample sleep, so the streamer paces at the file's native rate.
        self.sample_period = 1.0 / self.fs_hz

        # Main 3-channel stream: [EDA μS, HR BPM, HRV ms]
        self.info = StreamInfo(
            name=Config.STREAM_NAME,
            type=Config.STREAM_TYPE,
            channel_count=3,
            nominal_srate=self.fs_hz,
            channel_format='float32',
            source_id='mock_hardware'
        )
        self.outlet = StreamOutlet(self.info)

        # Side ECG stream — raw mV trace at native rate, for the dashboard's
        # waveform chart. Separate stream so the main 3-channel pipeline is
        # untouched. Real PLUX would publish its own ECG channel similarly.
        self.ecg_info = StreamInfo(
            name=Config.ECG_STREAM_NAME,
            type=Config.ECG_STREAM_TYPE,
            channel_count=1,
            nominal_srate=self.fs_hz,
            channel_format='float32',
            source_id='mock_hardware_ecg'
        )
        self.ecg_outlet = StreamOutlet(self.ecg_info)

        print(f"[DATA SOURCE] MOCK: broadcasting on LSL "
              f"'{Config.STREAM_NAME}' at {self.fs_hz:.0f} Hz "
              f"(+ side stream '{Config.ECG_STREAM_NAME}' for ECG)")
    
    def get_next_sample(self) -> tuple:
        """Returns next EDA/HR/HRV derived from the mock OpenSignals file."""
        if self.current_index >= self.rows:
            self.current_index = 0  # Loop back to start

        i = self.current_index
        eda = float(self.eda_uS[i])
        hr = float(self.hr_series[i])
        hrv = float(self.hrv_series[i])
        ecg = float(self.ecg_mV[i])
        sample = [eda, hr, hrv]

        self.outlet.push_sample(sample)
        # Push the raw ECG mV value on the side stream for the dashboard's
        # waveform chart. Doesn't affect the main pipeline.
        self.ecg_outlet.push_sample([ecg])
        self.current_index += 1

        # Pace at the file's native sample rate so the rest of the pipeline
        # sees realistic timing.
        time.sleep(self.sample_period)

        return (eda, hr, hrv)
    
    def cleanup(self):
        print("[DATA SOURCE] MOCK: Streaming terminated.")


class RealPLUXDataSource(DataSource):
    """
    Connects to real PLUX OpenSignals hardware via LSL and derives
    (EDA μS, HR BPM, RMSSD ms) from the raw ADC channels in real time.

    OpenSignals broadcasts RAW ADC values (CH1, CH2, …) at the device's
    sampling rate. The Python middleware (this class) is responsible for:
      1. ADC -> physical units (μS, mV) using PLUX hardware formulas
      2. Real-time R-peak detection on the ECG (sliding 5 s buffer)
      3. HR (60 000 / RR_ms) and RMSSD (rolling 10 s of RR intervals)

    Prerequisites:
    - PLUX device powered on and paired
    - OpenSignals software running and streaming to LSL
    - LSL stream named Config.STREAM_NAME available on network
    - Config.REAL_PLUX_ECG_CHANNEL / EDA_CHANNEL match your sensor layout
    """

    def __init__(self):
        print("[DATA SOURCE] Initializing REAL PLUX Data Source...")
        from pylsl import resolve_stream, StreamInlet

        print(f"[DATA SOURCE] Scanning network for LSL stream: {Config.STREAM_NAME}...")
        streams = resolve_stream("name", Config.STREAM_NAME)
        if not streams:
            raise RuntimeError(
                f"PLUX hardware stream '{Config.STREAM_NAME}' not found.\n"
                f"  1. Is the PLUX device powered on and paired (Bluetooth)?\n"
                f"  2. Is OpenSignals software running and recording?\n"
                f"  3. Is 'Lab Streaming Layer' enabled in OpenSignals preferences?"
            )

        self.inlet = StreamInlet(streams[0])
        info = self.inlet.info()
        self.fs_hz = float(info.nominal_srate()) or 1000.0
        nchan = info.channel_count()
        print(f"[DATA SOURCE] Connected to '{Config.STREAM_NAME}': "
              f"{nchan} channels @ {self.fs_hz:.0f} Hz")
        print(f"[DATA SOURCE]   reading ECG from channel {Config.REAL_PLUX_ECG_CHANNEL}, "
              f"EDA from channel {Config.REAL_PLUX_EDA_CHANNEL}")

        if Config.REAL_PLUX_ECG_CHANNEL >= nchan or Config.REAL_PLUX_EDA_CHANNEL >= nchan:
            raise RuntimeError(
                f"Config asks for channel {Config.REAL_PLUX_ECG_CHANNEL}/"
                f"{Config.REAL_PLUX_EDA_CHANNEL} but the stream only has {nchan}. "
                f"Adjust REAL_PLUX_ECG_CHANNEL / REAL_PLUX_EDA_CHANNEL in config.py."
            )

        # ---- Streaming R-peak detector state ----
        # Rolling buffer of recent ECG mV samples (5 s by default).
        buf_len = int(Config.REAL_PLUX_ECG_BUFFER_SEC * self.fs_hz)
        self._ecg_buf = collections.deque(maxlen=buf_len)
        self._sample_index = 0          # absolute count since session start
        self._last_peak_index = -10**9  # so the first peak always qualifies
        self._rr_buffer = collections.deque(
            maxlen=int(Config.RMSSD_WINDOW_SEC * 2)  # ~2 beats/s × 10 s
        )

        # Hold values between R-peaks (ZOH); seed with sane defaults.
        self.latest_hr = Config.MOCK_HR_BASE
        self.latest_hrv = Config.MOCK_HRV_BASE
        self.latest_eda = 0.0

        # Pre-compute the bandpass filter once.
        nyq = 0.5 * self.fs_hz
        self._bp_b, self._bp_a = butter(
            2, [Config.ECG_BANDPASS_LOW_HZ / nyq,
                Config.ECG_BANDPASS_HIGH_HZ / nyq], btype='band'
        )
        self._refractory_samples = int(Config.ECG_MIN_RR_MS * self.fs_hz / 1000)

    def get_next_sample(self) -> tuple:
        """
        Pull the most recent ADC sample, convert, run incremental R-peak
        detection, return (eda_uS, hr_bpm, hrv_ms).
        """
        # Drain any backlog and use the latest sample (mirrors the same fix
        # we made in acquisition.py — protects against inlet pile-up).
        samples, _ = self.inlet.pull_chunk(timeout=0.0)
        if not samples:
            return (self.latest_eda, self.latest_hr, self.latest_hrv)

        # Process every backlogged sample so the ECG buffer is dense (we'd
        # miss R-peaks if we only kept the last one). Cheap loop in practice.
        for s in samples:
            ecg_adc = s[Config.REAL_PLUX_ECG_CHANNEL]
            eda_adc = s[Config.REAL_PLUX_EDA_CHANNEL]

            # PLUX hardware conversions (same formulas as MockDataSource).
            eda_uS = (eda_adc / 65536.0) * 3.0 / 0.132
            ecg_mV = ((ecg_adc / 65536.0) - 0.5) * 3.0 / 1100.0 * 1000.0

            self.latest_eda = float(eda_uS)
            self._ecg_buf.append(float(ecg_mV))
            self._sample_index += 1

            # Run R-peak detector roughly 5× per second; cheap and avoids
            # running find_peaks on every single sample.
            if self._sample_index % max(1, int(self.fs_hz // 5)) == 0:
                self._detect_recent_peaks()

        return (self.latest_eda, self.latest_hr, self.latest_hrv)

    def _detect_recent_peaks(self):
        """Run find_peaks on the rolling buffer; update HR/HRV on new peaks."""
        if len(self._ecg_buf) < int(self.fs_hz):  # need ≥1 s of data
            return

        arr = np.asarray(self._ecg_buf)
        try:
            filt = filtfilt(self._bp_b, self._bp_a, arr)
        except ValueError:
            return  # buffer momentarily too short for filtfilt

        height = 0.6 * np.percentile(np.abs(filt), 99)
        peaks_rel, _ = find_peaks(filt, height=height, distance=self._refractory_samples)
        if len(peaks_rel) == 0:
            return

        # Convert relative-to-buffer indices to absolute sample indices.
        buf_start_abs = self._sample_index - len(arr)
        peaks_abs = peaks_rel + buf_start_abs

        # Process any peak we haven't already counted.
        new_peaks = peaks_abs[peaks_abs > self._last_peak_index]
        for p_abs in new_peaks:
            if self._last_peak_index < 0:
                self._last_peak_index = int(p_abs)
                continue
            rr_samples = int(p_abs) - self._last_peak_index
            rr_ms = rr_samples * 1000.0 / self.fs_hz
            self._last_peak_index = int(p_abs)

            # Reject obviously bogus RRs (missed or double beats).
            if rr_ms < Config.ECG_MIN_RR_MS or rr_ms > 2500:
                continue

            self._rr_buffer.append(rr_ms)
            self.latest_hr = 60_000.0 / rr_ms

            if len(self._rr_buffer) >= 2:
                rr_arr = np.asarray(self._rr_buffer)
                self.latest_hrv = float(np.sqrt(np.mean(np.diff(rr_arr) ** 2)))

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
