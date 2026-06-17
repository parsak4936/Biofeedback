"""
scripts/plux_direct_inspect.py
==============================
STANDALONE diagnostic that BYPASSES OpenSignals.

Connects DIRECTLY to a biosignalsplux hub over Bluetooth using the
official PLUX Python API (`import plux`). Reads ADC values straight
from the device firmware and prints them. If this works while OpenSignals
LSL doesn't, you have definitive proof OpenSignals is the broken layer.

Setup (one-time):
  1. Download PLUX's Python API from
       https://github.com/pluxbiosignals/python-samples
     Choose the build matching your Python version (e.g. PLUX-API-Python3-310
     for Python 3.10 on Windows).
  2. Copy the `plux.pyd` (and any DLLs that come with it) into this folder
     OR into your venv's `site-packages` directory.
  3. Verify install:
       python -c "import plux; print(plux.__file__)"

Usage:
    env\\Scripts\\activate.bat
    python scripts\\plux_direct_inspect.py XX:XX:XX:XX:XX:XX

The MAC address comes from OpenSignals (or your Bluetooth devices list).
For example, from your earlier OpenSignals header: 00:07:80:0F:31:9C

Press Ctrl+C to stop.
"""

from __future__ import annotations
import sys
import os
import time

# Make src/ importable so we can use the same ADC->physical converters.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))


def _try_import_plux():
    """Import plux, with a clear error message if it isn't installed."""
    try:
        import plux  # noqa: F401
        return plux
    except ImportError as e:
        print("=" * 70)
        print("  ERROR: PLUX Python API is not installed.")
        print("=" * 70)
        print()
        print(f"  ImportError: {e}")
        print()
        print("  To install:")
        print("    1. Download from")
        print("       https://github.com/pluxbiosignals/python-samples")
        print("    2. Pick the folder matching your Python version (run")
        print("       `python --version` to check). For Python 3.10 on")
        print("       Windows you want PLUX-API-Python3-310 (or 311, etc).")
        print("    3. Copy plux.pyd plus any included DLLs into:")
        print(f"         {os.path.dirname(sys.executable)}")
        print("       or simply alongside this script.")
        print("    4. Re-run this script.")
        print()
        return None


def main():
    plux = _try_import_plux()
    if plux is None:
        return 1

    if len(sys.argv) < 2:
        print("Usage:  python scripts/plux_direct_inspect.py <MAC_ADDRESS>")
        print("Example: python scripts/plux_direct_inspect.py 00:07:80:0F:31:9C")
        return 1

    mac = sys.argv[1].strip()
    fs_hz = 200
    n_bits = 16
    channels_mask = 0x03  # bit 0 = CH1, bit 1 = CH2 -> both channels

    print("=" * 70)
    print(f"  PLUX DIRECT INSPECTOR -- talking to {mac} via Bluetooth")
    print("=" * 70)
    print(f"  Sampling rate : {fs_hz} Hz")
    print(f"  Resolution    : {n_bits} bits")
    print(f"  Channels      : CH1, CH2 (mask 0x{channels_mask:02X})")
    print()
    print("  Make sure OpenSignals is NOT running -- the device can only be")
    print("  connected to one client at a time over Bluetooth.")
    print()

    # PLUX's official sample (`OneDeviceAcquisitionExample.py`) subclasses
    # plux.SignalsDev and overrides onRawFrame. The base class drives the
    # loop; onRawFrame is called for every incoming sample frame.
    class Inspector(plux.SignalsDev):
        def __init__(self, address):
            super().__init__(address)
            self._sample_count = 0
            self._last_print = time.time()
            self._accum = []

        def onRawFrame(self, n_seq, data):
            # data is a list/tuple of raw ADC integers, one per active channel
            self._sample_count += 1
            self._accum.append(tuple(data))

            now = time.time()
            if now - self._last_print >= 1.0:
                import numpy as np
                arr = np.asarray(self._accum, dtype=float)
                from data_sources import adc_to_eda_uS, adc_to_ecg_mV
                print(f"  --- 1 second tick ({arr.shape[0]} samples) ---")
                for ch_idx in range(arr.shape[1]):
                    col = arr[:, ch_idx]
                    m = col.mean()
                    s = col.std()
                    label = f"CH{ch_idx + 1}"
                    eda_uS = adc_to_eda_uS(m)
                    ecg_mV = adc_to_ecg_mV(m)
                    print(f"    {label}  ADC mean={m:10.1f}  std={s:9.3f}"
                          f"  as-EDA={eda_uS:8.4f} uS"
                          f"  as-ECG={ecg_mV:+8.4f} mV")
                print()
                self._last_print = now
                self._accum.clear()
            return False  # keep streaming

    print(f"[plux_direct] Connecting to {mac} ...")
    try:
        dev = Inspector(mac)
    except Exception as e:
        print(f"[plux_direct] Connection failed: {type(e).__name__}: {e}")
        print()
        print("  Common causes:")
        print("  - OpenSignals is still running and holding the device.")
        print("    Close OpenSignals completely (also kill it in Task Manager")
        print("    if needed) and try again.")
        print("  - MAC address is wrong. Verify in Windows Bluetooth settings.")
        print("  - Device is off or out of range. Power-cycle the hub.")
        return 1

    print(f"[plux_direct] Connected. Starting acquisition at {fs_hz} Hz...")
    try:
        dev.start(fs_hz, channels_mask, n_bits)
        print("[plux_direct] Streaming. Ctrl+C to stop.\n")
        dev.loop()
    except KeyboardInterrupt:
        print("\n[plux_direct] Stopped by user.")
    finally:
        try:
            dev.stop()
            dev.close()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
