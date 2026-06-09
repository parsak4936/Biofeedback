# src/bsnb_backend.py
"""
=============================================================================
biosignalsnotebooks backend -- STRICT implementation
=============================================================================

Implements HR / HRV / EDA extraction using ONLY functions exposed by the
official PLUX `biosignalsnotebooks` package, per Prof. La Rosa's email
of 2026-06:

  "those values should be extracted from the biosignal sensory input
   directly using the procedures illustrated in
   https://github.com/pluxbiosignals/biosignalsnotebooks"

Selection at runtime is via Config:

    Config.HR_HRV_BACKEND = 'neurokit' | 'bsnb'
    Config.EDA_BACKEND    = 'neurokit' | 'bsnb'

Switching backends does NOT change the stress-fusion math (`src/fusion.py`),
the threshold formula, channel mapping, or any disk format. It changes
ONLY how the three input numbers (EDA uS, HR BPM, RMSSD ms) are derived
from raw ECG / EDA samples.

-----------------------------------------------------------------------------
ECG -> HR / RMSSD chain  (pure biosignalsnotebooks)
-----------------------------------------------------------------------------
1. `detect.detect_r_peaks(ecg, fs)`   -- Pan-Tompkins R-peak detection
                                        (Selvaraj implementation). Internal
                                        steps: BP filter 5-15 Hz, differentiate,
                                        square, 80 ms moving-window integrate,
                                        adaptive thresholding. Returns
                                        (peak_indices, peak_amplitudes); we
                                        keep only peak_indices.
2. `detect.tachogram(peaks, fs)`     -- builds the RR interval series from
                                        the R-peak index array. Returns
                                        (rr_seconds, rr_time_seconds).
3. `extract.remove_ectopy(rr, t)`    -- ectopic-beat cleaner. 20 % rule
                                        applied to ADJACENT RR intervals:
                                        if |RR_i / RR_{i-1} - 1| > 0.20 the
                                        algorithm removes TWO consecutive
                                        entries (the suspect beat plus the
                                        next RR) to eliminate the propagated
                                        artifact, as in Lippman/Stein/Lerman.
4. HR (BPM)    = 60 / mean(cleaned_RR_s)
5. RMSSD (ms)  = sqrt(mean(diff(cleaned_RR_ms)**2))

Note: biosignalsnotebooks `extract.hrv_parameters` returns SDNN, SD1, SD2,
NN20, pNN20, NN50, pNN50, and frequency-domain power bands -- but it does
NOT expose RMSSD. RMSSD is computed here directly from the cleaned RR series
via the standard textbook formula. This is the only place this module
adds anything not in the library, and it is a closed-form computation
from values the library does provide.

WHAT THIS BACKEND DOES NOT DO (deliberately, per prof's request):
  * No Malik 20% gate on diff(RR). The Task Force 1996 rule from
    vret_server.py is NOT applied here. Removal of bad beats is handled
    upstream by `remove_ectopy`.
  * No physiological RR band filter [300, 1500] ms.
  * No RMSSD plausibility band [5, 300] ms.

If you want any of those gates, use HR_HRV_BACKEND='neurokit' instead --
the NeuroKit2 path keeps them. That is by design so we can A/B compare.

-----------------------------------------------------------------------------
EDA chain  (pure biosignalsnotebooks)
-----------------------------------------------------------------------------
biosignalsnotebooks does not ship a phasic / tonic decomposition function.
Three sub-modes are exposed here, switchable via Config.EDA_BSNB_METHOD:

  'raw'              (DEFAULT)
      The phasic value returned is just the live raw EDA sample. No
      decomposition. This is what DataAnalyze.py uses on the saved CSV:
      it computes percent change of rawEDA from baseline-mean(rawEDA).
      Simplest, no extra signal processing.

  'lowpass_subtract' (biosignalsnotebooks EDA tutorial approach)
      tonic  = process.lowpass(eda, 0.05 Hz, order=2, fs=fs, filtfilt=True)
      phasic = eda - tonic
      This is what the package's EDA tutorial recommends and what
      published PLUX example notebooks use for SCR detection. The 0.05 Hz
      cut-off is the standard SCL/SCR separation in the biosignals
      literature.

To switch sub-modes set Config.EDA_BSNB_METHOD in src/config.py.

-----------------------------------------------------------------------------
Why HR / RMSSD warm-up exists at all
-----------------------------------------------------------------------------
HR cannot be computed until at least one RR interval (so two R-peaks) has
been seen -- a few seconds at rest. RMSSD needs a window of many beats to
be statistically stable: PDF says 60 s minimum; with 200 Hz sampling that
is RMSSD_WINDOW_SEC * fs samples. Before each window has filled, the value
is emitted as NaN. The acquisition layer recognises NaN and the fusion
engine substitutes the baseline mean so the score keeps the same scale
through the warm-up. This is independent of the chosen backend.

=============================================================================
"""

from __future__ import annotations

import math
import warnings
import numpy as np

from config import Config

# Hard import. If biosignalsnotebooks is not installed we want the
# operator to see ImportError IMMEDIATELY at startup -- a silent
# fallback to NeuroKit2 here would defeat the comparison the prof asked
# for. Install via `pip install biosignalsnotebooks` if missing.
from biosignalsnotebooks import detect as _bsnb_detect
from biosignalsnotebooks import extract as _bsnb_extract
from biosignalsnotebooks import process as _bsnb_process


# =============================================================================
# Tunables for the EDA sub-mode. Kept here so a sub-mode switch is local.
# =============================================================================

# Cut-off frequency separating tonic SCL from phasic SCR. 0.05 Hz is the
# standard recommendation in the biosignals literature (e.g. Boucsein 2012
# "Electrodermal Activity"); biosignalsnotebooks' EDA tutorial uses the
# same value. Only consulted when EDA_BSNB_METHOD == 'lowpass_subtract'.
_EDA_TONIC_CUTOFF_HZ = 0.05


# =============================================================================
# ECG: R-peak detection via biosignalsnotebooks Pan-Tompkins
# =============================================================================

def _detect_r_peaks_bsnb(ecg_mv, fs_hz):
    """Run `detect.detect_r_peaks` and return JUST the peak-index array.

    detect_r_peaks() returns a tuple (peak_indices, peak_amplitudes); we
    keep the indices for downstream RR analysis.

    NOTE: the bsnb implementation indexes into a slice using `sample_rate`
    as a slice bound (`data[sample_rate:2*sample_rate]`). A float fs blows
    up with TypeError inside the library. We coerce to int defensively."""
    fs_int = int(round(float(fs_hz)))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            result = _bsnb_detect.detect_r_peaks(
                np.asarray(ecg_mv, dtype=float), fs_int,
                time_units=False, volts=False,
                resolution=None, device='biosignalsplux',
                plot_result=False,
            )
        peaks = result[0] if isinstance(result, tuple) else result
        return np.asarray(peaks, dtype=np.int64)
    except Exception:
        return np.asarray([], dtype=np.int64)


# =============================================================================
# RR interval series + ectopy cleaning (pure bsnb)
# =============================================================================

def _rr_series_clean_bsnb(peaks_idx, fs_hz):
    """Pure-bsnb pipeline from R-peak indices to cleaned RR (seconds).

      detect.tachogram(peaks, fs)   -- (rr_s, rr_time_s)
      extract.remove_ectopy(rr, t)  -- 20% adjacent-RR rule, removes 2 entries

    Returns numpy float64 array of cleaned RR intervals in seconds. Empty
    array if fewer than 2 peaks (cannot form even one RR)."""
    if peaks_idx.size < 2:
        return np.asarray([], dtype=float)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            # detect.tachogram returns (rr_seconds, rr_time_seconds) when
            # in_seconds=False (default for the peaks argument); we pass
            # the raw peak indices and the sampling rate.
            rr_s, rr_t = _bsnb_detect.tachogram(
                list(peaks_idx), int(round(fs_hz)),
                signal=False, in_seconds=False, out_seconds=True,
            )
            # remove_ectopy cleans the tachogram in place; returns
            # (cleaned_rr, cleaned_time). Both come back as Python lists.
            cleaned_rr, _ = _bsnb_extract.remove_ectopy(list(rr_s), list(rr_t))
        return np.asarray(cleaned_rr, dtype=float)
    except Exception:
        return np.asarray([], dtype=float)


def _rmssd_from_clean_rr_ms(rr_ms):
    """RMSSD (ms) = sqrt(mean(diff(rr_ms)**2)). Standard textbook formula.

    biosignalsnotebooks does NOT expose this; their `hrv_parameters` only
    returns SDNN, SD1, SD2, NN20/50 etc. So we compute it directly from
    the already-cleaned (post-remove_ectopy) RR series.

    NaN if there are fewer than 2 RR intervals (cannot diff)."""
    if rr_ms.size < 2:
        return float('nan')
    diffs = np.diff(rr_ms)
    return float(np.sqrt(np.mean(diffs ** 2)))


def _hr_bpm_from_clean_rr_s(rr_s):
    """HR (BPM) = 60 / mean(RR_seconds). NaN if no RR."""
    if rr_s.size == 0:
        return float('nan')
    mean_rr = float(np.mean(rr_s))
    if mean_rr <= 0:
        return float('nan')
    return 60.0 / mean_rr


# =============================================================================
# Public ECG API -- mirrors data_sources._compute_hr_rmssd_neurokit
# =============================================================================

def compute_hr_rmssd_bsnb(ecg_window: np.ndarray, fs_hz: float,
                          have_full_hrv_window: bool):
    """Strict biosignalsnotebooks HR + RMSSD from an ECG window.

    Pipeline:
      detect_r_peaks  ->  tachogram  ->  remove_ectopy
                      ->  HR = 60 / mean(RR_s)
                      ->  RMSSD = sqrt(mean(diff(RR_ms)**2))

    `have_full_hrv_window` is True only when the caller has buffered a
    full Config.RMSSD_WINDOW_SEC of ECG. Before that, RMSSD is NaN (PDF
    Section 3 warm-up rule). The check is at this level rather than per-window
    so it stays consistent with the NeuroKit2 backend's warm-up semantics
    and the fusion engine doesn't need to know which backend is active.

    Returns (hr_bpm, rmssd_ms); either or both may be NaN."""
    hr = float('nan')
    rmssd = float('nan')

    if ecg_window.size < int(fs_hz):
        return hr, rmssd

    peaks = _detect_r_peaks_bsnb(ecg_window, fs_hz)
    cleaned_rr_s = _rr_series_clean_bsnb(peaks, fs_hz)
    if cleaned_rr_s.size >= 1:
        hr = _hr_bpm_from_clean_rr_s(cleaned_rr_s)

    if have_full_hrv_window and cleaned_rr_s.size >= 2:
        rmssd = _rmssd_from_clean_rr_ms(cleaned_rr_s * 1000.0)

    return hr, rmssd


def pre_derive_hr_hrv_bsnb(ecg_mv: np.ndarray, fs_hz: float):
    """Offline twin of `compute_hr_rmssd_bsnb` for mock-mode pre-derivation.

    Same recipe; runs the streaming-cadence simulation that
    `data_sources._pre_derive_hr_hrv_neurokit` uses, so MockDataSource
    behaviour is identical regardless of backend. One full-file Pan-
    Tompkins pass, then slice the global peak-index array per window."""
    n = len(ecg_mv)
    hr_series = np.full(n, np.nan, dtype=np.float32)
    hrv_series = np.full(n, np.nan, dtype=np.float32)

    if n < int(fs_hz):
        return hr_series, hrv_series

    all_peaks = _detect_r_peaks_bsnb(np.asarray(ecg_mv, dtype=float), fs_hz)
    if all_peaks.size < 2:
        print("[BSNB] Pre-derivation: <2 R-peaks detected over full file. "
              "HR/HRV will be NaN throughout.")
        return hr_series, hrv_series

    hr_window_n  = int(Config.HR_WINDOW_SEC * fs_hz)
    hrv_window_n = int(Config.RMSSD_WINDOW_SEC * fs_hz)
    step         = max(1, int(Config.HR_COMPUTE_INTERVAL_SEC * fs_hz))

    current_hr    = float('nan')
    current_rmssd = float('nan')
    last_end = 0
    for end in range(step, n + 1, step):
        # HR over trailing HR_WINDOW_SEC of ECG.
        hr_lo = max(0, end - hr_window_n)
        hr_peaks = all_peaks[(all_peaks >= hr_lo) & (all_peaks < end)]
        cleaned_hr_rr_s = _rr_series_clean_bsnb(hr_peaks, fs_hz)
        if cleaned_hr_rr_s.size >= 1:
            hr_val = _hr_bpm_from_clean_rr_s(cleaned_hr_rr_s)
            if np.isfinite(hr_val):
                current_hr = hr_val

        # RMSSD only after the full RMSSD window has filled.
        if end >= hrv_window_n:
            hrv_lo = end - hrv_window_n
            hrv_peaks = all_peaks[(all_peaks >= hrv_lo) & (all_peaks < end)]
            cleaned_hrv_rr_s = _rr_series_clean_bsnb(hrv_peaks, fs_hz)
            if cleaned_hrv_rr_s.size >= 2:
                rmssd_val = _rmssd_from_clean_rr_ms(cleaned_hrv_rr_s * 1000.0)
                if np.isfinite(rmssd_val):
                    current_rmssd = rmssd_val

        hr_series[last_end:end]  = current_hr
        hrv_series[last_end:end] = current_rmssd
        last_end = end

    if last_end < n:
        hr_series[last_end:n]  = current_hr
        hrv_series[last_end:n] = current_rmssd

    return hr_series, hrv_series


# =============================================================================
# EDA: sub-mode dispatch (raw vs lowpass-subtract)
# =============================================================================

def phasic_eda_bsnb(eda_window, fs_hz: float) -> float:
    """Return the live "phasic" EDA value (uS).

    The meaning of "phasic" depends on Config.EDA_BSNB_METHOD:

      'raw'  -- returns the most recent raw EDA sample directly. No
                decomposition. This is what DataAnalyze.py does (it
                uses rawEDA as-is). Default and the prof's preferred
                approach as of his 2026-06 email.

      'lowpass_subtract' -- runs a Butterworth lowpass at 0.05 Hz over
                the rolling raw EDA window, subtracts to get phasic,
                returns the most recent phasic sample. This is the
                approach the biosignalsnotebooks EDA tutorial uses for
                SCR detection.

    Either way the value flows downstream as `current_phasic_eda` and
    is z-scored against `baseline_phasic_mean / sigma` by `fusion.py`
    (unchanged). The fusion math doesn't care whether the value is a
    raw uS or a phasic uS -- it just baselines it."""
    arr = np.asarray(eda_window, dtype=float)
    if arr.size == 0:
        return 0.0

    method = getattr(Config, 'EDA_BSNB_METHOD', 'raw').lower()

    if method == 'raw':
        val = float(arr[-1])
    elif method == 'lowpass_subtract':
        if arr.size < int(fs_hz * 2):
            return 0.0
        try:
            tonic = _bsnb_process.lowpass(arr, _EDA_TONIC_CUTOFF_HZ,
                                          order=2, fs=fs_hz,
                                          use_filtfilt=True)
            phasic = arr - np.asarray(tonic, dtype=float)
            val = float(phasic[-1])
        except Exception:
            return 0.0
    else:
        print(f"[BSNB] Unknown EDA_BSNB_METHOD={method!r}; falling back to 'raw'.")
        val = float(arr[-1])

    if not math.isfinite(val):
        return 0.0
    return val


def decompose_eda_bsnb(eda_signal, fs_hz: float):
    """Offline tonic + phasic decomposition over the whole signal.
    Used by the comparison notebook for plotting. Always uses the
    lowpass-subtract method here regardless of the live-mode setting,
    because the comparison plot is meant to *show* the decomposition."""
    arr = np.asarray(eda_signal, dtype=float)
    if arr.size < int(fs_hz * 2):
        return arr.copy(), np.zeros_like(arr)
    try:
        tonic = np.asarray(
            _bsnb_process.lowpass(arr, _EDA_TONIC_CUTOFF_HZ,
                                  order=2, fs=fs_hz, use_filtfilt=True),
            dtype=float)
        phasic = arr - tonic
    except Exception:
        tonic = arr.copy()
        phasic = np.zeros_like(arr)
    return tonic, phasic


# =============================================================================
# Standalone smoke test
# =============================================================================

if __name__ == '__main__':
    print('[BSNB strict] Smoke test against synthetic data...')
    fs = 200
    t = np.arange(0, 60, 1 / fs)
    rr = 60.0 / 75
    ecg = np.zeros_like(t)
    for k in np.arange(0, 60, rr):
        i = int(k * fs)
        if i + 20 < ecg.size:
            ecg[i:i + 20] = np.hanning(20) * 5
    ecg = ecg + 0.05 * np.random.randn(ecg.size)
    hr, rmssd = compute_hr_rmssd_bsnb(ecg, fs, have_full_hrv_window=True)
    print(f'  HR    = {hr:.2f} BPM   (expected ~75)')
    print(f'  RMSSD = {rmssd} ms     (may be NaN on too-regular synthetic ECG)')

    # EDA mode smoke
    eda = 4.0 + 0.5 * np.sin(2 * np.pi * 0.02 * t) + 0.05 * np.random.randn(t.size)
    print('  EDA_BSNB_METHOD options:')
    Config.EDA_BSNB_METHOD = 'raw'
    print(f'    raw                 -> {phasic_eda_bsnb(eda, fs):+.4f} uS')
    Config.EDA_BSNB_METHOD = 'lowpass_subtract'
    print(f'    lowpass_subtract    -> {phasic_eda_bsnb(eda, fs):+.4f} uS')
