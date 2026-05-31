# src/streamer.py
"""
Data streamer subprocess.

Tiny wrapper that creates a DataSource (mock or real PLUX, chosen by
Config.DATA_SOURCE) and drives its `get_next_sample()` in a loop. The
data source itself handles the LSL outlet and the per-sample pacing,
so this file is just the loop. Runs forever until Ctrl+C.

In mock mode, the data source publishes a 3-channel LSL stream
(`Config.STREAM_NAME` — typically "OpenSignals") at the recording file's
native rate, plus a 1-channel ECG side stream for the dashboard's
waveform display. In real-PLUX mode, OpenSignals software is the
publisher; this script just consumes from it and the upstream stream
publish is done by the PLUX/OpenSignals side.

The streamer must be started before main.py so the OpenSignals LSL
stream exists for acquisition to subscribe to. The launcher handles
this ordering.
"""

import time
from data_sources import DataSourceFactory
from config import Config


def main():
    print("=" * 50)
    print("  BIOFEEDBACK DATA STREAMER (UNIVERSAL)           ")
    print("=" * 50)
    print(f"\n[STREAMER] Configuration: DATA_SOURCE = '{Config.DATA_SOURCE}'")
    print(f"[STREAMER] Broadcasting on LSL: '{Config.STREAM_NAME}'\n")
    
    try:
        # Create data source (auto-selects mock or real based on config)
        source = DataSourceFactory.create()
        
        print("[STREAMER] Ready to stream. Press Ctrl+C to stop.\n")
        
        # Main streaming loop
        try:
            while True:
                # Get next sample from the selected source
                sample = source.get_next_sample()
                
        except KeyboardInterrupt:
            print("\n[STREAMER] Streaming terminated by user.")
            source.cleanup()
            
    except Exception as e:
        print(f"\n[ERROR] {str(e)}")
        print("\n[STREAMER] Startup failed. Check configuration and hardware.")
        return 1
    
    return 0


if __name__ == "__main__":
    exit(main())
