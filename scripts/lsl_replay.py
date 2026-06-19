"""
scripts/lsl_replay.py
=====================
STANDALONE replay tool. Does NOT modify the main pipeline.

Reads an OpenSignals .txt file from disk and publishes its raw ADC
samples on a fake LSL stream named "OpenSignals" -- the same stream
name and channel layout the real OpenSignals application uses
(`['NSEQ', 'EDA0', 'ECG1']`).

The point: you can run the full pipeline (`run.bat` with
Config.DATA_SOURCE = 'real_plux') end-to-end as if the PLUX device
were physically connected, but the data is replayed from a file you
already trust. If the pipeline produces correct HR / HRV / EDA on the
replay but fails on the real OpenSignals LSL output, that's
definitive proof that the bug is in OpenSignals (or its LSL bridge),
not in our code.

How the channel layout matches the real OpenSignals stream:
  - channel 0: nSeq            (label "NSEQ")
  - channel 1: CH1 raw ADC     (label "EDA0")
  - channel 2: CH2 raw ADC     (label "ECG1")

The file's "DI" column (digital input) is dropped because the real
OpenSignals LSL stream doesn't include it either. Confirmed by your
earlier `[DATA SOURCE] labels: ['NSEQ', 'EDA0', 'ECG1']` log line.

Usage
-----
    env\\Scripts\\activate.bat
    python scripts\\lsl_replay.py <path_to_opensignals_file.txt>

Then in ANOTHER CMD window:
    set Config.DATA_SOURCE = 'real_plux' in src/config.py
    run.bat

The pipeline will see "OpenSignals" on LSL, connect, and process the
replayed samples exactly as if they came from the device.

Optional flags:
    --loop            Restart at end of file (default: stop and exit).
    --rate <Hz>       Override playback rate; defaults to the file's
                      native sampling rate from its JSON header.
    --speed <factor>  Playback speed multiplier (1.0 = native, 2.0 =
                      twice as fast for quick testing).

Press Ctrl+C to stop.
"""

from __future__ import annotations
import sys
import os
import time
import argparse

import numpy as np
from pylsl import StreamInfo, StreamOutlet

# Make src/ importable so we can reuse parse_opensignals_header.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))
from data_sources import parse_opensignals_header, adc_to_eda_uS, adc_to_ecg_mV


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument('file', help='Path to an OpenSignals .txt recording.')
    ap.add_argument('--loop', action='store_true',
                    help='Restart at end of file instead of stopping.')
    ap.add_argument('--rate', type=float, default=None,
                    help='Override the playback rate (Hz). Default: '
                         'native rate from the file header.')
    ap.add_argument('--speed', type=float, default=1.0,
                    help='Playback speed multiplier. 1.0 = native. '
                         '2.0 = twice as fast.')
    args = ap.parse_args()

    if not os.path.isfile(args.file):
        print(f"ERROR: file not found: {args.file}")
        return 1

    # ---- Parse the OpenSignals header ----
    header = parse_opensignals_header(args.file)
    fs_native = header['fs_hz']
    fs_hz = args.rate if args.rate is not None else fs_native
    fs_effective = fs_hz * args.speed
    sample_period = 1.0 / fs_effective

    col_eda = header['column_index'].get('EDA')
    col_ecg = header['column_index'].get('ECG')
    if col_eda is None or col_ecg is None:
        print(f"ERROR: file is missing EDA or ECG sensor. "
              f"Sensors found: {header['sensor_order']}")
        return 1

    # Load the whole file into memory. For 60-minute recordings this is
    # still under 100 MB so it's fine.
    n_cols = len(header['columns'])
    data = np.genfromtxt(
        args.file, skip_header=3, usecols=tuple(range(n_cols)),
        invalid_raise=False,
    )
    data = data[np.all(np.isfinite(data), axis=1)]
    n_samples = data.shape[0]

    # Pre-extract the columns we will publish. nSeq is the first
    # column; CH1 and CH2 raw ADC values come from the file. Drop DI
    # to match the real OpenSignals LSL channel layout.
    #
    # IMPORTANT (2026-06-19 discovery): the real OpenSignals LSL bridge
    # publishes PRE-CONVERTED physical units (uS for EDA, mV for ECG),
    # NOT raw ADC. So to match that format, we convert ADC -> physical
    # units HERE before pushing. The pipeline (with Config.LSL_VALUES_
    # PRECONVERTED=True) then reads them as physical units directly.
    # Result: replay and real device produce identical LSL streams.
    col_nseq = 0       # nSeq is always first in OpenSignals files
    eda_adc_raw = data[:, col_eda].astype(np.float32)
    ecg_adc_raw = data[:, col_ecg].astype(np.float32)
    nseq = data[:, col_nseq].astype(np.float32)
    # Convert here so the LSL outlet publishes uS and mV (just like real
    # OpenSignals does).
    eda_uS_stream = adc_to_eda_uS(eda_adc_raw).astype(np.float32)
    ecg_mV_stream = adc_to_ecg_mV(ecg_adc_raw).astype(np.float32)

    duration_s = n_samples / fs_native

    print("=" * 70)
    print("  LSL REPLAY -- pretending to be the OpenSignals application")
    print("=" * 70)
    print(f"  File          : {os.path.basename(args.file)}")
    print(f"  Native rate   : {fs_native:.0f} Hz")
    print(f"  Playback rate : {fs_effective:.0f} Hz "
          f"({args.speed:.2f}x native)")
    print(f"  Duration      : {duration_s:.1f} s ({n_samples} samples)")
    print(f"  EDA   column  : {col_eda}  (label EDA0)")
    print(f"  ECG   column  : {col_ecg}  (label ECG1)")
    print(f"  Loop          : {'yes' if args.loop else 'no'}")
    print()

    # First-sample preview so the operator knows what's in the file.
    print(f"  Preview (first row of file):")
    print(f"    nSeq        = {int(nseq[0])}")
    print(f"    EDA file ADC= {int(eda_adc_raw[0])}  "
          f"-> pushing {eda_uS_stream[0]:.4f} uS on LSL")
    print(f"    ECG file ADC= {int(ecg_adc_raw[0])}  "
          f"-> pushing {ecg_mV_stream[0]:+.4f} mV on LSL")
    print()

    # ---- Build the LSL outlet matching OpenSignals's real layout ----
    # The pipeline's channel resolver picks EDA/ECG by label match
    # (case-insensitive substring), so labels MUST contain "EDA" and
    # "ECG" -- otherwise the resolver falls back to fixed indices and
    # could pick the wrong channels.
    info = StreamInfo(
        name="OpenSignals",
        type="Data",
        channel_count=3,
        nominal_srate=fs_hz,
        channel_format='float32',
        source_id='lsl_replay',
    )
    # Attach per-channel labels via the XML metadata API. Matches what
    # the real OpenSignals client publishes.
    channels = info.desc().append_child("channels")
    for label, unit, ctype in (
            ("NSEQ", "n/a", "counter"),
            ("EDA0", "uS",  "pre_converted"),
            ("ECG1", "mV",  "pre_converted"),
    ):
        ch = channels.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", unit)
        ch.append_child_value("type", ctype)

    outlet = StreamOutlet(info)
    print(f"[lsl_replay] LSL outlet 'OpenSignals' is now live "
          f"(3 channels @ {fs_hz:.0f} Hz).")
    print(f"[lsl_replay] In another window: run `run.bat` with "
          f"Config.DATA_SOURCE = 'real_plux'.")
    print(f"[lsl_replay] Ctrl+C to stop.")
    print()

    # ---- Stream the samples at native rate ----
    # We use a wall-clock catch-up loop instead of `time.sleep(1/fs)` per
    # sample because Windows `sleep` has ~15 ms granularity (a 200 Hz
    # stream would play back at ~65 Hz with naive per-sample sleeping).
    # On each iteration we compute "where should we be by now" from the
    # wall clock and push every sample up to that point, then yield
    # briefly to the OS.

    t_start = time.perf_counter()
    next_print = t_start + 1.0
    idx = 0
    eof_announced = False

    try:
        while True:
            if idx >= n_samples:
                if args.loop:
                    print(f"[lsl_replay] End of file -- looping.")
                    t_start = time.perf_counter()
                    idx = 0
                    eof_announced = False
                    continue
                else:
                    if not eof_announced:
                        print(f"[lsl_replay] End of file reached. "
                              f"Outlet stays open; pipeline will see "
                              f"no more samples and time out per "
                              f"STREAM_TIMEOUT_SEC.")
                        eof_announced = True
                    time.sleep(0.1)
                    continue

            # How far into the file should we be by now?
            now = time.perf_counter()
            target_idx = min(int((now - t_start) * fs_effective), n_samples)

            # Push every sample from idx up to target_idx.
            # Per the 2026-06-19 finding, we push PRE-CONVERTED physical
            # units (uS / mV) to match what real OpenSignals does on LSL.
            while idx < target_idx:
                sample = [
                    float(nseq[idx]),
                    float(eda_uS_stream[idx]),  # pre-converted uS
                    float(ecg_mV_stream[idx]),  # pre-converted mV
                ]
                outlet.push_sample(sample)
                idx += 1

            # Per-second progress line.
            if now >= next_print:
                elapsed = now - t_start
                progress_pct = 100.0 * idx / n_samples
                last = max(0, idx - 1)
                print(f"  t={elapsed:6.1f}s  played {idx:>8d}/{n_samples} "
                      f"({progress_pct:5.1f}%)  "
                      f"EDA={eda_uS_stream[last]:.3f} uS  "
                      f"ECG={ecg_mV_stream[last]:+.4f} mV")
                next_print = now + 1.0

            # Yield to OS so we don't burn 100% CPU. ~15 ms on Windows.
            time.sleep(0.001)

    except KeyboardInterrupt:
        print("\n[lsl_replay] Stopped by user.")
        return 0


if __name__ == '__main__':
    sys.exit(main())
