# src/streamer.py
"""
Universal Data Streamer

Replaces both hardware_mock.py and mock_streamer.py
Uses DataSourceFactory to automatically select mock or real hardware based on config
Broadcasts selected data source on LSL at 1000Hz
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
