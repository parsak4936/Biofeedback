# src/data_sources.py
"""
=============================================================================
Data sources — PDF-aligned (Shayan Itami, NISC Lab · VRET Biofeedback Pipeline)
=============================================================================

Two classes here, both implementing the same `DataSource` interface:

    MockDataSource      – replays a recorded OpenSignals .txt file
    RealPLUXDataSource  – reads live from PLUX biosignalsplux via OpenSignals LSL

Both follow the canonical processing chain from the VRET Biofeedback Pipeline
technical report, Section 2 ("Signals and derived measures") and Section 3
("Run-time architecture"). Quoted directly from the PDF, the chain is:

    ECG raw mV
      → nk.ecg_clean(...)                  # 0.5 Hz HP + 50/60 Hz powerline notch
      → nk.ecg_peaks(correct_artifacts=True)
                                           # signal_fixpeaks repairs missed/doubled beats
      → nk.ecg_rate(...)                   # mean per-sample HR (Bpm)
      → nk.hrv_time(...)["HRV_RMSSD"]      # RMSSD (ms) over 60 s window

    EDA raw μS
      → published as-is to the internal stream
        (EMA smoothing for display lives in processing.py; phasic decomposition
        for the score lives in processing.py too — see PDF §7 Cause 1)

The PDF design principle, restated: every number is computed by a validated
library call (NeuroKit2 for the biosignal math, pylsl for transport) rather
than hand-rolled signal processing. There is no fallback path in this module
because the PDF explicitly rejected the fallback as unreliable (PDF §4 Bug 5).
If NeuroKit2 raises, HR/HRV stay at their last valid value.

-----------------------------------------------------------------------------
Why HR and HRV are not available instantly  (PDF §3)
-----------------------------------------------------------------------------
HR derives from heartbeats. The R-peak detector needs to see several beats
before nk.ecg_rate can return a value; at a resting rate around 80 BPM that
is a few seconds. Until then, HR is emitted as NaN.

RMSSD is a root-mean-square of *successive beat-to-beat differences*. A short
window is statistically noisy — a 10 s window gives an estimator standard
deviation around 56 ms; a 60 s window narrows that to roughly 12 ms. The PDF
chooses 60 s. Before 60 s of ECG has been buffered, RMSSD is emitted as NaN.

NaN is the modular-pipeline equivalent of the PDF's `None`. The acquisition
layer recognises NaN HRV (with otherwise-valid EDA/HR) as "warming up" and
passes it downstream; the fusion engine substitutes the baseline mean so the
score's HRV term contributes zero deviation during warm-up — no faked HRV,
no renormalisation, no dead first minute. This matches PDF §3 exactly.

-----------------------------------------------------------------------------
Channel mapping  (PDF §4 Bug 1 and Bug 4)
-----------------------------------------------------------------------------
EDA and ECG are identified by their channel LABEL ("EDA" / "ECG", case-
insensitive) read from the LSL stream metadata or the OpenSignals .txt
header. If labels are missing or unrecognised, we fall back to the PDF-
corrected fixed mapping:

    3-channel stream  →  ch0 = digital, ch1 = EDA, ch2 = ECG
    2-channel stream  →  ch0 = EDA,     ch1 = ECG

The PDF's Bug 5 sanity check (beat-plausibility test) — confirming that the
channel mapped to ECG actually produces physiological R-peaks — lives in
the main pipeline as a once-per-session validation, not here. This module
just publishes what the labels say.

-----------------------------------------------------------------------------
Architecture (modular-pipeline-specific, not in the PDF)
-----------------------------------------------------------------------------
The PDF describes a single-process procedural server. Our modular pipeline
splits this into a streamer subprocess (this module) and a main pipeline
subprocess (acquisition + fusion). For the split to work end-to-end, BOTH
data source classes publish their derived 3-channel stream
(EDA µS, HR BPM, HRV ms) on `Config.STREAM_NAME` — the internal stream
that acquisition.py subscribes to.

That means the live PLUX path subscribes to PLUX's own raw-ADC LSL stream,
does the same NeuroKit2 derivation chain MockDataSource uses, and republishes
the derived 3-channel stream under our internal name. Flipping
`Config.DATA_SOURCE` from 'mock' to 'real_plux' is then a one-line change
that nothing downstream notices.

-----------------------------------------------------------------------------
How HR/HRV recompute cadence works
-----------------------------------------------------------------------------
Per PDF §3 "Window design": HR and RMSSD are recomputed every 25 ticks at
50 Hz, i.e. every 0.5 s. Between recomputes the values are held (zero-order
hold). MockDataSource pre-derives the entire HR/HRV series at file load by
simulating this same recompute-and-hold pattern offline, so the mock path
behaves identically to the live path on a per-sample basis. The live path
runs the same logic incrementally on a rolling ECG buffer.

=============================================================================
"""

import collections
import json
import os
import time
import warnings
from abc import ABC, abstractmethod

import numpy as np
from pylsl import StreamInfo, StreamOutlet, StreamInlet, resolve_stream

from config import Config

# NeuroKit2 is required. The PDF rejected the prominence-based fallback as
# unreliable (it picks up the T-wave as a second beat on a noisy ECG), so
# we don't carry a fallback here. If NeuroKit2 is missing the import fails
# loudly at startup, which is what we want.
import neurokit2 as nk


# =============================================================================
# PLUX hardware constants and transfer functions
# =============================================================================
# These come from biosignalsnotebooks/conversion.py (raw_to_phy), which is
# the official PLUX-maintained reference for sensor calibration. We pick
# the constants for `device='biosignalsplux'` because that's the hub the
# lab uses.
#
# Earlier values in this file (gain=0.132 for EDA, gain=1.1 for ECG via
# the `_PLUX_ECG_GAIN = 1100` line) were the BITalino constants
# (`bitalino_rev` / `bitalino_riot` in raw_to_phy). Source:
# https://github.com/pluxbiosignals/biosignalsnotebooks/.../conversion.py
# Using BITalino constants on biosignalsplux data produced EDA values
# ~9 % low and ECG values ~8 % low.
#
# The general PLUX transfer function for both ECG and EDA is:
#     phy = (raw * vcc / 2^resolution - vcc * offset) / gain
# where vcc, offset and gain are sensor/device-specific.

_PLUX_ADC_FULL_SCALE = 65536         # 16-bit ADC: 0..65535 (2^16)
_PLUX_REF_VOLTAGE_V = 3.0            # vcc for biosignalsplux family

# EDA, biosignalsplux: offset = 0, gain = 0.12
# (BITalino_rev/riot used gain = 0.132 with vcc = 3.3 -- different device.)
_PLUX_EDA_OFFSET = 0.0
_PLUX_EDA_GAIN = 0.12

# ECG, biosignalsplux: offset = 0.5, gain = 1.019
# (BITalino used gain = 1.1.) The bsnb formula outputs millivolts directly
# when these constants are used, so no extra ×1000 is needed (unlike the
# old code which used gain=1100 and then ×1000 to compensate).
_PLUX_ECG_OFFSET = 0.5
_PLUX_ECG_GAIN_BSNB = 1.019


def adc_to_eda_uS(adc):
    """PLUX EDA transfer function (biosignalsnotebooks/conversion.py).

        EDA (µS) = (raw * 3.0 / 65536 - 3.0 * 0) / 0.12
                 = raw * 3.0 / (65536 * 0.12)

    Returns skin conductance in microsiemens. EDA is unipolar (always >= 0),
    so the offset stays at 0 -- no recentering.
    """
    return ((adc * _PLUX_REF_VOLTAGE_V / _PLUX_ADC_FULL_SCALE)
            - _PLUX_REF_VOLTAGE_V * _PLUX_EDA_OFFSET) / _PLUX_EDA_GAIN


def adc_to_ecg_mV(adc):
    """PLUX ECG transfer function (biosignalsnotebooks/conversion.py).

        ECG (mV) = (raw * 3.0 / 65536 - 3.0 * 0.5) / 1.019

    Returns electrocardiogram voltage in millivolts. ECG is bipolar (swings
    both directions around zero) so offset = 0.5 recenters the ratio so
    R-peaks come out positive against a near-zero baseline.

    Note vs. the previous version: gain is 1.019 (biosignalsplux) instead
    of 1.1 (BITalino), and the * 1000 V->mV conversion is no longer needed
    because bsnb's formula yields millivolts directly with the new gain.
    """
    return ((adc * _PLUX_REF_VOLTAGE_V / _PLUX_ADC_FULL_SCALE)
            - _PLUX_REF_VOLTAGE_V * _PLUX_ECG_OFFSET) / _PLUX_ECG_GAIN_BSNB


# =============================================================================
# OpenSignals .txt header parser
# =============================================================================
# OpenSignals writes a 3-line metadata header on top of every recording:
#
#     # OpenSignals Text File Format. Version 1
#     # {"<MAC>": {... "sampling rate": 1000, "sensor": ["EDA","ECG"], ...}}
#     # EndOfHeader
#
# We parse the JSON middle line to discover:
#   - sampling rate (200 Hz or 1000 Hz depending on the recording config)
#   - which physical column holds which sensor (the order varies between
#     recordings — hardcoding either would silently break the other)


def parse_opensignals_header(file_path: str) -> dict:
    """
    Read an OpenSignals .txt file's JSON header and return key metadata:

        {
          'fs_hz':         float,            # sampling rate
          'sensor_order':  [str, ...],       # e.g. ['ECG', 'EDA']
          'column_index':  {'ECG': int, 'EDA': int},
          'columns':       [str, ...],       # raw column names from header
        }
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        header_lines = [next(f) for _ in range(3)]

    json_line = header_lines[1].lstrip('#').strip()
    meta = json.loads(json_line)
    device_meta = next(iter(meta.values()))  # first (only) device key

    sensors = device_meta['sensor']           # e.g. ['ECG', 'EDA']
    columns = device_meta['column']           # e.g. ['nSeq','DI','CH1','CH2']
    fs_hz = float(device_meta['sampling rate'])

    # nSeq + DI are the first two columns; each "CHn" maps to sensors[n-1].
    column_index = {}
    sensor_pos = 0
    for col_idx, col_name in enumerate(columns):
        if col_name.startswith('CH'):
            column_index[sensors[sensor_pos]] = col_idx
            sensor_pos += 1

    return {
        'fs_hz': fs_hz,
        'sensor_order': sensors,
        'column_index': column_index,
        'columns': columns,
    }


# =============================================================================
# Channel-by-label resolution  (PDF §4 Bug 1)
# =============================================================================

def _resolve_channels_by_label(labels, n_channels):
    """
    Identify EDA and ECG channels by their LSL channel label (case-
    insensitive substring match). Falls back to the PDF-corrected fixed
    order if labels are missing or unrecognised.

    Returns:
        (eda_idx, ecg_idx, source)
        where `source` is 'labels' or 'fallback' for the startup log line.
    """
    eda_idx = ecg_idx = None
    for idx, label in enumerate(labels or []):
        lab = (label or "").strip().upper()
        if "EDA" in lab and eda_idx is None:
            eda_idx = idx
        if "ECG" in lab and ecg_idx is None:
            ecg_idx = idx

    if eda_idx is not None and ecg_idx is not None:
        return eda_idx, ecg_idx, "labels"

    # PDF §4 Bug 4 fallback mapping. Real-1 session bug: OpenSignals can
    # publish a 4-channel LSL stream [nSeq, DI, CH1, CH2] when the device
    # mode includes the digital-input line. The previous code only branched
    # on `n_channels == 2`, so any other channel count fell through to the
    # 3-channel default and silently mapped EDA to the digital-input
    # column (always 0). Result: dashboard EDA reads ~0 the whole session.
    if n_channels == 2:
        return 0, 1, "fallback (2-ch: ch0=EDA, ch1=ECG)"
    if n_channels == 3:
        return 1, 2, "fallback (3-ch: ch0=digital, ch1=EDA, ch2=ECG)"
    if n_channels == 4:
        # OpenSignals 4-ch layout: [nSeq, DI, CH1, CH2] -> CH1=EDA, CH2=ECG.
        return 2, 3, "fallback (4-ch: nSeq/DI/CH1/CH2 -> ch2=EDA, ch3=ECG)"
    # Unknown shape — return the last two channels as a best guess but
    # flag the source loudly so the operator notices on startup.
    return n_channels - 2, n_channels - 1, (
        f"fallback (unknown {n_channels}-ch layout: "
        f"ch{n_channels - 2}=EDA, ch{n_channels - 1}=ECG)")


def _read_lsl_channel_labels(inlet, timeout_sec=2.0):
    """Read the per-channel `label` field from an LSL inlet's metadata.
    Returns a list of upper-cased label strings, or [] on failure."""
    labels = []
    try:
        full = inlet.info(timeout=timeout_sec)
        ch = full.desc().child("channels").child("channel")
        while not ch.empty():
            labels.append((ch.child_value("label") or "").strip().upper())
            ch = ch.next_sibling()
    except Exception:
        labels = []
    return labels


def _resolve_channels_by_content(inlet, n_channels, fs_hz,
                                  sample_sec=5.0):
    """
    Content-based channel auto-detect for the live PLUX stream.

    Pulls ~sample_sec of raw samples and fingerprints each channel
    against three independent tests:

      1. Variance: a channel with near-zero variance is the device's
         digital-input line (DI), always 0. Skipped.
      2. Monotonicity: a channel that strictly increases over time is
         the sample-sequence counter (nSeq). Skipped.
      3. Beat-plausibility (PDF §4 Bug 5): treat each candidate channel
         AS IF it were ECG, clean it with nk.ecg_clean, run nk.ecg_peaks,
         then measure what fraction of resulting RR intervals fall in
         the human-plausible 300-1500 ms band (40-200 BPM). Real ECG
         scores ~1.0; EDA / DI / nSeq force-fed through the detector
         score near zero.
      4. EDA-range check: convert the channel as if it were EDA (ADC ->
         microsiemens). Real resting EDA falls in 0.1-30 uS.

    Picks ECG = channel with the highest beat-plausibility (>= 0.7).
    Picks EDA = remaining non-DI, non-nSeq channel whose ADC->uS
                conversion mean falls inside [0.1, 30] uS.

    Returns (eda_idx, ecg_idx, source_string) on success.
    Raises RuntimeError with a clear operator message on failure
    (no plausible ECG channel, or no plausible EDA channel) so the
    session never starts with mis-mapped channels silently producing
    zero-EDA or impossible-RMSSD output.
    """
    print(f"[DATA SOURCE]   Auto-detect: sampling {sample_sec:.1f}s "
          f"of raw data ({n_channels} channels)...")

    # Pull a chunk. inlet is already open at this point.
    deadline = time.time() + sample_sec
    collected = []
    while time.time() < deadline:
        try:
            chunk, _ = inlet.pull_chunk(timeout=0.1,
                                         max_samples=int(fs_hz))
            if chunk:
                collected.extend(chunk)
        except Exception:
            break
    if len(collected) < int(fs_hz):
        raise RuntimeError(
            f"channel auto-detect: only got {len(collected)} samples "
            f"in {sample_sec:.1f}s (need at least {int(fs_hz)}). The "
            f"PLUX stream looks dead. Is OpenSignals recording?"
        )

    arr = np.asarray(collected, dtype=float)
    print(f"[DATA SOURCE]     got {arr.shape[0]} samples, "
          f"analysing channels...")

    fingerprints = []
    for ch in range(n_channels):
        col = arr[:, ch]
        col_var = float(np.var(col))
        # Monotonic = sample counter (nSeq). Strict increase + spread > 50%.
        if col.size >= 2:
            is_monotonic = bool(np.all(np.diff(col) >= 0)
                                 and col.max() > col.min() * 1.5
                                 and col_var > 1e6)
        else:
            is_monotonic = False
        # EDA-as-µS interpretation.
        eda_uS_mean = float(np.mean(adc_to_eda_uS(col)))
        # Beat-plausibility: only test channels that have real
        # signal energy (skip DI and nSeq).
        beat_plaus = 0.0
        if not is_monotonic and col_var > 1e3:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    ecg_mV_col = adc_to_ecg_mV(col)
                    cleaned = nk.ecg_clean(ecg_mV_col,
                                            sampling_rate=fs_hz)
                    _, info = nk.ecg_peaks(cleaned,
                                            sampling_rate=fs_hz,
                                            correct_artifacts=True)
                peaks = np.asarray(info["ECG_R_Peaks"])
                if peaks.size >= 3:
                    rr = np.diff(peaks) * 1000.0 / fs_hz
                    beat_plaus = float(np.mean((rr >= 300)
                                                & (rr <= 1500)))
            except Exception:
                beat_plaus = 0.0
        fingerprints.append({
            'ch': ch, 'var': col_var, 'monotonic': is_monotonic,
            'eda_uS_mean': eda_uS_mean, 'beat_plaus': beat_plaus,
        })
        print(f"[DATA SOURCE]     ch{ch}: var={col_var:.2e}  "
              f"monotonic={'Y' if is_monotonic else 'N'}  "
              f"EDA-as-uS={eda_uS_mean:.3f}  "
              f"beat-plaus={beat_plaus:.2f}")

    # ECG = highest beat-plausibility, must clear 0.7.
    ecg_candidates = [fp for fp in fingerprints if fp['beat_plaus'] >= 0.7]
    if not ecg_candidates:
        best = max(fingerprints, key=lambda fp: fp['beat_plaus'])
        raise RuntimeError(
            f"channel auto-detect: no channel produced plausible "
            f"heartbeats (best beat-plausibility = {best['beat_plaus']:.2f} "
            f"on ch{best['ch']}, threshold 0.7). Check ECG electrode "
            f"contact and chest-lead placement, then retry."
        )
    ecg_choice = max(ecg_candidates, key=lambda fp: fp['beat_plaus'])
    ecg_idx = ecg_choice['ch']

    # EDA = remaining channel with realistic µS range, not DI, not nSeq.
    eda_candidates = [
        fp for fp in fingerprints
        if fp['ch'] != ecg_idx
        and not fp['monotonic']
        and 0.1 <= fp['eda_uS_mean'] <= 30.0
        and fp['var'] > 1e3
    ]
    if not eda_candidates:
        raise RuntimeError(
            f"channel auto-detect: picked ECG=ch{ecg_idx} (plaus="
            f"{ecg_choice['beat_plaus']:.2f}), but no remaining "
            f"channel has a realistic EDA range "
            f"(0.1-30 µS after ADC conversion). Check EDA finger-pad "
            f"contact and gel."
        )
    # Among EDA candidates, pick the one with the largest µS mean —
    # ECG-as-EDA gives near-zero, DI gives 0, real EDA gives 3-10 µS.
    eda_idx = max(eda_candidates,
                   key=lambda fp: fp['eda_uS_mean'])['ch']

    return eda_idx, ecg_idx, (
        f"auto-detect by content (ecg-plaus="
        f"{ecg_choice['beat_plaus']:.2f}, "
        f"eda-mean={fingerprints[eda_idx]['eda_uS_mean']:.2f} µS)"
    )


# =============================================================================
# Malik / Task Force (1996) RR-interval artifact gate
# =============================================================================
# NeuroKit's Kubios artifact correction (correct_artifacts=True) reconstructs
# missed beats by interpolation — but does NOT remove interpolated intervals,
# so a few bad beats still inflate a time-domain RMSSD. The Malik / Task
# Force 1996 rule is the standard add-on: reject any beat whose RR change
# RELATIVE to the previous RR exceeds RR_MAX_RELATIVE_CHANGE (20%). Relative
# because the tolerable beat-to-beat change scales with heart rate.
#
# Applied identically to baseline and live RMSSD so they are measured the
# same way. Mirrors `gated_rmssd_from_peaks` in vret_server.py.

def _gated_rmssd_from_peaks(peak_indices, fs_hz):
    """
    RMSSD (ms) from R-peak sample indices with Malik 20% RR gate.

    Steps:
      1. RR intervals (ms) from consecutive peak indices.
      2. Clip to physiologically plausible band [RR_MIN_MS, RR_MAX_MS].
      3. Drop successive differences whose relative change > 20%.
      4. RMSSD = sqrt(mean(surviving diffs^2)).
      5. Reject result if outside [RMSSD_MIN_MS, RMSSD_MAX_MS]
         (PDF §4 Bug 3 plausibility band).

    Returns NaN if any gate fails. Returning NaN keeps the data source's
    "warmup / not measurable" semantics consistent.
    """
    pk = np.asarray(peak_indices, dtype=np.int64)
    if pk.size < 2:
        return float('nan')
    rr = np.diff(pk) * 1000.0 / fs_hz
    rr = rr[(rr >= Config.RR_MIN_MS) & (rr <= Config.RR_MAX_MS)]
    if rr.size < 2:
        return float('nan')
    rel_change = np.abs(np.diff(rr)) / rr[:-1]
    diffs = np.diff(rr)[rel_change <= Config.RR_MAX_RELATIVE_CHANGE]
    # Need MIN_PEAKS_HRV - 1 *clean* diffs for a meaningful estimate
    if diffs.size < (Config.MIN_PEAKS_HRV - 1):
        return float('nan')
    r = float(np.sqrt(np.mean(diffs ** 2)))
    if not (np.isfinite(r) and Config.RMSSD_MIN_MS <= r <= Config.RMSSD_MAX_MS):
        return float('nan')
    return r


# =============================================================================
# HR / RMSSD recompute (canonical NeuroKit2 chain — PDF §2)
# =============================================================================
# Both data sources call this on a trailing window of ECG samples (raw mV).
# Returns (hr_bpm, rmssd_ms); either can be NaN to mean "not measurable yet".
# This is the SINGLE place where the chain lives — no duplication between
# mock and live paths.

_BSNB_IMPORT_FAILED = False  # latched after first failure to avoid log spam


def _compute_hr_rmssd(ecg_window: np.ndarray, fs_hz: float,
                      have_full_hrv_window: bool):
    """Dispatch to the configured backend. Default = NeuroKit2 (below).
    'bsnb' routes to biosignalsnotebooks (Pan-Tompkins).

    If the bsnb backend cannot be loaded (e.g. biosignalsnotebooks
    installed --no-deps and missing bokeh / h5py / IPython), we fall
    back to NeuroKit2 with a single loud warning instead of crashing
    the streamer. The fallback is latched so the operator sees the
    warning once, not once per recompute."""
    global _BSNB_IMPORT_FAILED
    if (getattr(Config, 'HR_HRV_BACKEND', 'neurokit').lower() == 'bsnb'
            and not _BSNB_IMPORT_FAILED):
        try:
            from bsnb_backend import compute_hr_rmssd_bsnb
            return compute_hr_rmssd_bsnb(ecg_window, fs_hz, have_full_hrv_window)
        except ImportError as e:
            _BSNB_IMPORT_FAILED = True
            print(f"[DATA SOURCE] WARN: bsnb backend failed to import "
                  f"({type(e).__name__}: {e}). Falling back to NeuroKit2 "
                  f"for HR/RMSSD. Install the missing module with "
                  f"`pip install --no-deps <module-name>` if you want bsnb.")
    return _compute_hr_rmssd_neurokit(ecg_window, fs_hz, have_full_hrv_window)


def _compute_hr_rmssd_neurokit(ecg_window: np.ndarray, fs_hz: float,
                                have_full_hrv_window: bool):
    """
    Run the PDF chain on a trailing ECG window:

        nk.ecg_clean  →  nk.ecg_peaks(correct_artifacts=True)
                      →  nk.ecg_rate          (HR)
                      →  nk.hrv_time["HRV_RMSSD"]   (RMSSD)

    Parameters
    ----------
    ecg_window : raw ECG voltage samples (mV), length = up to HR_WINDOW_SEC×fs
                 for HR, or RMSSD_WINDOW_SEC×fs for RMSSD. Caller decides
                 which window length to pass.
    fs_hz      : sampling rate of the ECG window.
    have_full_hrv_window : True only when the caller has a full
                 RMSSD_WINDOW_SEC of ECG buffered. Below that, RMSSD is NaN
                 by design (PDF §3: "no valid live RMSSD in the first minute").

    Returns
    -------
    (hr_bpm, rmssd_ms) — both float; either or both can be NaN.

    The RMSSD plausibility band (PDF §4 Bug 3): values outside
    [PDF_RMSSD_MIN_MS, PDF_RMSSD_MAX_MS] are rejected as detector failures
    rather than reported as measurements. The Bug 3 fix is captured by
    `correct_artifacts=True` on nk.ecg_peaks, which invokes
    nk.signal_fixpeaks internally to repair missed/doubled beats.
    """
    hr = float('nan')
    rmssd = float('nan')

    if ecg_window.size < int(fs_hz):
        # Need at least one second of ECG before NeuroKit behaves at all.
        return hr, rmssd

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            cleaned = nk.ecg_clean(ecg_window, sampling_rate=fs_hz)
            signals, info = nk.ecg_peaks(
                cleaned, sampling_rate=fs_hz, correct_artifacts=True,
            )
    except Exception:
        # NeuroKit raised — leave both as NaN, caller holds last valid.
        return hr, rmssd

    n_peaks = len(info["ECG_R_Peaks"])

    # ---- HR via nk.ecg_rate (mean across the window) ----
    if n_peaks >= 2:
        try:
            rate = nk.ecg_rate(signals, sampling_rate=fs_hz)
            hr_val = float(np.nanmean(rate))
            if np.isfinite(hr_val):
                hr = hr_val
        except Exception:
            pass

    # ---- RMSSD via Malik-gated RR intervals (vret_server.py) ----
    # PDF §3: "There is simply no valid live RMSSD in the first minute".
    # Until the caller has a full RMSSD_WINDOW_SEC of ECG, RMSSD stays NaN.
    # Once available, we measure RMSSD via _gated_rmssd_from_peaks — which
    # applies the Malik 20% RR-change rule on top of Kubios's already-applied
    # peak correction, so interpolated-beat artifacts don't inflate the
    # estimate. Result is rejected (NaN) if outside the plausibility band.
    if have_full_hrv_window and n_peaks >= Config.MIN_PEAKS_HRV:
        rmssd_gated = _gated_rmssd_from_peaks(info["ECG_R_Peaks"], fs_hz)
        if np.isfinite(rmssd_gated):
            rmssd = rmssd_gated

    return hr, rmssd


# =============================================================================
# Abstract base class
# =============================================================================

class DataSource(ABC):
    """Common interface for mock and real data sources."""

    @abstractmethod
    def get_next_sample(self) -> tuple:
        """
        Returns one (eda_uS, hr_bpm, rmssd_ms) tuple.
        hr_bpm or rmssd_ms may be NaN to mean "warming up / no valid
        measurement yet" — downstream code handles NaN explicitly.
        """
        pass

    @abstractmethod
    def cleanup(self):
        """Gracefully release resources (file handles, sockets)."""
        pass


# =============================================================================
# MockDataSource — replays a recorded file
# =============================================================================

class MockDataSource(DataSource):
    """
    Replays an OpenSignals .txt recording at its native sample rate.

    What it does at construction
    ----------------------------
    1. Locates and reads the file named in Config.MOCK_DATA_FILE.
    2. Parses the JSON header → sampling rate, sensor order, column indices.
    3. Converts the raw ADC columns into physical units:
         - EDA channel → microsiemens via adc_to_eda_uS
         - ECG channel → millivolts  via adc_to_ecg_mV
    4. Pre-derives the per-sample HR and HRV time series offline by
       SIMULATING THE SAME RECOMPUTE-AND-HOLD CADENCE the live PLUX path
       uses (HR_COMPUTE_INTERVAL_SEC = 0.5 s, HR_WINDOW_SEC = 30 s,
       RMSSD_WINDOW_SEC = 60 s). The result is identical to what would
       happen if you streamed the file through the live path in real time,
       but it's much cheaper: nk.ecg_clean + nk.ecg_peaks runs ~2×/second
       of recording rather than 1000×/second.
    5. Opens two LSL outlets:
         - Config.STREAM_NAME       → derived 3-channel (EDA, HR, HRV)
         - Config.ECG_STREAM_NAME   → raw ECG voltage for the dashboard

    What it does per call to get_next_sample
    ----------------------------------------
    6. Looks up the next pre-derived (eda, hr, hrv) tuple by sample index.
    7. Pushes the tuple on the derived outlet, and the raw ECG sample on
       the side outlet (for the dashboard's waveform chart).
    8. Sleeps the per-sample period so the stream paces at the file's
       native rate. End-of-file is handled per Config.MOCK_LOOP.

    Channel mapping
    ---------------
    Read from the file header by sensor name ("EDA", "ECG"). The file
    header is authoritative — recordings exist with both ECG-first and
    EDA-first column orders, so hardcoding either would silently break
    the other.

    HR/HRV warm-up
    --------------
    Because the pre-derivation simulates the same windowing the live path
    uses, the first ~60 s of HR/HRV in the file are NaN (PDF §3). The
    baseline in main.py only uses non-NaN samples for the per-signal
    averages, so this is correct by design.
    """

    def __init__(self):
        print(f"[DATA SOURCE] Initializing MockDataSource...")

        # Locate the file relative to project root.
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_path = os.path.join(project_root, Config.MOCK_DATA_FILE)
        if not os.path.exists(data_path):
            raise FileNotFoundError(
                f"Mock data file not found: {data_path}\n"
                f"Set Config.MOCK_DATA_FILE to an existing OpenSignals .txt "
                f"recording in the project's data/ folder."
            )

        # Parse the header to learn fs and channel layout.
        header = parse_opensignals_header(data_path)
        self.fs_hz = header['fs_hz']
        col_eda = header['column_index'].get('EDA')
        col_ecg = header['column_index'].get('ECG')
        if col_eda is None or col_ecg is None:
            raise RuntimeError(
                f"OpenSignals file is missing EDA or ECG sensor. "
                f"Found sensors: {header['sensor_order']}"
            )

        # Load the numeric data. Some exports terminate with a truncated
        # final row; invalid_raise=False skips malformed lines instead of
        # crashing the load.
        n_cols = len(header['columns'])
        data = np.genfromtxt(
            data_path, skip_header=3, usecols=tuple(range(n_cols)),
            invalid_raise=False,
        )
        # Belt-and-braces: drop any rows that ended up with NaN.
        data = data[np.all(np.isfinite(data), axis=1)]
        self.rows = data.shape[0]
        duration_s = self.rows / self.fs_hz

        # ADC → physical units, vectorised.
        self.eda_uS = adc_to_eda_uS(data[:, col_eda])
        self.ecg_mV = adc_to_ecg_mV(data[:, col_ecg])

        print(f"[DATA SOURCE]   file = {os.path.basename(data_path)}")
        print(f"[DATA SOURCE]   fs   = {self.fs_hz:.0f} Hz")
        print(f"[DATA SOURCE]   EDA  = column {col_eda}, ECG = column {col_ecg}")
        print(f"[DATA SOURCE]   {self.rows} samples ({duration_s:.1f} s)")

        # Pre-derive HR and HRV using the same streaming algorithm the live
        # PDF path uses. See _pre_derive_hr_hrv for the simulation logic.
        print(f"[DATA SOURCE]   Pre-deriving HR + RMSSD via NeuroKit2 "
              f"(simulating PDF streaming chain)...")
        t0 = time.time()
        self.hr_series, self.hrv_series = _pre_derive_hr_hrv(
            self.ecg_mV, self.fs_hz,
        )
        n_real_hr = int(np.sum(np.isfinite(self.hr_series)))
        n_real_hrv = int(np.sum(np.isfinite(self.hrv_series)))
        elapsed = time.time() - t0
        print(f"[DATA SOURCE]   HR valid in {n_real_hr}/{self.rows} samples, "
              f"RMSSD valid in {n_real_hrv}/{self.rows}.")
        print(f"[DATA SOURCE]   Pre-derivation took {elapsed:.1f} s.")

        # Indices and pacing.
        # We pace against wall-clock with time.perf_counter() rather than
        # sleeping `1/fs_hz` per sample, because Windows' time.sleep has a
        # ~15 ms granularity. Sleeping 1 ms per sample on Windows actually
        # sleeps ~15 ms → the file plays back at ~65 Hz instead of 1000 Hz,
        # and the 60 s HRV warm-up never finishes within a 120 s baseline.
        # Catch-up: each get_next_sample() call pushes as many samples as
        # wall-clock has advanced since the last call, then sleeps briefly
        # so we don't burn CPU.
        self.current_index = 0
        self._stream_start_time = None      # set on the first get_next_sample()
        self._eof_announced = False

        # ---- LSL outlets ----
        # Derived 3-channel stream (EDA µS, HR BPM, HRV ms). Acquisition.py
        # subscribes to this. NaN values flow through to mark warm-up.
        self.info = StreamInfo(
            name=Config.STREAM_NAME,
            type=Config.STREAM_TYPE,
            channel_count=3,
            nominal_srate=self.fs_hz,
            channel_format='float32',
            source_id='mock_hardware',
        )
        self.outlet = StreamOutlet(self.info)

        # Side stream: raw ECG voltage for the dashboard's waveform chart.
        # Doesn't interact with the math pipeline.
        self.ecg_info = StreamInfo(
            name=Config.ECG_STREAM_NAME,
            type=Config.ECG_STREAM_TYPE,
            channel_count=1,
            nominal_srate=self.fs_hz,
            channel_format='float32',
            source_id='mock_hardware_ecg',
        )
        self.ecg_outlet = StreamOutlet(self.ecg_info)

        print(f"[DATA SOURCE]   Outlet ready on LSL '{Config.STREAM_NAME}' "
              f"({self.fs_hz:.0f} Hz) + side stream '{Config.ECG_STREAM_NAME}'.")

        # Wait for acquisition.py to subscribe before pushing samples — this
        # is the reproducibility fix. Without it the streamer races ahead
        # during subprocess startup and the file's first samples are
        # consumed before acquisition is listening.
        print("[DATA SOURCE]   Waiting for acquisition consumer...")
        t_start = time.time()
        while not self.outlet.have_consumers():
            time.sleep(0.05)
            if time.time() - t_start > Config.STREAMER_CONSUMER_WAIT_SEC:
                print(f"[DATA SOURCE]   No consumer after "
                      f"{Config.STREAMER_CONSUMER_WAIT_SEC}s; starting anyway.")
                break
        print(f"[DATA SOURCE]   Consumer attached, streaming from sample 0.")

    def get_next_sample(self) -> tuple:
        """
        Push as many pre-derived (eda, hr, hrv) samples as the wall clock
        says should have been published by now. Returns the most recent
        sample pushed (or held if at end of file).

        Why batch-push instead of one sample per call: Windows `time.sleep`
        has ~15 ms granularity, so sleeping 1 ms per sample never delivers
        the 1000 Hz native file rate (the file plays back ~15× too slow
        and the 60 s HRV warm-up never finishes within a 120 s baseline).
        We instead track wall-clock with time.perf_counter() and push
        enough samples each call to keep up with `fs_hz × elapsed_time`.
        """
        now = time.perf_counter()
        if self._stream_start_time is None:
            self._stream_start_time = now

        # End-of-file handling (PDF: stop publishing once the recording
        # ends, so acquisition's deadman fires and the session ends
        # cleanly; honour MOCK_LOOP for endless dev testing).
        if self.current_index >= self.rows:
            if Config.MOCK_LOOP:
                self.current_index = 0
                self._stream_start_time = now  # reset wall-clock baseline
            else:
                if not self._eof_announced:
                    print(f"[DATA SOURCE] MOCK: end of file reached "
                          f"({self.rows} samples). Halting stream — "
                          f"acquisition deadman will fire in "
                          f"{Config.STREAM_TIMEOUT_SEC:.0f}s.")
                    self._eof_announced = True
                time.sleep(0.005)  # don't burn CPU on the post-EOF hold
                # Hold the last sample. Acquisition will see no NEW samples
                # arriving (we stop pushing) and time out.
                last = self.rows - 1
                return (float(self.eda_uS[last]),
                        float(self.hr_series[last]),
                        float(self.hrv_series[last]))

        # How far into the file should we be by now? `target_index` is the
        # sample we'd be at if pacing were perfect. We push everything
        # from `current_index` up to (but not past) `target_index`.
        elapsed = now - self._stream_start_time
        target_index = min(int(elapsed * self.fs_hz), self.rows)

        # Catch up: push the batch. At 1000 Hz with ~15 ms loop granularity
        # on Windows, this typically pushes ~15 samples per call.
        while self.current_index < target_index:
            i = self.current_index
            self.outlet.push_sample([
                float(self.eda_uS[i]),
                float(self.hr_series[i]),     # NaN during HR warm-up
                float(self.hrv_series[i]),    # NaN during HRV warm-up
            ])
            self.ecg_outlet.push_sample([float(self.ecg_mV[i])])
            self.current_index += 1

        # Brief sleep so we yield to the OS between batches without
        # busy-spinning. ~15 ms on Windows in practice (which is exactly
        # the batch size at 1000 Hz file rate, ~15 samples — no drift).
        time.sleep(0.001)

        # Return the most recently published sample for the streamer loop.
        i = max(0, self.current_index - 1)
        return (float(self.eda_uS[i]),
                float(self.hr_series[i]),
                float(self.hrv_series[i]))

    def cleanup(self):
        print("[DATA SOURCE] MOCK: Streaming terminated.")


def _pre_derive_hr_hrv(ecg_mv: np.ndarray, fs_hz: float):
    """Dispatch to the configured backend (same idea as _compute_hr_rmssd).
    Falls back to NeuroKit2 with a warning if bsnb cannot be loaded."""
    global _BSNB_IMPORT_FAILED
    if (getattr(Config, 'HR_HRV_BACKEND', 'neurokit').lower() == 'bsnb'
            and not _BSNB_IMPORT_FAILED):
        try:
            from bsnb_backend import pre_derive_hr_hrv_bsnb
            return pre_derive_hr_hrv_bsnb(ecg_mv, fs_hz)
        except ImportError as e:
            _BSNB_IMPORT_FAILED = True
            print(f"[DATA SOURCE] WARN: bsnb backend failed to import "
                  f"({type(e).__name__}: {e}). Falling back to NeuroKit2 "
                  f"for HR/RMSSD pre-derivation.")
    return _pre_derive_hr_hrv_neurokit(ecg_mv, fs_hz)


def _pre_derive_hr_hrv_neurokit(ecg_mv: np.ndarray, fs_hz: float):
    """
    Offline simulation of the live streaming HR/RMSSD chain.

    Strategy: run `nk.ecg_clean` + `nk.ecg_peaks(correct_artifacts=True)`
    ONCE over the whole file to get a global peak index array, then slice
    that array per window to compute HR / RMSSD at each recompute step.
    Numerically identical to per-window peak detection (the windows are
    independent), but ~10× faster on a 6-minute file because it avoids
    re-running the expensive cleaning + peak-detection per window.

    The simulated cadence matches the live PLUX path exactly:
      * Recompute step  : HR_COMPUTE_INTERVAL_SEC × fs_hz samples
      * HR window       : HR_WINDOW_SEC × fs_hz samples
      * RMSSD window    : RMSSD_WINDOW_SEC × fs_hz samples
      * Between recomputes: zero-order hold

    Per PDF §3, the first ~60 s of HRV are NaN because the 60 s RMSSD
    window hasn't filled yet. HR comes online within a few seconds of
    the first detected R-peak. Both warm-ups are applied identically in
    mock and live paths.

    Inputs
    ------
    ecg_mv : raw ECG voltage in mV (already ADC-converted), shape (N,).
    fs_hz  : sampling rate.

    Returns
    -------
    (hr_series, hrv_series) — both float32 arrays of length N. NaN means
    "not measurable in this window".
    """
    n = len(ecg_mv)
    hr_series = np.full(n, np.nan, dtype=np.float32)
    hrv_series = np.full(n, np.nan, dtype=np.float32)

    if n < int(fs_hz):
        return hr_series, hrv_series

    # ---- ONE full-file pass for cleaning + peak detection ----
    # This is the expensive part. Doing it once instead of per-window
    # cuts startup time from ~30 s to ~3 s on the 367 s file.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            cleaned = nk.ecg_clean(ecg_mv, sampling_rate=fs_hz)
            _, info = nk.ecg_peaks(
                cleaned, sampling_rate=fs_hz, correct_artifacts=True,
            )
        all_peaks = np.asarray(info["ECG_R_Peaks"], dtype=np.int64)
    except Exception as e:
        print(f"[DATA SOURCE] Pre-derivation failed at nk.ecg_peaks ({e}); "
              f"HR/HRV will be NaN throughout.")
        return hr_series, hrv_series

    if len(all_peaks) < 2:
        return hr_series, hrv_series

    hr_window_n = int(Config.HR_WINDOW_SEC * fs_hz)
    hrv_window_n = int(Config.RMSSD_WINDOW_SEC * fs_hz)
    step = max(1, int(Config.HR_COMPUTE_INTERVAL_SEC * fs_hz))

    current_hr = float('nan')
    current_rmssd = float('nan')

    # ---- Walk the file at HR_COMPUTE_INTERVAL_SEC cadence ----
    # At each step, slice the global peaks array to extract peaks in the
    # trailing window, then call the per-window library functions on the
    # slice (nk.ecg_rate / nk.hrv_time accept a peak-index array directly).
    last_end = 0
    for end in range(step, n + 1, step):
        # HR over the trailing HR_WINDOW_SEC of ECG.
        hr_lo = max(0, end - hr_window_n)
        hr_peaks = all_peaks[(all_peaks >= hr_lo) & (all_peaks < end)]
        if len(hr_peaks) >= 2:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    rate = nk.ecg_rate(hr_peaks, sampling_rate=fs_hz)
                hr_val = float(np.nanmean(rate))
                if np.isfinite(hr_val):
                    current_hr = hr_val
            except Exception:
                pass

        # RMSSD over the trailing RMSSD_WINDOW_SEC — only valid once the
        # full window has filled (PDF §3 warm-up). Uses the Malik 20%
        # RR-change gate (vret_server.py): Kubios correction has already
        # repaired peak DETECTION, but Malik filters out interpolated /
        # ectopic INTERVALS that would still inflate a time-domain RMSSD.
        if end >= hrv_window_n:
            hrv_lo = end - hrv_window_n
            hrv_peaks = all_peaks[(all_peaks >= hrv_lo) & (all_peaks < end)]
            if len(hrv_peaks) >= Config.MIN_PEAKS_HRV:
                rmssd = _gated_rmssd_from_peaks(hrv_peaks, fs_hz)
                if np.isfinite(rmssd):
                    current_rmssd = rmssd

        # Fill the per-sample series for this step interval (ZOH).
        hr_series[last_end:end] = current_hr
        hrv_series[last_end:end] = current_rmssd
        last_end = end

    # Fill the tail if `n` isn't a multiple of `step`.
    if last_end < n:
        hr_series[last_end:n] = current_hr
        hrv_series[last_end:n] = current_rmssd

    return hr_series, hrv_series


# =============================================================================
# RealPLUXDataSource — live from the device
# =============================================================================

class RealPLUXDataSource(DataSource):
    """
    Subscribes to PLUX's `OpenSignals` LSL stream, applies the same
    canonical chain MockDataSource uses, and REPUBLISHES the derived
    3-channel stream on `Config.STREAM_NAME` so acquisition.py reads
    exactly the same shape regardless of mock or real mode.

    Two LSL streams are involved:

        Upstream (PLUX → us):  Config.PLUX_LSL_NAME   (raw ADC, 2-3 channels)
        Downstream (us → acq): Config.STREAM_NAME     (derived EDA/HR/HRV)

    Channel mapping is resolved by label (case-insensitive); the PDF-
    corrected fixed order is the fallback. Beat-plausibility (PDF Bug 5)
    is the responsibility of the main pipeline, not this module.

    Per-sample loop
    ---------------
    1. Pull whatever ADC samples have accumulated in the inlet (zero-block).
    2. Convert each raw ADC sample to physical units (EDA µS, ECG mV).
    3. Append the ECG sample to a rolling buffer
       (RMSSD_WINDOW_SEC + a few seconds of margin).
    4. Every HR_COMPUTE_INTERVAL_SEC, recompute HR + RMSSD via the chain
       (same _compute_hr_rmssd used by the mock pre-derivation).
    5. Push the latest (EDA µS, HR BPM, RMSSD ms) tuple on our derived
       outlet, with NaN for any signal not yet measurable.
    6. Push the raw ECG mV on the side stream.

    Bluetooth-dropout watchdog
    --------------------------
    PLUX streams over Bluetooth and drops occasionally. If we keep
    returning held values forever, acquisition's deadman (which reads
    from OUR outlet, not the PLUX inlet) would never trip. We track
    wall-clock time since the last fresh PLUX sample and raise
    ConnectionError after Config.STREAM_TIMEOUT_SEC of silence — matching
    how the acquisition-side deadman fails the session.
    """

    def __init__(self):
        print("[DATA SOURCE] Initializing RealPLUXDataSource (PDF-aligned)...")

        # ---- Connect to PLUX's raw-ADC LSL stream ----
        print(f"[DATA SOURCE]   Scanning for upstream PLUX LSL: "
              f"'{Config.PLUX_LSL_NAME}'...")
        upstream = resolve_stream("name", Config.PLUX_LSL_NAME)
        if not upstream:
            raise RuntimeError(
                f"PLUX LSL stream '{Config.PLUX_LSL_NAME}' not found.\n"
                f"  1. Is the PLUX device powered on and paired?\n"
                f"  2. Is OpenSignals running AND recording (red button)?\n"
                f"  3. Is 'Lab Streaming Layer' enabled in OpenSignals "
                f"prefs, with the stream named '{Config.PLUX_LSL_NAME}'?"
            )

        self.inlet = StreamInlet(upstream[0])
        info = self.inlet.info()
        self.fs_hz = float(info.nominal_srate()) or 1000.0
        n_channels = info.channel_count()

        print(f"[DATA SOURCE]   PLUX: {n_channels} channels @ "
              f"{self.fs_hz:.0f} Hz")

        # ----------------------------------------------------------------
        # Channel resolution: labels first, then content fingerprint.
        # The earlier real_plux session had EDA pinned to ~0 because the
        # fixed-index fallback picked the DI line (always zero). Even
        # with labels, OpenSignals builds disagree on naming, so we now
        # ALWAYS verify the choice against the live data.
        # ----------------------------------------------------------------
        labels = _read_lsl_channel_labels(self.inlet)
        label_eda, label_ecg, label_source = _resolve_channels_by_label(
            labels, n_channels,
        )
        used_content = False
        try:
            # Content-based detect sees the actual samples — it cannot be
            # fooled by missing or mis-applied labels.
            c_eda, c_ecg, c_source = _resolve_channels_by_content(
                self.inlet, n_channels, self.fs_hz, sample_sec=5.0,
            )
            if label_source == "labels" and (c_eda != label_eda
                                              or c_ecg != label_ecg):
                # Labels said one thing, data says another. Trust data.
                print(f"[DATA SOURCE]   WARN: label-based mapping "
                      f"(EDA=ch{label_eda}, ECG=ch{label_ecg}) disagrees "
                      f"with content-based (EDA=ch{c_eda}, ECG=ch{c_ecg}). "
                      f"Using content-based to be safe.")
            self.eda_ch, self.ecg_ch = c_eda, c_ecg
            source = c_source
            used_content = True
        except RuntimeError as e:
            # Content detection refused: either no plausible ECG or no
            # plausible EDA was found. Fall back to whatever the labels /
            # fixed-indices said, but make the operator notice loudly.
            print(f"[DATA SOURCE]   WARN: {e}")
            print(f"[DATA SOURCE]   WARN: falling back to "
                  f"label/fixed-index mapping. Verify EDA values look "
                  f"physiological (1-20 µS range) once baseline starts.")
            self.eda_ch, self.ecg_ch = label_eda, label_ecg
            source = label_source

        # ----------------------------------------------------------------
        # Force-print channel mapping in a banner so it cannot be missed,
        # then post-validate by sampling a short window and confirming
        # EDA looks physiological (1-30 µS at rest). If EDA stays under
        # 0.1 µS for >2 s the operator almost certainly has either a
        # disconnected EDA electrode OR a mis-mapped channel (real-1
        # session bug). In that case we ABORT before letting the session
        # start with garbage data being recorded.
        # ----------------------------------------------------------------
        eda_min_valid_uS = 0.1     # below this = no skin contact OR wrong channel
        verify_sec = 3.0
        try:
            verify_chunk = []
            deadline = time.time() + verify_sec
            while time.time() < deadline:
                chunk, _ = self.inlet.pull_chunk(timeout=0.1,
                                                  max_samples=int(self.fs_hz))
                if chunk:
                    verify_chunk.extend(chunk)
            if not verify_chunk:
                raise RuntimeError(
                    f"channel-mapping verify: no samples arrived in "
                    f"{verify_sec:.0f}s. Is OpenSignals still recording?"
                )
            v_arr = np.asarray(verify_chunk, dtype=float)
            eda_samples = v_arr[:, self.eda_ch]
            ecg_samples = v_arr[:, self.ecg_ch]
            # When LSL is pre-converted, the samples are already in
            # physical units -- skip the calibration formulas.
            if getattr(Config, 'LSL_VALUES_PRECONVERTED', False):
                eda_uS_arr = eda_samples
                ecg_mV_arr = ecg_samples
            else:
                eda_uS_arr = adc_to_eda_uS(eda_samples)
                ecg_mV_arr = adc_to_ecg_mV(ecg_samples)
            eda_uS_mean = float(np.mean(eda_uS_arr))
            ecg_mV_p2p = float(np.max(ecg_mV_arr) - np.min(ecg_mV_arr))

            print()
            print("[DATA SOURCE]   " + "=" * 60)
            print(f"[DATA SOURCE]   CHANNEL MAPPING (verify against OpenSignals UI)")
            print(f"[DATA SOURCE]   " + "-" * 60)
            print(f"[DATA SOURCE]   source      : {source}")
            print(f"[DATA SOURCE]   labels      : {labels or 'none'}")
            print(f"[DATA SOURCE]   n_channels  : {n_channels}")
            print(f"[DATA SOURCE]   EDA -> ch{self.eda_ch}"
                  f"  raw ADC mean={float(np.mean(eda_samples)):.0f}"
                  f"  -> {eda_uS_mean:.3f} uS")
            print(f"[DATA SOURCE]   ECG -> ch{self.ecg_ch}"
                  f"  raw ADC mean={float(np.mean(ecg_samples)):.0f}"
                  f"  -> peak-to-peak {ecg_mV_p2p:.3f} mV")
            print("[DATA SOURCE]   " + "=" * 60)
            print()

            if eda_uS_mean < eda_min_valid_uS:
                # Informational note (NOT an error). The pipeline proceeds
                # normally -- ECG / HR / HRV stream as usual, EDA chart
                # reads near zero until skin contact improves. Operator
                # request 2026-06: this is a normal startup state, treat
                # it as a quiet status rather than a panic banner.
                print(f"[DATA SOURCE]   NOTE: EDA reading is small "
                      f"({eda_uS_mean:.6f} uS, threshold "
                      f"{eda_min_valid_uS:.2f} uS) -- normal if finger pads "
                      f"are still warming up. Proceeding with the session.")
        except Exception as e:
            # Verify itself crashed (LSL hiccup, decode error). Don't abort —
            # the operator can still run the session and catch problems
            # by eye. Log loudly so it doesn't go unnoticed.
            print(f"[DATA SOURCE]   WARN: channel-mapping verify skipped "
                  f"({type(e).__name__}: {e})")
            print(f"[DATA SOURCE]   Channel mapping ({source}): "
                  f"EDA=ch{self.eda_ch}, ECG=ch{self.ecg_ch} "
                  f"(labels: {labels or 'none'})")

        # ---- Open our downstream "derived" outlet ----
        # Same shape and channel order as MockDataSource so acquisition.py
        # doesn't care whether it's live or replay. NaN flows through for
        # the HR/HRV warm-up period.
        self.out_info = StreamInfo(
            name=Config.STREAM_NAME,
            type=Config.STREAM_TYPE,
            channel_count=3,
            nominal_srate=self.fs_hz,
            channel_format='float32',
            source_id='plux_hardware_derived',
        )
        self.outlet = StreamOutlet(self.out_info)

        self.ecg_out_info = StreamInfo(
            name=Config.ECG_STREAM_NAME,
            type=Config.ECG_STREAM_TYPE,
            channel_count=1,
            nominal_srate=self.fs_hz,
            channel_format='float32',
            source_id='plux_hardware_ecg',
        )
        self.ecg_outlet = StreamOutlet(self.ecg_out_info)
        # Echo the live channel indices once now that both outlets exist,
        # so a quick scan of the streamer console confirms which physical
        # channel is being broadcast as ECG / EDA. Useful when the
        # dashboard chart disagrees with OpenSignals -- the most common
        # culprit is a mis-mapped self.ecg_ch / self.eda_ch.
        print(f"[DATA SOURCE]   LIVE CHANNELS: "
              f"EDA=ch{self.eda_ch}  ECG=ch{self.ecg_ch}  "
              f"(both outlets are now publishing).")

        # ---- Rolling buffer for ECG (60 s + 5 s margin = full RMSSD window) ----
        buffer_len = int((Config.RMSSD_WINDOW_SEC + 5) * self.fs_hz)
        self._ecg_buffer = collections.deque(maxlen=buffer_len)
        self._sample_index = 0  # absolute count since session start
        self._recompute_step = max(1, int(Config.HR_COMPUTE_INTERVAL_SEC * self.fs_hz))

        # Latest values held between recomputes (ZOH). NaN until first
        # successful compute (HR within ~3 s, RMSSD after 60 s).
        self.latest_eda = 0.0
        self.latest_hr = float('nan')
        self.latest_rmssd = float('nan')

        # Bluetooth-dropout watchdog state.
        self._last_fresh_sample_time = time.time()
        self._watchdog_warned = False

        # Wait for acquisition consumer before publishing (reproducibility).
        print(f"[DATA SOURCE]   Outlet ready on LSL '{Config.STREAM_NAME}' "
              f"({self.fs_hz:.0f} Hz).")
        print("[DATA SOURCE]   Waiting for acquisition consumer...")
        t_start = time.time()
        while not self.outlet.have_consumers():
            time.sleep(0.05)
            if time.time() - t_start > Config.STREAMER_CONSUMER_WAIT_SEC:
                print(f"[DATA SOURCE]   No consumer after "
                      f"{Config.STREAMER_CONSUMER_WAIT_SEC}s; starting anyway.")
                break
        print("[DATA SOURCE]   Consumer attached, beginning stream.")

    def get_next_sample(self) -> tuple:
        """
        Pull whatever new ADC samples are available from PLUX, process them,
        and push the latest derived tuple to the downstream outlet.
        """
        samples, _ = self.inlet.pull_chunk(timeout=0.0)

        # ---- Bluetooth watchdog ----
        if not samples:
            silent_for = time.time() - self._last_fresh_sample_time
            if (silent_for > Config.REAL_PLUX_WATCHDOG_WARN_SEC
                    and not self._watchdog_warned):
                print(f"[DATA SOURCE] WARN: no PLUX samples for "
                      f"{silent_for:.1f}s — Bluetooth dropout likely. "
                      f"Holding last value; will raise at "
                      f"{Config.STREAM_TIMEOUT_SEC}s.")
                self._watchdog_warned = True
            if silent_for > Config.STREAM_TIMEOUT_SEC:
                raise ConnectionError(
                    f"[DATA SOURCE] PLUX inlet silent for {silent_for:.1f}s "
                    f"(threshold {Config.STREAM_TIMEOUT_SEC}s). Check that "
                    f"OpenSignals is still recording and the device is paired."
                )
            # No new data — republish the last derived sample so the
            # downstream stream stays alive at roughly the same rate.
            self.outlet.push_sample(
                [self.latest_eda, self.latest_hr, self.latest_rmssd]
            )
            return self.latest_eda, self.latest_hr, self.latest_rmssd

        self._last_fresh_sample_time = time.time()
        self._watchdog_warned = False

        # ---- Per-sample processing for everything in the chunk ----
        # Per Config.LSL_VALUES_PRECONVERTED (default True): OpenSignals
        # LSL publishes physical units directly (uS / mV), so we MUST NOT
        # apply our adc_to_eda_uS / adc_to_ecg_mV calibration formulas --
        # doing so would double-convert and destroy the value. When
        # False (raw-ADC mode), apply the formulas as before.
        _preconverted = getattr(Config, 'LSL_VALUES_PRECONVERTED', False)
        for s in samples:
            if _preconverted:
                eda_uS = float(s[self.eda_ch])  # already uS
                ecg_mV = float(s[self.ecg_ch])  # already mV
            else:
                eda_uS = adc_to_eda_uS(s[self.eda_ch])
                ecg_mV = adc_to_ecg_mV(s[self.ecg_ch])

            self.latest_eda = float(eda_uS)
            self._ecg_buffer.append(float(ecg_mV))
            self._sample_index += 1

            # Push raw ECG to side outlet (every sample, for dashboard).
            self.ecg_outlet.push_sample([float(ecg_mV)])

            # DIAGNOSTIC (operator request 2026-06). Once per second print
            # the raw ADC and converted physical value for both channels,
            # plus the latest HR/RMSSD, so the operator can verify the
            # dashboard chart matches what's actually being pushed.
            # Precision is intentionally HIGH (6 decimal places on EDA,
            # 6 on ECG mV) so the log preserves small values for audit.
            if self._sample_index % int(self.fs_hz) == 0:
                # When pre-converted, the raw LSL value IS the physical
                # unit -- the "ADC=" column is the LSL value itself, the
                # arrow column repeats it (no conversion applied).
                raw_eda_str = f"{float(s[self.eda_ch]):.4f}"
                raw_ecg_str = f"{float(s[self.ecg_ch]):.4f}"
                unit_tag = "(pre-conv)" if _preconverted else "(raw ADC)"
                print(f"[DATA SOURCE] ECG push {unit_tag}: "
                      f"LSL ecg={raw_ecg_str}  "
                      f"-> {ecg_mV:+.6f} mV   "
                      f"(EDA: LSL eda={raw_eda_str}  "
                      f"-> {eda_uS:.6f} uS,  "
                      f"latest_hr={self.latest_hr if np.isfinite(self.latest_hr) else 'NaN'},  "
                      f"latest_rmssd={self.latest_rmssd if np.isfinite(self.latest_rmssd) else 'NaN'})")

            # Periodic HR/RMSSD recompute.
            if self._sample_index % self._recompute_step == 0:
                self._recompute_hr_hrv()

            # Push derived 3-channel sample on every input sample so the
            # downstream stream is dense (acquisition then drains it to
            # the latest tick at 50 Hz).
            self.outlet.push_sample(
                [self.latest_eda, self.latest_hr, self.latest_rmssd]
            )

        return self.latest_eda, self.latest_hr, self.latest_rmssd

    def _recompute_hr_hrv(self):
        """Run the canonical chain on the rolling ECG buffer."""
        if len(self._ecg_buffer) < int(self.fs_hz):
            return  # need at least 1 s of ECG
        arr = np.asarray(self._ecg_buffer, dtype=float)

        # HR window: up to HR_WINDOW_SEC trailing samples.
        hr_window_n = int(Config.HR_WINDOW_SEC * self.fs_hz)
        hr_slice = arr[-hr_window_n:] if len(arr) > hr_window_n else arr

        # RMSSD window: needs the full RMSSD_WINDOW_SEC.
        hrv_window_n = int(Config.RMSSD_WINDOW_SEC * self.fs_hz)
        have_full_hrv = len(arr) >= hrv_window_n
        hrv_slice = arr[-hrv_window_n:] if have_full_hrv else arr

        hr_val, _ = _compute_hr_rmssd(hr_slice, self.fs_hz,
                                       have_full_hrv_window=False)
        if np.isfinite(hr_val):
            self.latest_hr = hr_val

        if have_full_hrv:
            _, rmssd_val = _compute_hr_rmssd(hrv_slice, self.fs_hz,
                                              have_full_hrv_window=True)
            if np.isfinite(rmssd_val):
                self.latest_rmssd = rmssd_val

    def cleanup(self):
        print("[DATA SOURCE] REAL: Hardware connection closed.")


# =============================================================================
# Factory
# =============================================================================

class DataSourceFactory:
    """
    Picks the data source from Config.DATA_SOURCE. Two valid options:

        'mock'       → MockDataSource (replays Config.MOCK_DATA_FILE)
        'real_plux'  → RealPLUXDataSource (live from PLUX via OpenSignals LSL)

    The previous 'mock2' / 'real_plux2' variants are gone — both code paths
    now use the same canonical NeuroKit2 chain from PDF §2, so there is no
    A/B to maintain.
    """

    @staticmethod
    def create() -> DataSource:
        choice = Config.DATA_SOURCE.lower()
        if choice == 'mock':
            return MockDataSource()
        if choice == 'real_plux':
            return RealPLUXDataSource()
        # Soft-handle the now-removed v2 variants so old configs don't
        # silently fall through to a ValueError without explanation.
        if choice in ('mock2', 'real_plux2'):
            raise ValueError(
                f"Config.DATA_SOURCE = '{Config.DATA_SOURCE}' is no longer "
                f"valid. The v1/v2 split was removed; both modes now use "
                f"the same canonical NeuroKit2 chain. Use '{choice[:-1]}' "
                f"instead (i.e. drop the trailing '2')."
            )
        raise ValueError(
            f"Invalid Config.DATA_SOURCE: '{Config.DATA_SOURCE}'. "
            f"Valid options: 'mock', 'real_plux'."
        )


# =============================================================================
# Standalone smoke test
# =============================================================================

if __name__ == "__main__":
    print("\n=== data_sources.py standalone smoke test ===\n")
    try:
        src = DataSourceFactory.create()
        eda, hr, hrv = src.get_next_sample()
        print(f"\nFirst sample: EDA={eda:.3f} µS  "
              f"HR={'NaN' if not np.isfinite(hr) else f'{hr:.1f} BPM'}  "
              f"HRV={'NaN (warm-up)' if not np.isfinite(hrv) else f'{hrv:.1f} ms'}")
        src.cleanup()
        print("\n✓ Data source created and produced one sample.")
    except Exception as e:
        print(f"\n✗ {type(e).__name__}: {e}")
