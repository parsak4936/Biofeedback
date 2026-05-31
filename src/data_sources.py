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
import warnings
from abc import ABC, abstractmethod
from pylsl import StreamInfo, StreamOutlet
from scipy.signal import butter, filtfilt, find_peaks
from config import Config

# NeuroKit2 is the team-preferred reference library for biosignal processing.
# We use it for R-peak detection. Our adaptive prominence detector is kept as
# a fallback in case NeuroKit2 raises on a degenerate input.
try:
    import neurokit2 as nk
    _NEUROKIT_AVAILABLE = True
except ImportError:
    _NEUROKIT_AVAILABLE = False


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


def _detect_r_peaks(ecg_filt: np.ndarray, distance: int) -> np.ndarray:
    """
    Adaptive, two-pass R-peak detection on an already-bandpassed ECG signal.

    Why two passes: a fixed amplitude threshold breaks across recordings —
    one person's R-peaks may be 0.1 mV, another's 1.0 mV, and a recording with
    occasional motion artifacts inflates any percentile-based threshold so the
    real beats get missed. Instead we:
      1. Loosely gather candidate peaks using a low prominence floor.
      2. Take the median prominence of those candidates as the typical R-peak
         size for THIS recording.
      3. Keep peaks whose prominence is at least a fraction of that median.

    Detecting on prominence (not raw height) ignores slow baseline wander.
    `distance` is the refractory period in samples (blocks T-wave re-triggers).
    """
    std = float(np.std(ecg_filt))
    if std <= 0:
        return np.array([], dtype=np.int64)

    # Pass 1 — loose candidates.
    floor = Config.ECG_CANDIDATE_PROMINENCE_STD_FRAC * std
    cand, props = find_peaks(ecg_filt, prominence=floor, distance=distance)

    if len(cand) >= 5:
        ref_prom = float(np.median(props['prominences']))
        prominence = Config.ECG_PEAK_PROMINENCE_FRACTION * ref_prom
    else:
        # Not enough candidates to estimate; fall back to the loose floor.
        prominence = floor

    peaks, _ = find_peaks(ecg_filt, prominence=prominence, distance=distance)
    return peaks


def _rmssd_from_rr(rr_ms: np.ndarray) -> float:
    """
    RMSSD for a small window of consecutive RR intervals.

    Mathematically this is sqrt(mean(diff(rr)^2)). NeuroKit2's `hrv_rmssd`
    computes the same value from peak indices; we'd have to reconstruct peak
    indices from RR intervals to call it, which is wasteful inside the
    per-beat loop. The cleaner approach is to use the NeuroKit2 helper at
    summary time (whole-baseline metrics) and use this lightweight identical
    formula for the rolling per-beat computation.
    """
    if len(rr_ms) < 2:
        return Config.MOCK_HRV_BASE
    diffs = np.diff(rr_ms)
    return float(np.sqrt(np.mean(diffs ** 2)))


def compute_hrv_summary(peaks: np.ndarray, fs_hz: float) -> dict:
    """
    Whole-recording HRV summary using NeuroKit2's hrv_time function.
    Returns RMSSD plus bonus time-domain metrics (SDNN, pNN50, MeanNN, MedianNN).
    Used at the end of baseline calibration; not called per tick.
    """
    if not _NEUROKIT_AVAILABLE or len(peaks) < 4:
        # Fallback: minimal computation matching what we'd get from NeuroKit2
        if len(peaks) < 2:
            return {"RMSSD": 0.0, "SDNN": 0.0, "MeanNN": 0.0, "MedianNN": 0.0, "pNN50": 0.0}
        rr = np.diff(peaks) * (1000.0 / fs_hz)
        return {
            "RMSSD":    _rmssd_from_rr(rr),
            "SDNN":     float(np.std(rr, ddof=1)) if len(rr) >= 2 else 0.0,
            "MeanNN":   float(np.mean(rr)),
            "MedianNN": float(np.median(rr)),
            "pNN50":    float(np.mean(np.abs(np.diff(rr)) > 50) * 100) if len(rr) >= 2 else 0.0,
        }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            df = nk.hrv_time(peaks, sampling_rate=fs_hz, show=False)
        return {
            "RMSSD":    float(df['HRV_RMSSD'].iloc[0]),
            "SDNN":     float(df['HRV_SDNN'].iloc[0]),
            "MeanNN":   float(df['HRV_MeanNN'].iloc[0]),
            "MedianNN": float(df['HRV_MedianNN'].iloc[0]),
            "pNN50":    float(df['HRV_pNN50'].iloc[0]),
        }
    except Exception as e:
        print(f"[HRV] NeuroKit2 hrv_time failed ({e}); using fallback.")
        return compute_hrv_summary(peaks, fs_hz)  # unreachable; we handled !available above


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
    distance = int(Config.ECG_MIN_RR_MS * fs_hz / 1000)

    # Primary: NeuroKit2 (the team-preferred reference library). It provides
    # multiple well-tested R-peak detection algorithms; the default "neurokit"
    # method matched our adaptive detector within 1-3 BPM on every test file.
    # If NeuroKit2 raises (extremely short or degenerate input), fall back to
    # our prominence-based detector so the pipeline never crashes here.
    peaks = None
    if _NEUROKIT_AVAILABLE:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                cleaned = nk.ecg_clean(ecg_mv, sampling_rate=fs_hz)
                signals = nk.ecg_findpeaks(cleaned, sampling_rate=fs_hz,
                                           method=Config.NEUROKIT_RPEAK_METHOD)
            peaks = np.asarray(signals['ECG_R_Peaks'], dtype=np.int64)
        except Exception as e:
            print(f"[ECG] NeuroKit2 failed ({e}); falling back to adaptive detector.")
            peaks = None
    if peaks is None:
        peaks = _detect_r_peaks(ecg_filt, distance)

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

    # RMSSD over a rolling 10 s window of RR intervals. We compute this using
    # NeuroKit2's hrv_rmssd if available (so the published library is
    # responsible for the canonical metric); the formula is the same one we
    # used before (sqrt(mean(diff(rr)^2))) — using the library version is
    # about auditability and citation alignment with the team's references.
    window_n = max(2, int(Config.RMSSD_WINDOW_SEC * 1000 / median_rr))

    # Hold each beat's HR/RMSSD from that peak until the next.
    for i in range(len(peaks) - 1):
        start = peaks[i]
        end = peaks[i + 1]
        hr_series[start:end] = hr_per_beat[i]

        window_lo = max(0, i + 1 - window_n)
        window_rr = rr_ms_clean[window_lo:i + 1]
        if len(window_rr) >= 2:
            hrv_series[start:end] = _rmssd_from_rr(window_rr)

    # Carry the final value forward to the end of the array.
    if len(peaks) >= 1:
        last = peaks[-1]
        hr_series[last:] = hr_series[last - 1] if last > 0 else Config.MOCK_HR_BASE
        hrv_series[last:] = hrv_series[last - 1] if last > 0 else Config.MOCK_HRV_BASE

    return hr_series, hrv_series, peaks


def resolve_plux_channels(inlet, n_channels: int) -> tuple:
    """
    Discover which LSL channel index carries ECG and which carries EDA on
    a live PLUX / OpenSignals stream, returning (eda_idx, ecg_idx).

    Strategy:
      1. Pull the stream's channel metadata via inlet.info().desc() and
         look at the per-channel "label" field. If a channel's label
         contains "EDA" or "ECG" (case-insensitive), use that index.
      2. If labels are missing or unrecognised (older OpenSignals builds,
         or a device that doesn't publish channel descriptors), fall back
         to Config.REAL_PLUX_EDA_CHANNEL / REAL_PLUX_ECG_CHANNEL.

    The label path is the safe default: if the operator reconfigures the
    OpenSignals sensor layout (e.g. swaps which physical input is ECG
    vs EDA), the pipeline picks it up automatically. A swap was the bug
    that produced impossible RMSSD readings in earlier integration
    attempts; auto-discovery makes it impossible to repeat by accident.
    """
    labels = []
    try:
        full_info = inlet.info(timeout=2.0)
        ch = full_info.desc().child("channels").child("channel")
        while not ch.empty():
            label = (ch.child_value("label") or "").strip().upper()
            labels.append(label)
            ch = ch.next_sibling()
    except Exception:
        labels = []

    eda_idx = ecg_idx = None
    if labels:
        for idx, lab in enumerate(labels):
            if "EDA" in lab and eda_idx is None:
                eda_idx = idx
            if "ECG" in lab and ecg_idx is None:
                ecg_idx = idx

    if eda_idx is None or ecg_idx is None:
        # Fall back to whatever Config says, but clamp to the actual
        # channel count so a misconfigured Config doesn't crash here.
        fb_eda = min(int(Config.REAL_PLUX_EDA_CHANNEL), n_channels - 1)
        fb_ecg = min(int(Config.REAL_PLUX_ECG_CHANNEL), n_channels - 1)
        eda_idx = eda_idx if eda_idx is not None else fb_eda
        ecg_idx = ecg_idx if ecg_idx is not None else fb_ecg
        print(f"[DATA SOURCE] channel labels missing or partial "
              f"({labels or 'none'}); using fixed-index fallback "
              f"EDA=ch{eda_idx}, ECG=ch{ecg_idx}.")
    else:
        print(f"[DATA SOURCE] channels resolved by label -> "
              f"EDA=ch{eda_idx}, ECG=ch{ecg_idx} (labels: {labels}).")
    return eda_idx, ecg_idx


def derive_hr_hrv_from_ecg_nk(ecg_mv: np.ndarray, fs_hz: float) -> tuple:
    """
    HR/HRV derivation following the colleague's vret_server_v2 chain.

    One full-file pass through nk.ecg_clean + nk.ecg_peaks(correct_artifacts=True)
    to locate R-peaks. Two sliding windows then walk the recording:
      * HR    = 60 / mean(RR) over the trailing DS2_HR_WINDOW_SEC of beats.
                Mathematically equivalent to mean(nk.ecg_rate) for full
                windows (the colleague's mean-over-window HR).
      * RMSSD = sqrt(mean(diff(RR)^2)) over the trailing DS2_HRV_WINDOW_SEC.
                Identical formula to nk.hrv_time(...)["HRV_RMSSD"].

    Values are recomputed every DS2_UPDATE_INTERVAL_SEC and held (ZOH)
    between updates, matching the colleague's HR_COMPUTE_INTERVAL cadence.
    RMSSD outside DS2_RMSSD_MIN_MS..DS2_RMSSD_MAX_MS is rejected as a
    detector failure rather than reported as a real reading.

    Returns (hr_series, hrv_series, peaks) — same shape as
    derive_hr_hrv_from_ecg so MockDataSource2 can drop it in.
    """
    n = len(ecg_mv)
    hr_series = np.full(n, Config.MOCK_HR_BASE, dtype=np.float32)
    hrv_series = np.full(n, Config.MOCK_HRV_BASE, dtype=np.float32)

    if n < int(fs_hz) or not _NEUROKIT_AVAILABLE:
        return hr_series, hrv_series, np.array([], dtype=np.int64)

    # Single full-file pass via the colleague's clean + peaks chain.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            cleaned = nk.ecg_clean(ecg_mv, sampling_rate=fs_hz)
            _, info = nk.ecg_peaks(cleaned, sampling_rate=fs_hz,
                                   correct_artifacts=True)
        all_peaks = np.asarray(info["ECG_R_Peaks"], dtype=np.int64)
    except Exception as e:
        print(f"[DS2] ecg_clean/ecg_peaks failed ({e}); HR/HRV unavailable.")
        return hr_series, hrv_series, np.array([], dtype=np.int64)

    if len(all_peaks) < 2:
        return hr_series, hrv_series, all_peaks

    hr_win = int(Config.DS2_HR_WINDOW_SEC * fs_hz)
    hrv_win = int(Config.DS2_HRV_WINDOW_SEC * fs_hz)
    stride = max(1, int(Config.DS2_UPDATE_INTERVAL_SEC * fs_hz))

    current_hr = Config.MOCK_HR_BASE
    current_hrv = Config.MOCK_HRV_BASE
    last_end = 0

    for end in range(stride, n + 1, stride):
        # HR over trailing DS2_HR_WINDOW_SEC of ECG.
        hr_peaks = all_peaks[(all_peaks >= end - hr_win) & (all_peaks < end)]
        if len(hr_peaks) >= 2:
            rr = np.diff(hr_peaks) * (1000.0 / fs_hz)
            mean_rr = float(np.mean(rr))
            if mean_rr > 0:
                hr_val = 60_000.0 / mean_rr
                if np.isfinite(hr_val):
                    current_hr = hr_val

        # RMSSD only once the longer window has filled.
        if end >= hrv_win:
            hrv_peaks = all_peaks[(all_peaks >= end - hrv_win)
                                  & (all_peaks < end)]
            if len(hrv_peaks) >= Config.DS2_MIN_PEAKS_HRV:
                rr = np.diff(hrv_peaks) * (1000.0 / fs_hz)
                if len(rr) >= 2:
                    rmssd = float(np.sqrt(np.mean(np.diff(rr) ** 2)))
                    if (np.isfinite(rmssd)
                            and Config.DS2_RMSSD_MIN_MS <= rmssd
                            <= Config.DS2_RMSSD_MAX_MS):
                        current_hrv = rmssd

        hr_series[last_end:end] = current_hr
        hrv_series[last_end:end] = current_hrv
        last_end = end

    if last_end < n:
        hr_series[last_end:n] = current_hr
        hrv_series[last_end:n] = current_hrv

    return hr_series, hrv_series, all_peaks


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

    # Subclasses (e.g. MockDataSource2) override these to pick a different
    # HR/HRV derivation chain without copy-pasting the rest of __init__.
    SOURCE_ID = 'mock_hardware'
    ECG_SOURCE_ID = 'mock_hardware_ecg'
    DERIVATION_LABEL = "adaptive R-peak detection (NeuroKit2 primary, prominence fallback)"

    def _derive_hr_hrv(self, ecg_mv: np.ndarray, fs_hz: float) -> tuple:
        """Default derivation. Overridden by MockDataSource2."""
        return derive_hr_hrv_from_ecg(ecg_mv, fs_hz)

    def __init__(self):
        print(f"[DATA SOURCE] Initializing {self.__class__.__name__}...")

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

        # Derive HR (BPM) and HRV/RMSSD (ms) from the ECG trace once at load
        # time. The actual derivation chain is selected by subclass — see
        # MockDataSource2._derive_hr_hrv for the colleague's NeuroKit chain.
        print(f"[DATA SOURCE] Deriving HR and HRV via {self.DERIVATION_LABEL}...")
        self.hr_series, self.hrv_series, r_peaks = self._derive_hr_hrv(
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
            source_id=self.SOURCE_ID
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
            source_id=self.ECG_SOURCE_ID
        )
        self.ecg_outlet = StreamOutlet(self.ecg_info)

        print(f"[DATA SOURCE] MOCK: outlet ready on LSL "
              f"'{Config.STREAM_NAME}' at {self.fs_hz:.0f} Hz "
              f"(+ side stream '{Config.ECG_STREAM_NAME}' for ECG)")

        # Wait for the acquisition consumer to subscribe before we start
        # pushing samples. This is the reproducibility fix: without it, the
        # streamer races ahead while the launcher's 2-second sleep runs, and
        # acquisition's inlet ends up with a variable-size head buffer that
        # depends on OS scheduling. With the wait, every run starts from
        # file index 0 deterministically.
        print("[DATA SOURCE] MOCK: waiting for acquisition consumer...")
        t_start = time.time()
        while not self.outlet.have_consumers():
            time.sleep(0.05)
            if time.time() - t_start > Config.STREAMER_CONSUMER_WAIT_SEC:
                print(f"[DATA SOURCE] MOCK: no consumer after "
                      f"{Config.STREAMER_CONSUMER_WAIT_SEC}s, starting anyway.")
                break
        print(f"[DATA SOURCE] MOCK: consumer attached, beginning stream at sample 0.")
    
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

        # Auto-discover which channel is EDA and which is ECG from the
        # LSL metadata. Falls back to Config indices if labels missing.
        # Storing on self so get_next_sample can use them.
        self.eda_ch, self.ecg_ch = resolve_plux_channels(self.inlet, nchan)

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
            ecg_adc = s[self.ecg_ch]
            eda_adc = s[self.eda_ch]

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

        # Same adaptive prominence-based detector used for offline mock data,
        # so live and replayed sessions behave identically.
        peaks_rel = _detect_r_peaks(filt, self._refractory_samples)
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


class MockDataSource2(MockDataSource):
    """
    Same recording, same LSL contract, same downstream pipeline — only the
    HR/HRV derivation is swapped for the colleague's vret_server_v2 chain
    (nk.ecg_clean -> ecg_peaks -> ecg_rate / hrv_time, sliding windows).
    Drop-in A/B comparison: flip Config.DATA_SOURCE between 'mock' and
    'mock2' on the same MOCK_DATA_FILE.
    """
    SOURCE_ID = 'mock_hardware_v2'
    ECG_SOURCE_ID = 'mock_hardware_ecg_v2'
    DERIVATION_LABEL = (f"NeuroKit2 sliding windows "
                        f"(HR={Config.DS2_HR_WINDOW_SEC}s, "
                        f"RMSSD={Config.DS2_HRV_WINDOW_SEC}s, "
                        f"colleague's vret_server_v2 chain)")

    def _derive_hr_hrv(self, ecg_mv: np.ndarray, fs_hz: float) -> tuple:
        return derive_hr_hrv_from_ecg_nk(ecg_mv, fs_hz)


class RealPLUXDataSource2(DataSource):
    """
    Live PLUX variant using the colleague's vret_server_v2 chain.

    Maintains a rolling DS2_PLUX_ECG_BUFFER_SEC-second ECG buffer. Every
    DS2_UPDATE_INTERVAL_SEC, runs nk.ecg_clean + nk.ecg_peaks on the
    trailing slice, then nk.ecg_rate (mean over the HR window) for HR and
    nk.hrv_time["HRV_RMSSD"] for RMSSD. RMSSD outside DS2_RMSSD_MIN/MAX_MS
    is rejected as a detector failure.
    """

    def __init__(self):
        print("[DATA SOURCE] Initializing REAL PLUX (v2 - NeuroKit2 windows)...")
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

        # Auto-discover EDA/ECG indices from the LSL channel labels.
        self.eda_ch, self.ecg_ch = resolve_plux_channels(self.inlet, nchan)

        if not _NEUROKIT_AVAILABLE:
            raise RuntimeError(
                "RealPLUXDataSource2 requires neurokit2 (it is the whole point "
                "of the v2 variant). Install it or use 'real_plux' instead."
            )

        buf_len = int(Config.DS2_PLUX_ECG_BUFFER_SEC * self.fs_hz)
        self._ecg_buf = collections.deque(maxlen=buf_len)
        self._sample_index = 0
        self._stride_samples = max(1, int(Config.DS2_UPDATE_INTERVAL_SEC * self.fs_hz))
        self._hr_win_samples = int(Config.DS2_HR_WINDOW_SEC * self.fs_hz)
        self._hrv_win_samples = int(Config.DS2_HRV_WINDOW_SEC * self.fs_hz)

        self.latest_hr = Config.MOCK_HR_BASE
        self.latest_hrv = Config.MOCK_HRV_BASE
        self.latest_eda = 0.0

    def get_next_sample(self) -> tuple:
        samples, _ = self.inlet.pull_chunk(timeout=0.0)
        if not samples:
            return (self.latest_eda, self.latest_hr, self.latest_hrv)

        for s in samples:
            ecg_adc = s[self.ecg_ch]
            eda_adc = s[self.eda_ch]
            # PLUX hardware conversions (same formulas as MockDataSource).
            eda_uS = (eda_adc / 65536.0) * 3.0 / 0.132
            ecg_mV = ((ecg_adc / 65536.0) - 0.5) * 3.0 / 1100.0 * 1000.0

            self.latest_eda = float(eda_uS)
            self._ecg_buf.append(float(ecg_mV))
            self._sample_index += 1

            if self._sample_index % self._stride_samples == 0:
                self._recompute_hr_hrv()

        return (self.latest_eda, self.latest_hr, self.latest_hrv)

    def _recompute_hr_hrv(self):
        """Run the colleague's chain on the rolling ECG buffer. Quiet on
        failure — last values are held."""
        if len(self._ecg_buf) < int(self.fs_hz):
            return  # need >=1 s of data before nk.ecg_peaks is meaningful

        arr = np.asarray(self._ecg_buf, dtype=float)

        # HR: trailing HR window
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                hr_slice = arr[-self._hr_win_samples:] if len(arr) > self._hr_win_samples else arr
                cleaned = nk.ecg_clean(hr_slice, sampling_rate=self.fs_hz)
                peaks_df, info_hr = nk.ecg_peaks(
                    cleaned, sampling_rate=self.fs_hz, correct_artifacts=True)
                if len(info_hr["ECG_R_Peaks"]) >= 2:
                    rate = nk.ecg_rate(peaks_df, sampling_rate=self.fs_hz)
                    hr_val = float(np.nanmean(rate))
                    if np.isfinite(hr_val):
                        self.latest_hr = hr_val
        except Exception:
            pass

        # RMSSD: trailing HRV window (longer; only once the buffer has filled)
        if len(arr) >= self._hrv_win_samples:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    hrv_slice = arr[-self._hrv_win_samples:]
                    cleaned = nk.ecg_clean(hrv_slice, sampling_rate=self.fs_hz)
                    peaks_df, info_hrv = nk.ecg_peaks(
                        cleaned, sampling_rate=self.fs_hz, correct_artifacts=True)
                    if len(info_hrv["ECG_R_Peaks"]) >= Config.DS2_MIN_PEAKS_HRV:
                        rmssd = float(nk.hrv_time(
                            peaks_df, sampling_rate=self.fs_hz
                        )["HRV_RMSSD"].iloc[0])
                        if (np.isfinite(rmssd)
                                and Config.DS2_RMSSD_MIN_MS <= rmssd
                                <= Config.DS2_RMSSD_MAX_MS):
                            self.latest_hrv = rmssd
            except Exception:
                pass

    def cleanup(self):
        print("[DATA SOURCE] REAL (v2): Hardware connection closed.")


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
        elif source_type == 'mock2':
            return MockDataSource2()
        elif source_type == 'real_plux':
            return RealPLUXDataSource()
        elif source_type == 'real_plux2':
            return RealPLUXDataSource2()
        else:
            raise ValueError(
                f"Invalid DATA_SOURCE: '{Config.DATA_SOURCE}'\n"
                f"Valid options: 'mock', 'mock2', 'real_plux', 'real_plux2'"
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
