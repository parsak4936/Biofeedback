# launcher.py
"""
BIOFEEDBACK VIRTUAL CLINIC - Single Entry Point

This is the ONLY file a casual user needs to run.

Configuration:
  - Change Config.DATA_SOURCE in src/config.py to switch between mock/real hardware
  - Default is 'real_plux' (real PLUX device)
  - Change to 'mock' to use synthetic test data

Startup Sequence:
  1. streamer.py - Data acquisition (mock or real hardware)
  2. main.py - Signal processing & baseline calibration
  3. dashboard.py - Clinical visualization

All systems shut down automatically when you close this launcher.
"""

import subprocess
import time
import sys
import os
from pathlib import Path


def launch_system():
    """Launch the complete biofeedback system."""
    
    print("\n" + "=" * 60)
    print("  BIOFEEDBACK VIRTUAL CLINIC - LAUNCHING SYSTEM           ")
    print("=" * 60 + "\n")
    
    # ============================================
    # PATIENT INFORMATION
    # ============================================
    print("[SYSTEM] Patient Information")
    print("-" * 60)
    patient_name = input("Enter patient name: ").strip()
    patient_id = input("Enter patient ID (or press Enter to skip): ").strip()
    
    if not patient_name:
        patient_name = "UNNAMED"
    if not patient_id:
        patient_id = "NO_ID"
    
    patient_info = f"{patient_name}_{patient_id}"
    print(f"[SYSTEM] ✓ Patient: {patient_info}\n")
    
    # Get paths
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')
    config_file = os.path.join(src_dir, 'config.py')
    
    # Read current config to show user
    try:
        with open(config_file, 'r') as f:
            content = f.read()
            if "DATA_SOURCE = 'mock'" in content:
                current_source = "MOCK (Synthetic Test Data)"
            elif "DATA_SOURCE = 'real_plux'" in content:
                current_source = "REAL PLUX DEVICE"
            else:
                current_source = "UNKNOWN"
    except:
        current_source = "UNKNOWN"
    
    print(f"[CONFIG] Active Data Source: {current_source}")
    print(f"[CONFIG] To change: Edit src/config.py and set DATA_SOURCE\n")
    
    # Startup sequence
    processes = []
    
    try:
        # 1. Data Acquisition (Mock or Real Hardware)
        print("[LAUNCHER] ① Starting Data Acquisition Layer...")
        print("[LAUNCHER]    (Initializing data source...)\n")
        p_streamer = subprocess.Popen(
            [sys.executable, "streamer.py"], 
            cwd=src_dir,
            universal_newlines=True
        )
        processes.append(p_streamer)
        time.sleep(2)  # Give LSL network time to establish
        
        # 2. Processing Pipeline
        print("[LAUNCHER] ② Starting Signal Processing Pipeline...")
        print(f"[LAUNCHER]    (Patient: {patient_info})")
        print("[LAUNCHER]    (Baseline calibration: 120 seconds)\n")
        
        # Pass patient info as environment variable
        env = os.environ.copy()
        env['PATIENT_NAME'] = patient_name
        env['PATIENT_ID'] = patient_id
        
        p_main = subprocess.Popen(
            [sys.executable, "main.py"], 
            cwd=src_dir,
            universal_newlines=True,
            env=env
        )
        processes.append(p_main)
        time.sleep(2)
        
        # 3. Clinical Dashboard
        print("[LAUNCHER] ③ Starting Clinical Dashboard...")
        print("[LAUNCHER]    (Connecting to data streams...)\n")
        p_dash = subprocess.Popen(
            [sys.executable, "dashboard.py"], 
            cwd=src_dir,
            env=env
        )
        processes.append(p_dash)
        
        print("=" * 60)
        print(f"  ✓ SYSTEM ONLINE - Patient: {patient_info}                ")
        print("=" * 60)
        print("\nDashboard is now displaying patient metrics in real-time.")
        print("Session data will be saved to: data/session_*.csv")
        print("\nTo shutdown: Close the Dashboard window or press Ctrl+C below.\n")
        
        # Keep launcher alive
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n\n[LAUNCHER] Shutting down all subsystems...")
        
        # Terminate processes in reverse order
        for p in reversed(processes):
            try:
                p.terminate()
                p.wait(timeout=2)
            except:
                p.kill()
        
        print("[LAUNCHER] All systems offline.")
        print("[LAUNCHER] Goodbye.\n")
        return 0
    
    except Exception as e:
        print(f"\n[ERROR] System startup failed: {str(e)}")
        print("\nTroubleshooting:")
        print("  - Ensure Python 3.8+ is installed")
        print("  - Check PyQt5, pyqtgraph, numpy, pylsl in requirements.txt")
        print("  - If using real PLUX: ensure device is powered and Bluetooth paired")
        print("  - Check that no other instances are running\n")
        
        # Kill any partial startup
        for p in processes:
            try:
                p.terminate()
            except:
                pass
        
        return 1


if __name__ == "__main__":
    exit_code = launch_system()