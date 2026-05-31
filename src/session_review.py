"""
session_review.py
=================

Offline replay + summary for any past Biofeedback session.

Usage:
    python src/session_review.py            # interactive picker
    python src/session_review.py <path>     # open a specific CSV

Behavior:
    1. Lists every data/session_*.csv (newest first).
    2. After you pick one (or pass a path), opens a matplotlib window with:
         - S_t over time with threshold bands shaded (calm / stressed / ultra)
         - Smoothed EDA, HR, HRV alongside
         - Time-in-state summary panel
         - Session metadata (patient, mode, duration, artifacts)
    3. Prints a one-line summary suitable for clinical notes.

This is the "Monitoring and Analysis -> Data Logger" view from the framework
diagram — a way to audit any past participant's session without re-running it.
"""

import glob
import json
import os
import sys
import argparse
from datetime import datetime
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt

# Apply a dark theme globally so axes / text / ticks all match the clinical look.
plt.style.use('dark_background')
matplotlib.rcParams.update({
    'figure.facecolor': '#0a0a0a',
    'axes.facecolor':   '#0a0a0a',
    'savefig.facecolor': '#0a0a0a',
    'axes.edgecolor':   '#888888',
    'axes.labelcolor':  '#dddddd',
    'xtick.color':      '#cccccc',
    'ytick.color':      '#cccccc',
    'text.color':       '#eeeeee',
    'axes.titlecolor':  '#ffffff',
    'grid.color':       '#333333',
    'grid.alpha':       0.5,
})


def find_sessions(data_dir: str):
    """Return all session CSVs in data_dir, newest first."""
    if not os.path.isdir(data_dir):
        return []
    files = [
        os.path.join(data_dir, f)
        for f in os.listdir(data_dir)
        if f.startswith('session_') and f.endswith('.csv')
    ]
    files.sort(key=os.path.getmtime, reverse=True)
    return files


def interactive_pick(sessions):
    """Show numbered list, return the chosen path."""
    if not sessions:
        print("[REVIEW] No session_*.csv files found in data/.")
        sys.exit(1)
    print("\nAvailable sessions (newest first):\n")
    for idx, path in enumerate(sessions, 1):
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        size_kb = os.path.getsize(path) / 1024
        print(f"  [{idx:2d}] {os.path.basename(path):50s}  "
              f"{mtime:%Y-%m-%d %H:%M}  {size_kb:>7.1f} KB")
    print()
    while True:
        choice = input(f"Pick a session [1-{len(sessions)}] (or q to quit): ").strip()
        if choice.lower() in ('q', 'quit', 'exit'):
            sys.exit(0)
        try:
            i = int(choice)
            if 1 <= i <= len(sessions):
                return sessions[i - 1]
        except ValueError:
            pass
        print(f"  Invalid input. Enter a number 1-{len(sessions)}.")


def _safe(col, default=None):
    """Return a single value from a Series-or-missing column."""
    if col is None or len(col) == 0:
        return default
    try:
        return col.iloc[0]
    except Exception:
        return default


def load_baseline_json(csv_path: str):
    """Find the baseline_*.json that matches this session CSV's timestamp
    + patient id, if one exists in the same data/ folder. The CSV filename
    looks like  session_<ts>_<first>_<id>.csv  and the baseline JSON looks
    like  baseline_<ts>_<id>.json  — same timestamp, same id. Returns the
    parsed dict, or None if no match found."""
    base = os.path.basename(csv_path)
    if not base.startswith('session_'):
        return None
    parts = base[len('session_'):-len('.csv')].split('_')
    if len(parts) < 3:
        return None
    ts = '_'.join(parts[:2])      # 'YYYYMMDD_HHMMSS'
    pid = parts[-1]               # last token is patient id
    pattern = os.path.join(os.path.dirname(csv_path),
                           f"baseline_{ts}_{pid}.json")
    matches = glob.glob(pattern)
    if not matches:
        return None
    try:
        with open(matches[0], 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def summarize(df: pd.DataFrame, baseline_meta: dict = None) -> dict:
    """Compute summary statistics for one session.

    Pulls demographics from the per-tick CSV (every row carries them) and,
    if a matching baseline JSON is present, also pulls the personal
    averages and locked thresholds for the clinical record."""
    live_df = df[df['phase'] == 'LIVE'] if 'phase' in df.columns else df
    state_counts = (live_df['state'].value_counts().to_dict()
                    if 'state' in live_df.columns else {})
    pipeline_rate = 50.0  # Hz; matches Config.PIPELINE_RATE

    def seconds(state):
        return state_counts.get(state, 0) / pipeline_rate

    first = _safe(df.get('patient_first_name'), '')
    last  = _safe(df.get('patient_last_name'),  '')
    pid   = _safe(df.get('patient_id'),        '?')
    gender = _safe(df.get('gender'), '?')
    sdate  = _safe(df.get('session_date'), '?')
    snum   = _safe(df.get('session_number'), '?')

    out = {
        'patient_full':       f"{first} {last}".strip() or '?',
        'patient_id':         str(pid),
        'gender':             str(gender),
        'session_date':       str(sdate),
        'session_number':     str(snum),
        'samples_total':      len(df),
        'samples_live':       len(live_df),
        'duration_sec':       len(df) / pipeline_rate,
        'live_duration_sec':  len(live_df) / pipeline_rate,
        'time_calm':          seconds('calm'),
        'time_stressed':      seconds('stressed'),
        'time_ultra':         seconds('ultra_stressed'),
        'mean_s_t':           float(live_df['s_t'].mean()) if len(live_df) else 0.0,
        'max_s_t':            float(live_df['s_t'].max())  if len(live_df) else 0.0,
        'mean_dashboard':     float(live_df['dashboard_score'].mean()) if len(live_df) else 0.0,
        'artifacts_eda':      int(_safe(df.get('artifacts_eda'), 0) or 0),
        'artifacts_hr':       int(_safe(df.get('artifacts_hr'),  0) or 0),
        'artifacts_hrv':      int(_safe(df.get('artifacts_hrv'), 0) or 0),
        'baseline_eda':       None,
        'baseline_hr':        None,
        'baseline_hrv':       None,
        'thresh_mild':        None,
        'thresh_high':        None,
        'sigma_baseline':     None,
        'data_source':        None,
    }
    if baseline_meta:
        try:
            pb = baseline_meta.get('personal_baselines', {})
            out['baseline_eda']  = pb.get('eda_uS')
            out['baseline_hr']   = pb.get('hr_bpm')
            out['baseline_hrv']  = pb.get('hrv_ms')
            th = baseline_meta.get('thresholds', {})
            out['thresh_mild']   = th.get('mild')
            out['thresh_high']   = th.get('high')
            out['sigma_baseline'] = baseline_meta.get('sigma_baseline')
            out['data_source']    = baseline_meta.get('source')
        except Exception:
            pass
    return out


def render(df: pd.DataFrame, summary: dict, csv_path: str,
           show: bool = True, save_path: str = None):
    """Build the matplotlib review figure. If save_path is given, write it
    to disk (PNG/PDF per file extension) instead of (or in addition to)
    popping a window."""
    fig, axes = plt.subplots(4, 1, figsize=(14, 9), sharex=True,
                             gridspec_kw={'height_ratios': [3, 1, 1, 1]})
    fig.suptitle(
        f"Session Review — {summary['patient_full']} "
        f"(ID {summary['patient_id']}, {summary['gender']}, "
        f"session #{summary['session_number']} on {summary['session_date']}) — "
        f"{os.path.basename(csv_path)}",
        fontsize=11, fontweight='bold', color='#ffffff'
    )

    t = df.index.values / 50.0  # convert sample index to seconds

    # --- S_t with state bands ---
    ax = axes[0]
    ax.set_title("Stress Index  S_t", loc='left', fontsize=10, color='#ffffff')
    ax.plot(t, df['s_t'], color='#ffffff', linewidth=1.2)
    # Shade state bands behind the curve for visual scanning. Same RGB tuples
    # as the live dashboard so a clinician's eye can move between the two
    # tools without re-learning the colour code.
    state_colors = {'calm': '#1f3a1f', 'stressed': '#3a3a1f',
                    'ultra_stressed': '#3a1f1f', 'unknown': '#222222'}
    prev_state = None
    seg_start = 0
    for i, state in enumerate(df['state']):
        if state != prev_state:
            if prev_state is not None:
                ax.axvspan(t[seg_start], t[i],
                           color=state_colors.get(prev_state, '#222'), alpha=0.55)
            seg_start = i
            prev_state = state
    if prev_state is not None:
        ax.axvspan(t[seg_start], t[-1],
                   color=state_colors.get(prev_state, '#222'), alpha=0.55)
    ax.set_ylabel("S_t", color='#dddddd')

    # --- Smoothed EDA / HR / HRV ---
    for ax, col, color, label in (
        (axes[1], 'eda', '#00ff66', 'EDA (μS)'),
        (axes[2], 'hr',  '#ff9933', 'HR (BPM)'),
        (axes[3], 'hrv', '#33aaff', 'HRV (ms)'),
    ):
        ax.plot(t, df[col], color=color, linewidth=1.1)
        ax.set_ylabel(label, fontsize=9, color='#dddddd')
    axes[-1].set_xlabel("time (s)", color='#dddddd')

    # --- Summary text box ---
    def _fmt(x, unit='', spec='.2f'):
        return f"{x:{spec}}{unit}" if x is not None else "--"

    box = (
        f"Patient:    {summary['patient_full']}\n"
        f"ID / sex:   {summary['patient_id']}  /  {summary['gender']}\n"
        f"Session:    #{summary['session_number']}  on  {summary['session_date']}\n"
        f"Source:     {summary.get('data_source') or '--'}\n"
        f"\n"
        f"Duration:   {summary['duration_sec']:.1f}s total "
        f"({summary['live_duration_sec']:.1f}s LIVE)\n"
        f"\n"
        f"Time CALM:     {summary['time_calm']:>6.1f}s\n"
        f"Time STRESSED: {summary['time_stressed']:>6.1f}s\n"
        f"Time ULTRA:    {summary['time_ultra']:>6.1f}s\n"
        f"\n"
        f"S_t  mean: {summary['mean_s_t']:+.2f}  max: {summary['max_s_t']:+.2f}\n"
        f"Score mean: {summary['mean_dashboard']:.1f}/100\n"
        f"\n"
        f"Personal baseline:\n"
        f"  EDA = {_fmt(summary['baseline_eda'], ' uS')}\n"
        f"  HR  = {_fmt(summary['baseline_hr'],  ' BPM')}\n"
        f"  HRV = {_fmt(summary['baseline_hrv'], ' ms')}\n"
        f"\n"
        f"Locked thresholds:\n"
        f"  MILD = {_fmt(summary['thresh_mild'])}\n"
        f"  HIGH = {_fmt(summary['thresh_high'])}\n"
        f"  sigma = {_fmt(summary['sigma_baseline'])}\n"
        f"\n"
        f"Artifacts removed:\n"
        f"  EDA = {summary['artifacts_eda']}\n"
        f"  HR  = {summary['artifacts_hr']}\n"
        f"  HRV = {summary['artifacts_hrv']}"
    )
    fig.text(0.99, 0.5, box, ha='right', va='center',
             fontsize=9.5, family='monospace', color='#eeeeee',
             bbox=dict(facecolor='#1a1a1a', edgecolor='#666666',
                       boxstyle='round,pad=0.8', linewidth=1.0))

    # Color legend for the state bands so the operator knows what each shade means.
    legend_y = 0.13
    for i, (label, color) in enumerate([
        ('CALM',      state_colors['calm']),
        ('STRESSED',  state_colors['stressed']),
        ('ULTRA',     state_colors['ultra_stressed']),
    ]):
        fig.text(0.82 + i * 0.05, legend_y, f"  {label}  ",
                 ha='center', va='center', fontsize=8, color='#eeeeee',
                 bbox=dict(facecolor=color, edgecolor='#555555',
                           boxstyle='round,pad=0.3'))

    fig.subplots_adjust(right=0.78, hspace=0.30, top=0.92, bottom=0.08)
    try:
        fig.canvas.manager.set_window_title(
            f"Session Review — {os.path.basename(csv_path)}"
        )
    except Exception:
        pass
    if save_path:
        # dpi=150 + tight margins gives a crisp page suitable for
        # printing on A4/Letter and stapling to the patient file.
        fig.savefig(save_path, dpi=150, facecolor=fig.get_facecolor(),
                    bbox_inches='tight')
    if show:
        plt.show()
    else:
        plt.close(fig)


def cli_summary_line(summary: dict, csv_path: str):
    """Multi-line summary printed to console — paste-friendly for the
    patient file. Mirrors what's in the review-window box."""
    print()
    print(f"[REVIEW] {os.path.basename(csv_path)}")
    print(f"   patient    : {summary['patient_full']}  "
          f"(ID {summary['patient_id']}, {summary['gender']})")
    print(f"   session    : #{summary['session_number']}  on {summary['session_date']}")
    print(f"   source     : {summary.get('data_source') or '--'}")
    print(f"   duration   : {summary['live_duration_sec']:.0f}s LIVE "
          f"({summary['duration_sec']:.0f}s total)")
    print(f"   time-state : CALM={summary['time_calm']:.0f}s  "
          f"STRESSED={summary['time_stressed']:.0f}s  "
          f"ULTRA={summary['time_ultra']:.0f}s")
    print(f"   S_t        : mean={summary['mean_s_t']:+.2f}  "
          f"max={summary['max_s_t']:+.2f}  "
          f"score mean={summary['mean_dashboard']:.1f}/100")
    if summary['baseline_eda'] is not None:
        print(f"   baseline   : EDA={summary['baseline_eda']:.2f}uS  "
              f"HR={summary['baseline_hr']:.1f}BPM  "
              f"HRV={summary['baseline_hrv']:.1f}ms")
    if summary['thresh_mild'] is not None:
        print(f"   thresholds : MILD={summary['thresh_mild']:.2f}  "
              f"HIGH={summary['thresh_high']:.2f}  "
              f"sigma={summary['sigma_baseline']:.3f}")
    print(f"   artifacts  : EDA={summary['artifacts_eda']} "
          f"HR={summary['artifacts_hr']} HRV={summary['artifacts_hrv']}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Replay a past Biofeedback session.")
    parser.add_argument('csv', nargs='?',
                        help="Path to session_*.csv (omit for picker).")
    parser.add_argument('--no-window', action='store_true',
                        help="Print summary only; skip the matplotlib window.")
    parser.add_argument('--save',
                        help="Save the review figure to PATH (PNG or PDF) for "
                             "attaching to the patient file. Implies --no-window.")
    args = parser.parse_args()

    if args.csv:
        path = args.csv
        if not os.path.isfile(path):
            print(f"[REVIEW] File not found: {path}")
            sys.exit(1)
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(os.path.dirname(here), 'data')
        sessions = find_sessions(data_dir)
        path = interactive_pick(sessions)

    df = pd.read_csv(path)
    baseline = load_baseline_json(path)
    summary = summarize(df, baseline_meta=baseline)
    cli_summary_line(summary, path)

    if args.save:
        # Build the figure but don't pop a window — dump to disk and exit.
        # matplotlib will pick PDF vs PNG from the file extension.
        plt.ioff()
        render(df, summary, path, show=False, save_path=args.save)
        print(f"[REVIEW] Saved review to: {args.save}")
        return

    if not args.no_window:
        render(df, summary, path)


if __name__ == '__main__':
    main()
