"""
compare_backends.py
===================
CLI version of compare_backends.ipynb. Runs both backends on a given
OpenSignals .txt file, prints agreement metrics, and saves comparison
PNGs alongside the input file.

Use this if you want to validate the comparison without launching
Jupyter -- the numbers should match what the notebook produces cell
for cell.

Usage:
    python compare_backends.py <path_to_opensignals.txt>
"""

from __future__ import annotations
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd
import matplotlib
# Force a non-interactive backend BEFORE pyplot is imported. Otherwise
# the bsnb package's IPython dependency steers matplotlib to an
# interactive backend, which crashes outside a real IPython kernel.
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# Make src/ importable
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from data_sources import (parse_opensignals_header,
                          adc_to_eda_uS, adc_to_ecg_mV,
                          _pre_derive_hr_hrv_neurokit)
from bsnb_backend import pre_derive_hr_hrv_bsnb, decompose_eda_bsnb

import neurokit2 as nk
warnings.filterwarnings('ignore')


def _stats(s, label):
    valid = np.isfinite(s)
    if valid.sum() == 0:
        return f'{label:10s}  no valid samples'
    return (f'{label:10s}  valid {valid.sum()}/{len(s)}  '
            f'mean = {np.nanmean(s):6.2f}  std = {np.nanstd(s):5.2f}')


def agreement(a, b, label):
    mask = np.isfinite(a) & np.isfinite(b)
    if mask.sum() < 10:
        print(f'{label:10s}  too few valid samples ({mask.sum()})')
        return
    aa, bb = a[mask], b[mask]
    r = float(np.corrcoef(aa, bb)[0, 1]) if aa.std() > 0 and bb.std() > 0 else float('nan')
    rmse = float(np.sqrt(np.mean((aa - bb) ** 2)))
    bias = float(np.mean(aa - bb))
    print(f'{label:10s}  n={mask.sum():>6d}  r={r:+.3f}  '
          f'RMSE={rmse:7.3f}  bias(nk-bsnb)={bias:+7.3f}')


def moldoveanu_stress(eda, hr, hrv, fs, baseline_sec=120.0):
    """Prof's DataAnalyze.py math: percent-change deltas, weights
    0.5 / 0.3 / 0.2 (EDA / HRV / HR), 1-second rolling smoothing."""
    bn = int(baseline_sec * fs)
    bmask = np.isfinite(hr) & np.isfinite(hrv)
    bmask[bn:] = False
    avg_eda = float(np.nanmean(eda[bmask])) if bmask.any() else float(np.nanmean(eda))
    avg_hr  = float(np.nanmean(hr[bmask]))  if bmask.any() else float(np.nanmean(hr))
    avg_hrv = float(np.nanmean(hrv[bmask])) if bmask.any() else float(np.nanmean(hrv))
    norm_eda = (eda - avg_eda) / max(avg_eda, 1e-9) * 100
    norm_hrv = (avg_hrv - hrv) / max(avg_hrv, 1e-9) * 100   # inverted
    norm_hr  = (hr - avg_hr)   / max(avg_hr,  1e-9) * 100
    s_inst = 0.5 * norm_eda + 0.3 * norm_hrv + 0.2 * norm_hr
    w = max(1, int(fs))
    s_t = pd.Series(s_inst).rolling(w, min_periods=1).mean().to_numpy()
    return s_t, (avg_eda, avg_hr, avg_hrv)


def main(file_path: str) -> int:
    if not os.path.isfile(file_path):
        print(f'ERROR: file not found: {file_path}')
        return 1

    print(f'[Load] {file_path}')
    header = parse_opensignals_header(file_path)
    fs = header['fs_hz']
    col_eda = header['column_index']['EDA']
    col_ecg = header['column_index']['ECG']
    n_cols = len(header['columns'])
    data = np.genfromtxt(file_path, skip_header=3,
                         usecols=tuple(range(n_cols)),
                         invalid_raise=False)
    data = data[np.all(np.isfinite(data), axis=1)]
    eda_uS = adc_to_eda_uS(data[:, col_eda])
    ecg_mV = adc_to_ecg_mV(data[:, col_ecg])
    n = len(eda_uS)
    t = np.arange(n) / fs

    print(f'  fs       = {fs:.0f} Hz')
    print(f'  duration = {t[-1]:.1f} s ({n} samples)')
    print(f'  EDA col  = {col_eda}  mean = {eda_uS.mean():.2f} uS')
    print(f'  ECG col  = {col_ecg}  p-p = {ecg_mV.max() - ecg_mV.min():.3f} mV')

    print()
    print('[1/3] NeuroKit2 backend...')
    t0 = time.time()
    hr_nk, hrv_nk = _pre_derive_hr_hrv_neurokit(ecg_mV, fs)
    print(f'  {time.time() - t0:.1f} s')

    print('[2/3] biosignalsnotebooks backend...')
    t0 = time.time()
    hr_bs, hrv_bs = pre_derive_hr_hrv_bsnb(ecg_mV, fs)
    print(f'  {time.time() - t0:.1f} s')

    print('[3/3] EDA decomposition (both)...')
    # NeuroKit2 EDA via cvxEDA
    ds_rate = 10
    ds = nk.signal_resample(eda_uS, sampling_rate=fs,
                             desired_sampling_rate=ds_rate)
    ds = nk.eda_clean(ds, sampling_rate=ds_rate)
    decomp = nk.eda_phasic(ds, sampling_rate=ds_rate)
    phasic_nk_ds = decomp['EDA_Phasic'].values
    tonic_nk_ds  = decomp['EDA_Tonic'].values
    phasic_nk = np.interp(t, np.arange(len(phasic_nk_ds)) / ds_rate, phasic_nk_ds)
    tonic_nk  = np.interp(t, np.arange(len(tonic_nk_ds))  / ds_rate, tonic_nk_ds)
    # biosignalsnotebooks EDA (lowpass-subtract)
    tonic_bs, phasic_bs = decompose_eda_bsnb(eda_uS, fs)

    print()
    print('== Per-backend signal stats ==')
    for s, lab in ((hr_nk, 'HR (nk)'), (hr_bs, 'HR (bsnb)'),
                    (hrv_nk, 'HRV (nk)'), (hrv_bs, 'HRV (bsnb)'),
                    (phasic_nk, 'phEDA (nk)'), (phasic_bs, 'phEDA (bsnb)')):
        print(' ', _stats(s, lab))

    print()
    print('== Agreement between backends ==')
    agreement(hr_nk,     hr_bs,     'HR')
    agreement(hrv_nk,    hrv_bs,    'RMSSD')
    agreement(phasic_nk, phasic_bs, 'phasic-EDA')
    agreement(tonic_nk,  tonic_bs,  'tonic-EDA')

    print()
    print('== Stress-index comparison (Moldoveanu fusion on both) ==')
    s_nk, base_nk = moldoveanu_stress(eda_uS, hr_nk, hrv_nk, fs)
    s_bs, base_bs = moldoveanu_stress(eda_uS, hr_bs, hrv_bs, fs)
    print(f'  NeuroKit baseline (EDA, HR, HRV): '
          f'{tuple(round(x, 2) for x in base_nk)}')
    print(f'  BSNB     baseline (EDA, HR, HRV): '
          f'{tuple(round(x, 2) for x in base_bs)}')
    agreement(s_nk, s_bs, 'stress')

    # ---- Save plots ----
    out_png_a = file_path + '.compare_signals.png'
    out_png_b = file_path + '.compare_stress.png'

    fig, axs = plt.subplots(4, 1, figsize=(14, 14), sharex=True)
    axs[0].plot(t, ecg_mV, color='#888', linewidth=0.4)
    axs[0].set_ylabel('ECG (mV)')
    axs[0].set_title('Raw ECG')
    axs[0].grid(alpha=0.3)

    axs[1].plot(t, hr_nk, color='#1f77b4', linewidth=1.5, label='NeuroKit2 HR')
    axs[1].plot(t, hr_bs, color='#d62728', linewidth=1.5, linestyle='--', label='BSNB HR')
    axs[1].set_ylabel('HR (BPM)'); axs[1].legend(); axs[1].grid(alpha=0.3)

    axs[2].plot(t, hrv_nk, color='#1f77b4', linewidth=1.5, label='NeuroKit2 RMSSD')
    axs[2].plot(t, hrv_bs, color='#d62728', linewidth=1.5, linestyle='--', label='BSNB RMSSD')
    axs[2].set_ylabel('RMSSD (ms)'); axs[2].legend(); axs[2].grid(alpha=0.3)

    axs[3].plot(t, eda_uS,   color='#888', linewidth=0.6, label='Raw EDA')
    axs[3].plot(t, tonic_nk, color='#1f77b4', linewidth=1.5, label='NeuroKit2 tonic')
    axs[3].plot(t, tonic_bs, color='#d62728', linewidth=1.5, linestyle='--', label='BSNB tonic')
    axs[3].set_ylabel('EDA (uS)'); axs[3].set_xlabel('Time (s)')
    axs[3].legend(); axs[3].grid(alpha=0.3)

    plt.tight_layout(); plt.savefig(out_png_a, dpi=120); plt.close()
    print(f'\n[Plot] Saved: {out_png_a}')

    fig, ax = plt.subplots(1, 1, figsize=(14, 4.5))
    ax.plot(t, s_nk, color='#1f77b4', linewidth=1.2, label='Stress (NeuroKit2 -> Moldoveanu)')
    ax.plot(t, s_bs, color='#d62728', linewidth=1.2, linestyle='--',
            label='Stress (BSNB -> Moldoveanu)')
    ax.axhline(0, color='#444', linewidth=0.6)
    ax.set_xlabel('Time (s)'); ax.set_ylabel('Stress index')
    ax.set_title('Stress index -- Moldoveanu fusion driven by both backends')
    ax.legend(); ax.grid(alpha=0.3)
    plt.tight_layout(); plt.savefig(out_png_b, dpi=120); plt.close()
    print(f'[Plot] Saved: {out_png_b}')
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('usage: python compare_backends.py <path_to_opensignals.txt>')
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
