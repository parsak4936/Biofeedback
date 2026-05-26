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


def summarize(df: pd.DataFrame) -> dict:
    """Compute summary statistics for the session."""
    live_df = df[df['phase'] == 'LIVE']
    state_counts = live_df['state'].value_counts().to_dict()
    pipeline_rate = 50.0  # Hz; matches Config.PIPELINE_RATE

    def seconds(state):
        return state_counts.get(state, 0) / pipeline_rate

    return {
        'patient': f"{df['patient_name'].iloc[0]} ({df['patient_id'].iloc[0]})",
        'mode': df.get('session_mode', pd.Series(['unknown'])).iloc[0],
        'samples_total': len(df),
        'samples_live': len(live_df),
        'duration_sec': len(df) / pipeline_rate,
        'live_duration_sec': len(live_df) / pipeline_rate,
        'time_calm': seconds('calm'),
        'time_stressed': seconds('stressed'),
        'time_ultra': seconds('ultra_stressed'),
        'mean_s_t': live_df['s_t'].mean() if len(live_df) else 0.0,
        'max_s_t': live_df['s_t'].max() if len(live_df) else 0.0,
        'artifacts_eda': df.get('artifacts_eda', pd.Series([0])).iloc[-1] if len(df) else 0,
        'artifacts_hr':  df.get('artifacts_hr',  pd.Series([0])).iloc[-1] if len(df) else 0,
        'artifacts_hrv': df.get('artifacts_hrv', pd.Series([0])).iloc[-1] if len(df) else 0,
    }


def render(df: pd.DataFrame, summary: dict, csv_path: str):
    """Build the matplotlib review window — dark theme, clinical look."""
    fig, axes = plt.subplots(4, 1, figsize=(14, 9), sharex=True,
                             gridspec_kw={'height_ratios': [3, 1, 1, 1]})
    fig.suptitle(
        f"Session Review — {summary['patient']} — mode={summary['mode']} — "
        f"file={os.path.basename(csv_path)}",
        fontsize=12, fontweight='bold', color='#ffffff'
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
    box = (
        f"Patient: {summary['patient']}\n"
        f"Mode: {summary['mode']}\n"
        f"Total duration: {summary['duration_sec']:.1f}s "
        f"(LIVE: {summary['live_duration_sec']:.1f}s)\n"
        f"\n"
        f"Time CALM:     {summary['time_calm']:>6.1f}s\n"
        f"Time STRESSED: {summary['time_stressed']:>6.1f}s\n"
        f"Time ULTRA:    {summary['time_ultra']:>6.1f}s\n"
        f"\n"
        f"S_t mean: {summary['mean_s_t']:.2f}    max: {summary['max_s_t']:.2f}\n"
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
    plt.show()


def cli_summary_line(summary: dict, csv_path: str):
    """One-line summary printed to console — paste-friendly for notes."""
    print()
    print(f"[REVIEW] {os.path.basename(csv_path)}")
    print(f"   patient={summary['patient']}  mode={summary['mode']}  "
          f"live={summary['live_duration_sec']:.0f}s")
    print(f"   CALM={summary['time_calm']:.0f}s  "
          f"STRESSED={summary['time_stressed']:.0f}s  "
          f"ULTRA={summary['time_ultra']:.0f}s  "
          f"|  mean S_t={summary['mean_s_t']:.2f}  max={summary['max_s_t']:.2f}")
    print(f"   artifacts: EDA={summary['artifacts_eda']} "
          f"HR={summary['artifacts_hr']} HRV={summary['artifacts_hrv']}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Replay a past Biofeedback session.")
    parser.add_argument('csv', nargs='?', help="Path to session_*.csv (omit for picker).")
    parser.add_argument('--no-window', action='store_true',
                        help="Print summary only; skip the matplotlib window.")
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
    summary = summarize(df)
    cli_summary_line(summary, path)
    if not args.no_window:
        render(df, summary, path)


if __name__ == '__main__':
    main()
