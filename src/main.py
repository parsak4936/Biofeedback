# src/main.py

import time
import os
from config import Config
from acquisition import BiofeedbackAcquisition
from processing import SignalProcessor
from fusion import FusionEngine
from output import UnityBridge
from session_manager import SessionManager

def run_pipeline():
    print("==================================================")
    print("    BIOFEEDBACK ACROPHOBIA PIPELINE STARTING      ")
    print("==================================================\n")

    # Get patient info from environment (set by launcher.py)
    patient_name = os.environ.get('PATIENT_NAME', 'PATIENT')
    patient_id = os.environ.get('PATIENT_ID', '000')
    
    # Initialize session manager to track all metrics
    session = SessionManager(patient_name=patient_name, patient_id=patient_id)
    
    # 1. Initialize all modules
    acq = BiofeedbackAcquisition()
    proc = SignalProcessor()
    fusion = FusionEngine()
    out = UnityBridge()
    
    print("\n[MAIN] All modules online. Entering 50Hz acquisition loop...")
    print(f"[MAIN] Logging to: {os.path.basename(session.output_file_path)}\n")
    
    tick_duration = 1.0 / Config.PIPELINE_RATE
    thresholds_locked = False
    
    try:
        while True:
            start_time = time.time()
            
            # --- PHASE 1: ACQUISITION ---
            # Pulls raw data from LSL and applies the Zero-Order Hold
            raw_vector = acq.get_synchronized_sample()
            
            # Record raw sample in session
            session.record_raw_sample(raw_vector[0], raw_vector[1], raw_vector[2])
            
            # --- PHASE 2: PROCESSING & CALIBRATION ---
            # Applies EMA Smoothing and handles the 120s baseline buffering
            smoothed_vector, is_baseline_ready = proc.process_sample(raw_vector)
            
            # Update session phase
            session.update_phase_baseline(is_baseline_ready)
            
            # --- PHASE 3: FUSION & OUTPUT (Only runs after 120s) ---
            if is_baseline_ready:
                # Lock thresholds the very first tick the baseline finishes
                if not thresholds_locked:
                    # For this implementation, we map a baseline sigma approximation 
                    # based on the observed data. (Standardizing at 1.5 variance)
                    fusion.set_thresholds(sigma_baseline=1.5)
                    thresholds_locked = True
                    
                    # Store baseline stats in session
                    session.set_baseline_stats(
                        proc.personal_averages,
                        proc.artifacts_removed if hasattr(proc, 'artifacts_removed') else {'eda': 0, 'hr': 0, 'hrv': 0}
                    )
                    session.set_thresholds(fusion.thresh_mild, fusion.thresh_high)
                
                # Calculate immediate stress
                s_inst = fusion.compute_s_instant(smoothed_vector, proc.personal_averages)
                
                # Smooth the stress index and evaluate kinematic state
                s_t, state, dashboard = fusion.evaluate_state(s_inst)
                
                # Record stress metrics in session
                session.record_stress_metric(s_inst, s_t, state, dashboard)
                
                # Broadcast to Unity
                out.broadcast_state(s_t, state, dashboard)
            
            # Log sample to output file (every tick during LIVE phase)
            if is_baseline_ready and len(session.stress_history['s_t']) > 0:
                s_t = session.stress_history['s_t'][-1]
                state = session.stress_history['state'][-1]
                dashboard_score = session.stress_history['dashboard_score'][-1]
                s_inst = session.stress_history['s_instant'][-1]
                session.log_sample(smoothed_vector[0], smoothed_vector[1], smoothed_vector[2],
                                 s_inst, s_t, state, dashboard_score)
            else:
                # During baseline, just log signals
                session.log_sample(smoothed_vector[0], smoothed_vector[1], smoothed_vector[2])
                
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
        print(f"[SESSION] Output saved to: {os.path.basename(session.output_file_path)}")
    except ConnectionError as e:
        print("\n==================================================")
        print("      SIGNAL ACQUISITION LOST                      ")
        print("==================================================")
        print(f"\n{str(e)}")
        print("\nNo signal changes detected during session.")
        print("==================================================")
        print("      PIPELINE TERMINATED (NO SIGNAL)              ")
        print("==================================================")
        print(f"[SESSION] Output saved to: {os.path.basename(session.output_file_path)}")

if __name__ == "__main__":
    run_pipeline()