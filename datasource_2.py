"""
datasource_2.py
================
Alternative stress-index analysis using the professor's reference math
(Moldoveanu et al., 2023 -- the approach behind `DataAnalyze.py` and the
prof-supplied `BiofeedbackSessions/` CSVs).

This is a POST-HOC ANALYSIS SCRIPT. It does NOT touch the live pipeline
(src/main.py, src/fusion.py etc.). It reads a finished session CSV, applies
the prof's math from scratch, prints every intermediate value the user is
expected to audit, and saves a side-by-side plot of stress vs balloon
height for visual comparison.

Two input formats are auto-detected:

  1. Professor's session CSV (data/BiofeedbackSessions/P001_*.csv) --
     identified by the presence of `baselineStatus`, `balloonState`,
     `rawHR`, `rawEDA`, `rawHRV`, `balloonHeight` columns.

  2. This pipeline's `samples.csv` (data/<session>/samples.csv) --
     identified by the presence of `phase`, `eda`, `hr`, `hrv`,
     `height_m` columns. The `phase` column drives baseline vs live
     segmentation (BASELINE rows are the baseline window; LIVE rows are
     the exposure window).

The math, identical to DataAnalyze.py:

    baseline window  = rows where the participant was at rest
    live window      = rows where the balloon was active (post-baseline)

    avgEDA, avgHR, avgHRV  =  mean over the baseline window

    normEDA  =  (live_EDA - avgEDA)  / avgEDA  * 100        # ↑ = stress
    normHRV  =  (avgHRV   - live_HRV) / avgHRV  * 100        # ↓ = stress  (INVERTED)
    normHR   =  (live_HR  - avgHR)   / avgHR   * 100         # ↑ = stress

    S_instant = 0.5 * normEDA + 0.3 * normHRV + 0.2 * normHR     # Moldoveanu weights
    S_t       = S_instant.rolling(window=SMOOTH_N).mean()        # ~1 s smoothing

    stdStress = S_t.std()
    mild  =  1.33 * stdStress     # ~= 90th percentile of a normal distribution
    high  =  2.28 * stdStress     # ~= 99th percentile

    correlation = pearson(S_t, balloonHeight)

Usage
-----
    python datasource_2.py <path_to_csv>

Example
-------
    python datasource_2.py data/BiofeedbackSessions/P001_easy_relax_20260330_141544.csv
    python datasource_2.py data/shayan_fortheenmin_Session1_2026-06-03_M/samples.csv

Output
------
  * Console: averages, std, thresholds, percent-time-in-each-zone,
             correlation, sample counts. Every intermediate the user
             is expected to audit is printed.
  * PNG: <input>.datasource_2.png with two stacked panels (stress, height).
  * CSV: <input>.datasource_2.csv with per-sample (timestamp, normEDA,
         normHRV, normHR, s_instant, s_t, height) so the trace can be
         compared row-by-row against the existing pipeline output.
"""

from __future__ import annotations

import os
import sys
import math

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


# ---------------------------------------------------------------------------
# Tunables. Kept identical to the prof's script unless noted.
# ---------------------------------------------------------------------------

# Rolling-mean window in SAMPLES (not seconds). DataAnalyze.py used 50,
# which is ~1 s at 50 Hz logging. The user's samples.csv is written at
# 1 Hz (`SAMPLES_CSV_RATE_HZ` in session_manager.py), so for that schema
# a window of 50 would smooth over 50 seconds. We auto-pick at load time
# based on the detected sampling rate; this constant is the FALLBACK.
SMOOTH_WINDOW_SAMPLES_FALLBACK = 50

# Threshold multipliers on stdStress (Moldoveanu 2023 / prof's script).
THRESH_MILD_SD = 1.33      # ~= 90.8th percentile of a normal distribution
THRESH_HIGH_SD = 2.28      # ~= 98.9th percentile


# ---------------------------------------------------------------------------
# File-format detection + load
# ---------------------------------------------------------------------------

def _read_csv_skipping_comments(file_path: str) -> pd.DataFrame:
    """The prof's CSVs start with one or two `#` comment lines.
    `comment='#'` makes pandas skip them. Same behaviour as DataAnalyze.py."""
    return pd.read_csv(file_path, comment='#')


def detect_schema(df: pd.DataFrame) -> str:
    """Return 'prof' or 'user' or raise on unknown shape."""
    cols = set(df.columns)
    prof_required = {'baselineStatus', 'balloonState', 'rawHR', 'rawEDA',
                     'rawHRV', 'balloonHeight', 'timestamp'}
    user_required = {'phase', 'eda', 'hr', 'hrv', 'sample_n'}
    if prof_required.issubset(cols):
        return 'prof'
    if user_required.issubset(cols):
        return 'user'
    missing_prof = prof_required - cols
    missing_user = user_required - cols
    raise SystemExit(
        f"Unknown CSV schema.\n"
        f"  Missing for prof format : {sorted(missing_prof)}\n"
        f"  Missing for user format : {sorted(missing_user)}\n"
        f"  Got columns             : {sorted(cols)}"
    )


def estimate_sampling_rate_hz(timestamps: pd.Series) -> float:
    """Best-effort sample-rate guess. Uses median dt between consecutive
    rows; robust to occasional gaps. Returns 1.0 Hz if it can't tell."""
    ts = pd.to_numeric(timestamps, errors='coerce').dropna().to_numpy()
    if ts.size < 3:
        return 1.0
    dt = np.diff(ts)
    dt = dt[dt > 0]
    if dt.size == 0:
        return 1.0
    median_dt = float(np.median(dt))
    if median_dt <= 0:
        return 1.0
    return 1.0 / median_dt


# ---------------------------------------------------------------------------
# Schema-specific extraction. Each returns a normalised dict:
#   { 'baseline_df': DataFrame, 'live_df': DataFrame,
#     'time_col': name, 'fs_hz': float }
# Both DataFrames have at minimum columns: time, eda, hr, hrv, height.
# ---------------------------------------------------------------------------

def extract_prof(df: pd.DataFrame) -> dict:
    """Filter rows exactly as DataAnalyze.py does:
        baseline = eventType=='data' & baselineStatus=='collecting'
        live     = eventType=='data' & baselineStatus=='complete'
                   & balloonState != 'waiting'
    """
    base = df.query("eventType == 'data' and baselineStatus == 'collecting'").copy()
    live = df.query("eventType == 'data' and baselineStatus == 'complete' "
                    "and balloonState != 'waiting'").copy()

    # The prof's CSV carries BOTH smoothed columns (`eda`, `hrv`) and RAW
    # columns (`rawEDA`, `rawHRV`, `rawHR`). DataAnalyze.py uses the RAW
    # ones; we drop the smoothed ones first so the rename below doesn't
    # collide and produce duplicate column names.
    drop_smoothed = [c for c in ('eda', 'hrv', 'heartRate') if c in base.columns]
    base = base.drop(columns=drop_smoothed)
    live = live.drop(columns=drop_smoothed)

    rename = {'rawEDA': 'eda', 'rawHR': 'hr', 'rawHRV': 'hrv',
              'balloonHeight': 'height', 'timestamp': 'time'}
    base = base.rename(columns=rename)[['time', 'eda', 'hr', 'hrv', 'height']]
    live = live.rename(columns=rename)[['time', 'eda', 'hr', 'hrv', 'height']]

    fs = estimate_sampling_rate_hz(live['time'] if len(live) else base['time'])
    return {'baseline_df': base, 'live_df': live, 'fs_hz': fs}


def extract_user(df: pd.DataFrame) -> dict:
    """Drive segmentation from the `phase` column written by SessionManager.
    BASELINE rows go to baseline_df, LIVE rows to live_df. `height_m` is
    the per-tick Unity altitude (NaN until Unity starts streaming back)."""
    # sample_n is a 1-indexed counter; convert to seconds via fs_hz estimate
    # so plots and the rolling window length make sense.
    base = df[df['phase'] == 'BASELINE'].copy()
    live = df[df['phase'] == 'LIVE'].copy()

    # Construct a synthetic time column. samples.csv is written at 1 Hz
    # decimation by default, so sample_n step ~= 1 second. We expose
    # both `sample_n` (raw) and `time` (seconds) so downstream code can
    # use either.
    if 'sample_n' in df.columns:
        # Approximate seconds = sample_n / fs_hz. Estimate fs from the
        # spacing of sample_n in BASELINE.
        if len(base) >= 3:
            dt = float(np.median(np.diff(base['sample_n'].to_numpy())))
            fs = 1.0 / dt if dt > 0 else 1.0
        else:
            fs = 1.0
    else:
        fs = 1.0

    rename = {'height_m': 'height'}
    base = base.rename(columns=rename)
    live = live.rename(columns=rename)
    # Make sure all the expected columns exist
    for col in ('eda', 'hr', 'hrv', 'height'):
        if col not in base.columns:
            base[col] = float('nan')
        if col not in live.columns:
            live[col] = float('nan')

    base['time'] = base['sample_n'].astype(float) / fs
    live['time'] = live['sample_n'].astype(float) / fs
    base = base[['time', 'eda', 'hr', 'hrv', 'height']]
    live = live[['time', 'eda', 'hr', 'hrv', 'height']]
    return {'baseline_df': base, 'live_df': live, 'fs_hz': fs}


# ---------------------------------------------------------------------------
# Math -- mirrors DataAnalyze.py step-by-step.
# Anything that deviates from the prof's script is called out in a comment.
# ---------------------------------------------------------------------------

def compute_baseline_averages(baseline_df: pd.DataFrame) -> dict:
    """Plain mean of each signal over the baseline window. No 3-sigma
    cleaning, no outlier removal -- matches DataAnalyze.py exactly."""
    # `.mean()` skips NaN by default; the prof's data has fully-populated
    # baseline rows so this is equivalent to his `.mean()`.
    avg = baseline_df[['eda', 'hr', 'hrv']].mean()
    return {'eda': float(avg['eda']),
            'hr':  float(avg['hr']),
            'hrv': float(avg['hrv'])}


def compute_normalised_deltas(live_df: pd.DataFrame, avgs: dict) -> pd.DataFrame:
    """Percent deviation from baseline, sign-aligned so positive = more stress.
    The HRV expression is INVERTED ((base - live)/base) because parasympathetic
    drop = lower HRV = more stress."""
    eda_base = avgs['eda'] if avgs['eda'] not in (0, None) else 1e-9
    hr_base  = avgs['hr']  if avgs['hr']  not in (0, None) else 1e-9
    hrv_base = avgs['hrv'] if avgs['hrv'] not in (0, None) else 1e-9

    norm = pd.DataFrame({
        'time':    live_df['time'].to_numpy(),
        'height':  live_df['height'].to_numpy(),
        'normEDA': (live_df['eda'] - eda_base) / eda_base * 100.0,
        'normHRV': (hrv_base - live_df['hrv']) / hrv_base * 100.0,   # inverted
        'normHR':  (live_df['hr']  - hr_base)  / hr_base  * 100.0,
    })
    return norm


def compute_stress_index(norm: pd.DataFrame, smooth_window: int) -> pd.DataFrame:
    """Weighted composite + rolling-mean smoothing. Weights are 0.5/0.3/0.2
    as in DataAnalyze.py (EDA-heavy, the Moldoveanu 2023 weighting)."""
    s_instant = (0.5 * norm['normEDA']
                 + 0.3 * norm['normHRV']
                 + 0.2 * norm['normHR'])
    # min_periods=1 ensures the first samples still produce a value
    # (DataAnalyze.py uses the default which yields NaN until the window
    # fills; we match that default for fidelity).
    s_t = s_instant.rolling(window=smooth_window).mean()
    out = norm.copy()
    out['s_instant'] = s_instant
    out['s_t'] = s_t
    return out


def compute_thresholds(s_t: pd.Series) -> dict:
    """Two horizontal thresholds at 1.33SD and 2.28SD above zero. The standard
    deviation is computed over the smoothed `s_t`, matching the prof's
    script. Note: this anchors the bands at *zero*, not at `s_t.mean()`,
    which is why the analysis assumes the live signal is already
    baseline-normalised."""
    std = float(s_t.std(ddof=1)) if len(s_t.dropna()) >= 2 else 0.0
    return {
        'std': std,
        'mild': THRESH_MILD_SD * std,
        'high': THRESH_HIGH_SD * std,
    }


# ---------------------------------------------------------------------------
# Reporting + plotting
# ---------------------------------------------------------------------------

def fraction_in_each_zone(s_t: pd.Series, mild: float, high: float) -> dict:
    """Percent of live samples spent in each band. Useful for cross-method
    comparison: if your existing pipeline says "30% stressed" but this
    method says "15%", that's the magnitude of the disagreement."""
    s = s_t.dropna()
    if len(s) == 0:
        return {'calm': 0.0, 'stressed': 0.0, 'ultra': 0.0}
    calm_pct  = float(np.mean(s <= mild)) * 100.0
    ultra_pct = float(np.mean(s > high)) * 100.0
    stressed_pct = max(0.0, 100.0 - calm_pct - ultra_pct)
    return {'calm': calm_pct, 'stressed': stressed_pct, 'ultra': ultra_pct}


def print_audit_report(file_path: str, schema: str, fs_hz: float,
                       baseline_df: pd.DataFrame, live_df: pd.DataFrame,
                       avgs: dict, thresholds: dict,
                       df_full: pd.DataFrame,
                       smooth_window: int) -> None:
    """Print every intermediate number the user said they would audit.
    Layout is grouped by analysis step so it lines up with DataAnalyze.py."""
    print()
    print("=" * 66)
    print(f"datasource_2: prof reference math (DataAnalyze.py equivalent)")
    print("=" * 66)
    print(f"  File:    {file_path}")
    print(f"  Schema:  {schema}")
    print(f"  fs_hz:   {fs_hz:.3f}   (estimated from row time deltas)")
    print(f"  Window:  {smooth_window} samples ~= {smooth_window / fs_hz:.2f} s "
          f"(rolling smoothing)")
    print()

    # Step 2: balloon stats
    h = live_df['height'].dropna()
    if len(h):
        print(f"[Step 2] Balloon height (live window):")
        print(f"  rows = {len(h)}   "
              f"avg = {h.mean():.2f} m   std = {h.std(ddof=1):.2f} m   "
              f"min = {h.min():.1f}   max = {h.max():.1f}")
    else:
        print(f"[Step 2] Balloon height: no rows.")
    print()

    # Step 3: per-signal baseline averages
    print(f"[Step 3] Baseline averages (no outlier removal -- plain mean):")
    print(f"  baseline rows : {len(baseline_df)}  "
          f"({len(baseline_df) / fs_hz:.1f} s)")
    print(f"  avgEDA = {avgs['eda']:7.3f}    "
          f"avgHR  = {avgs['hr']:7.2f}    "
          f"avgHRV = {avgs['hrv']:7.2f}")
    print()

    # Step 4-5 sanity checks on the deltas + index
    s_t = df_full['s_t']
    s_instant = df_full['s_instant']
    valid = s_t.dropna()
    print(f"[Step 4-5] Composite stress index (0.5*EDA + 0.3*HRV + 0.2*HR):")
    print(f"  live rows           : {len(live_df)}")
    print(f"  s_instant mean      : {s_instant.mean():+.3f}   "
          f"std = {s_instant.std(ddof=1):.3f}")
    print(f"  s_t (smoothed) mean : {valid.mean():+.3f}   "
          f"std = {valid.std(ddof=1):.3f}")
    print(f"  s_t range           : [{valid.min():+.3f}, {valid.max():+.3f}]")
    print()

    # Step 6: thresholds + time in each zone
    print(f"[Step 6] Thresholds (multiples of stdStress = {thresholds['std']:.3f}):")
    print(f"  mild (1.33SD)  = {thresholds['mild']:+.3f}")
    print(f"  high (2.28SD)  = {thresholds['high']:+.3f}")
    pct = fraction_in_each_zone(s_t, thresholds['mild'], thresholds['high'])
    print(f"  Time in zones (% of valid live samples):")
    print(f"    calm     : {pct['calm']:5.1f}%")
    print(f"    stressed : {pct['stressed']:5.1f}%")
    print(f"    ultra    : {pct['ultra']:5.1f}%")
    print()

    # Step 7: correlation
    h_aligned = df_full['height']
    pair = pd.concat([s_t, h_aligned], axis=1).dropna()
    if len(pair) >= 3 and pair.iloc[:, 1].std() > 0:
        corr = float(pair.iloc[:, 0].corr(pair.iloc[:, 1]))
        print(f"[Step 7] Pearson correlation, s_t vs balloonHeight:")
        print(f"  r = {corr:+.3f}   (n = {len(pair)})")
        print(f"  Interpretation: does the stress index track the altitude?")
    else:
        print(f"[Step 7] Correlation skipped (no balloon height data or "
              f"insufficient variance).")
    print()
    print("=" * 66)


def save_plot(file_path: str, df_full: pd.DataFrame, thresholds: dict,
              out_png: str) -> None:
    fig, axs = plt.subplots(2, figsize=(12, 10))

    # Top: stress index with two threshold lines (same layout as DataAnalyze.py)
    axs[0].plot(df_full['time'], df_full['s_t'],
                label='Stress Index (s_t)', color='blue', linewidth=1.2)
    axs[0].axhline(y=thresholds['mild'], color='orange', linestyle='-',
                   linewidth=1.2, alpha=0.6,
                   label=f"1.33SD ~= mild = {thresholds['mild']:+.2f}")
    axs[0].axhline(y=thresholds['high'], color='red', linestyle='-',
                   linewidth=1.5, alpha=0.6,
                   label=f"2.28SD ~= high = {thresholds['high']:+.2f}")
    axs[0].axhline(y=0.0, color='#666666', linestyle=':', linewidth=0.8)
    axs[0].set_title('Stress Index vs Time (datasource_2 / Moldoveanu math)',
                     fontsize=14)
    axs[0].set_xlabel('Time (s)')
    axs[0].set_ylabel('Stress Index (%)')
    axs[0].grid(True, axis='y', linestyle='--', alpha=0.3)
    axs[0].legend(loc='lower right', fontsize=9)

    # Bottom: balloon height vs time
    axs[1].plot(df_full['time'], df_full['height'],
                label='Balloon Height', color='blue', linewidth=1.2)
    axs[1].set_title('Balloon Height vs Time', fontsize=14)
    axs[1].set_xlabel('Time (s)')
    axs[1].set_ylabel('Height (m)')
    axs[1].grid(True, axis='y', linestyle='--', alpha=0.3)

    plt.tight_layout()
    plt.savefig(out_png, dpi=120)
    print(f"[Plot] Saved: {out_png}")


def save_trace_csv(df_full: pd.DataFrame, out_csv: str) -> None:
    """Persist the per-sample trace so the user can join it to their existing
    pipeline output and diff row-by-row in pandas/Excel."""
    keep = ['time', 'normEDA', 'normHRV', 'normHR', 's_instant', 's_t', 'height']
    df_full[keep].to_csv(out_csv, index=False, float_format='%.4f')
    print(f"[CSV ] Saved: {out_csv}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(file_path: str) -> int:
    if not os.path.isfile(file_path):
        print(f"ERROR: file not found: {file_path}")
        return 1

    print(f"[Load] Reading {file_path} ...")
    df = _read_csv_skipping_comments(file_path)
    print(f"[Load] {len(df)} rows, {len(df.columns)} columns.")

    schema = detect_schema(df)
    print(f"[Load] Detected schema: {schema}")

    if schema == 'prof':
        extracted = extract_prof(df)
    else:
        extracted = extract_user(df)
    baseline_df = extracted['baseline_df']
    live_df = extracted['live_df']
    fs_hz = extracted['fs_hz']

    if len(baseline_df) < 10:
        print(f"ERROR: baseline window has only {len(baseline_df)} rows; "
              f"averages would not be reliable.")
        return 2
    if len(live_df) < 10:
        print(f"ERROR: live window has only {len(live_df)} rows; "
              f"nothing to analyze.")
        return 3

    # Pick a smoothing window that corresponds to ~1 second of data.
    # DataAnalyze.py hardcodes 50, which assumes ~50 Hz. We auto-adapt
    # so user samples.csv (typically 1 Hz) and prof data (~50 Hz) both
    # smooth over a comparable physical time window.
    smooth_window = max(1, int(round(fs_hz * 1.0)))
    if smooth_window == 1:
        smooth_window = min(SMOOTH_WINDOW_SAMPLES_FALLBACK, max(3, len(live_df) // 20))
    print(f"[Calc] Rolling smoothing window = {smooth_window} samples "
          f"({smooth_window / fs_hz:.2f} s)")

    avgs = compute_baseline_averages(baseline_df)
    norm = compute_normalised_deltas(live_df, avgs)
    df_full = compute_stress_index(norm, smooth_window)
    thresholds = compute_thresholds(df_full['s_t'])

    print_audit_report(file_path, schema, fs_hz, baseline_df, live_df,
                       avgs, thresholds, df_full, smooth_window)

    out_png = file_path + '.datasource_2.png'
    out_csv = file_path + '.datasource_2.csv'
    save_plot(file_path, df_full, thresholds, out_png)
    save_trace_csv(df_full, out_csv)
    # Show the plot only if running interactively; in headless / scripted
    # runs we just save and exit so the script is composable.
    if os.environ.get('DATASOURCE_2_SHOW', '0') == '1':
        plt.show()
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("usage: python datasource_2.py <session_csv_path>")
        print("       (works on prof BiofeedbackSessions/*.csv OR pipeline samples.csv)")
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
