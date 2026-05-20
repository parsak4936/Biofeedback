# src/main.py

import time
from config import Config
from acquisition import BiofeedbackAcquisition
from processing import SignalProcessor
from fusion import FusionEngine
from output import UnityBridge

def run_pipeline():
    print("==================================================")
    print("    BIOFEEDBACK ACROPHOBIA PIPELINE STARTING      ")
    print("==================================================\n")

    # 1. Initialize all modules
    acq = BiofeedbackAcquisition()
    proc = SignalProcessor()
    fusion = FusionEngine()
    out = UnityBridge()
    
    print("\n[MAIN] All modules online. Entering 50Hz acquisition loop...")
    
    tick_duration = 1.0 / Config.PIPELINE_RATE
    thresholds_locked = False
    
    try:
        while True:
            start_time = time.time()
            
            # --- PHASE 1: ACQUISITION ---
            # Pulls raw data from LSL and applies the Zero-Order Hold
            raw_vector = acq.get_synchronized_sample()
            
            # --- PHASE 2: PROCESSING & CALIBRATION ---
            # Applies EMA Smoothing and handles the 120s baseline buffering
            smoothed_vector, is_baseline_ready = proc.process_sample(raw_vector)
            
            # --- PHASE 3: FUSION & OUTPUT (Only runs after 120s) ---
            if is_baseline_ready:
                # Lock thresholds the very first tick the baseline finishes
                if not thresholds_locked:
                    # For this implementation, we map a baseline sigma approximation 
                    # based on the observed data. (Standardizing at 1.5 variance)
                    fusion.set_thresholds(sigma_baseline=1.5)
                    thresholds_locked = True
                
                # Calculate immediate stress
                s_inst = fusion.compute_s_instant(smoothed_vector, proc.personal_averages)
                
                # Smooth the stress index and evaluate kinematic state
                s_t, state, dashboard = fusion.evaluate_state(s_inst)
                
                # Broadcast to Unity
                out.broadcast_state(s_t, state, dashboard)
                
            # --- PHASE 4: STRICT LATENCY ENFORCEMENT ---
            elapsed = time.time() - start_time
            sleep_time = tick_duration - elapsed
            
            if sleep_time > 0:
                time.sleep(sleep_time)
            # We don't print frame drops here since acquisition.py handles tracking
                
    except KeyboardInterrupt:
        print("\n==================================================")
        print("      PIPELINE TERMINATED BY OPERATOR             ")
        print("==================================================")

if __name__ == "__main__":
    run_pipeline()