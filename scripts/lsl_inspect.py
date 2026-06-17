"""
scripts/lsl_inspect.py
======================
STANDALONE diagnostic. Does NOT touch the main pipeline.

Connects to the OpenSignals LSL stream and prints, ONCE PER SECOND:
  - the raw ADC value of every channel
  - the converted physical value if the channel is EDA or ECG
  - the rolling std of each channel over the last second (so you can
    immediately spot dead/constant channels)

Use this to answer "what is OpenSignals actually publishing to LSL?" --
independent of our pipeline, independent of the dashboard. If the values
here look real but the dashboard shows constants, the bug is in the
dashboard / pipeline. If the values here look dead while OpenSignals's
own UI shows real waveforms, the bug is in OpenSignals's LSL bridge.

Usage:
    env\\Scripts\\activate.bat
    python scripts\\lsl_inspect.py

Press Ctrl+C to stop.
"""

from __future__ import annotations
import sys
import time
import os

# Make src/ importable so we can use the same ADC->physical converters
# as the pipeline (so any discrepancy is NOT a converter mismatch).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

import numpy as np
from pylsl import resolve_streams, StreamInlet

from data_sources import adc_to_eda_uS, adc_to_ecg_mV


def _read_channel_labels(inlet, timeout=2.0):
    """Pull the per-channel `label` field from the LSL stream metadata."""
    labels = []
    try:
        info = inlet.info(timeout=timeout)
        ch = info.desc().child("channels").child("channel")
        while not ch.empty():
            labels.append((ch.child_value("label") or "").strip().upper())
            ch = ch.next_sibling()
    except Exception:
        labels = []
    return labels


def main():
    print("=" * 70)
    print("  LSL INSPECTOR -- raw view of what OpenSignals publishes")
    print("=" * 70)
    print()
    print("[lsl_inspect] Looking for any LSL stream on the network...")
    streams = resolve_streams(wait_time=2.0)
    if not streams:
        print("[lsl_inspect] NO streams found. Things to check:")
        print("  - OpenSignals is running")
        print("  - OpenSignals's Lab Streaming Layer toggle is ON")
        print("  - Recording is active (red button pressed)")
        return 1

    # Prefer the 'OpenSignals' stream if present, else the first one.
    chosen = None
    for s in streams:
        if s.name().lower() == 'opensignals':
            chosen = s
            break
    if chosen is None:
        chosen = streams[0]

    print(f"[lsl_inspect] Found {len(streams)} stream(s):")
    for s in streams:
        marker = "  <-- watching" if s is chosen else ""
        print(f"  - name='{s.name()}'  type='{s.type()}'  "
              f"channels={s.channel_count()}  fs={s.nominal_srate():.0f} Hz"
              f"{marker}")
    print()

    inlet = StreamInlet(chosen)
    labels = _read_channel_labels(inlet)
    n_ch = chosen.channel_count()
    if labels:
        print(f"[lsl_inspect] Channel labels: {labels}")
    else:
        print(f"[lsl_inspect] No labels in metadata; channels are numbered "
              f"0..{n_ch-1}.")
    print()
    print("[lsl_inspect] Now printing ONE LINE PER SECOND with what each")
    print("              channel is publishing. Ctrl+C to stop.")
    print()
    print(f"  {'channel':<12s} {'raw ADC mean':>14s} {'raw ADC std':>13s}  "
          f"{'as-EDA uS':>11s}  {'as-ECG mV':>11s}  {'verdict':<30s}")
    print("  " + "-" * 105)

    try:
        while True:
            time.sleep(1.0)
            samples, _ = inlet.pull_chunk(timeout=0.0)
            if not samples:
                print("  (no samples received in last second -- recording "
                      "may have stopped)")
                continue
            arr = np.asarray(samples, dtype=float)
            n_pulled = arr.shape[0]

            for ch_idx in range(n_ch):
                col = arr[:, ch_idx]
                m = float(col.mean())
                s = float(col.std())
                # Convert assuming this channel were each sensor type, so the
                # operator can directly see what it would be on either path.
                eda_uS = float(adc_to_eda_uS(m))
                ecg_mV = float(adc_to_ecg_mV(m))
                label = labels[ch_idx] if ch_idx < len(labels) else f'ch{ch_idx}'

                # Verdict heuristic -- only an aid, not a proof.
                if s < 1e-9:
                    verdict = "DEAD (constant value)"
                elif s < 0.5 and ch_idx > 0:
                    verdict = "very flat -- contact issue?"
                elif label.startswith('NSEQ') or 'SEQ' in label:
                    verdict = "sample counter (expected)"
                elif label == 'DI':
                    verdict = "digital line (expected flat)"
                else:
                    verdict = "OK -- has variation"

                print(f"  {label:<12s} {m:14.1f} {s:13.3f}  "
                      f"{eda_uS:11.4f}  {ecg_mV:+11.4f}  {verdict:<30s}")
            print()
    except KeyboardInterrupt:
        print("\n[lsl_inspect] Stopped by user.")
        return 0


if __name__ == '__main__':
    sys.exit(main())
