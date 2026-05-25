# src/session_manager.py
"""
Session Manager - Tracks all session-level state

Monitors:
- Patient information (name, ID)
- Current phase (baseline buffering vs live therapy)
- Signal history (for charts)
- Baseline values and cleanup statistics
- Stress metrics and state history
- Session timing and statistics
"""

import time
import os
from datetime import datetime
from config import Config


class SessionManager:
    """Centralized session state tracking."""
    
    def __init__(self, patient_name: str = "PATIENT", patient_id: str = "000"):
        """
        Initialize a new session.
        
        Args:
            patient_name: Patient name or identifier
            patient_id: Patient ID number
        """
        self.patient_name = patient_name
        self.patient_id = patient_id
        self.patient_info = f"{patient_name}_{patient_id}"
        
        self.session_start_time = time.time()
        self.session_start_datetime = datetime.now()
        self.session_timestamp = self.session_start_datetime.strftime("%Y%m%d_%H%M%S")
        
        # ============================================
        # PHASE TRACKING
        # ============================================
        self.phase = "BASELINE"  # "BASELINE" or "LIVE"
        self.baseline_end_time = None
        self.baseline_ticks_remaining = int(Config.BASELINE_SEC * Config.PIPELINE_RATE)
        
        # ============================================
        # SIGNAL HISTORY (for charting)
        # ============================================
        # Store raw samples for dashboard updates
        self.signal_history = {
            'eda': [],
            'hr': [],
            'hrv': [],
            'timestamps': []
        }
        self.max_history_length = int(Config.PIPELINE_RATE * 60)  # Keep last 60 seconds
        
        # ============================================
        # BASELINE STATISTICS
        # ============================================
        self.personal_baselines = None
        self.baseline_buffers = None
        self.artifacts_removed = {
            'eda': 0,
            'hr': 0,
            'hrv': 0
        }
        
        # ============================================
        # STRESS METRICS HISTORY
        # ============================================
        self.stress_history = {
            's_instant': [],      # Raw instantaneous stress
            's_t': [],             # Smoothed stress (1-second window)
            'state': [],           # calm/stressed/ultra_stressed
            'dashboard_score': []  # 0-100 operator display
        }
        
        # ============================================
        # THRESHOLDS (locked after baseline)
        # ============================================
        self.thresh_mild = None
        self.thresh_high = None
        
        # ============================================
        # SESSION OUTPUT FILE
        # ============================================
        self._setup_output_file()
        
        print(f"\n[SESSION] ╔{'═' * 56}╗")
        print(f"[SESSION] ║ New Session Started")
        print(f"[SESSION] ║ Patient: {self.patient_info:<45} ║")
        print(f"[SESSION] ║ Start Time: {self.session_start_datetime.strftime('%Y-%m-%d %H:%M:%S'):<29} ║")
        print(f"[SESSION] ║ Phase: {self.phase:<50} ║")
        print(f"[SESSION] ║ Baseline duration: {Config.BASELINE_SEC}s ({self.baseline_ticks_remaining} ticks)          ║")
        print(f"[SESSION] ╚{'═' * 56}╝\n")
    
    def _setup_output_file(self):
        """Create output CSV file for this session."""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        data_dir = os.path.join(project_root, 'data')
        os.makedirs(data_dir, exist_ok=True)
        
        # Filename format: session_YYYYMMDD_HHMMSS_PatientName_PatientID.csv
        filename = f"session_{self.session_timestamp}_{self.patient_info}.csv"
        self.output_file_path = os.path.join(data_dir, filename)
        
        # Write CSV header
        with open(self.output_file_path, 'w', newline='') as f:
            f.write("timestamp,phase,patient_name,patient_id,eda,hr,hrv,s_instant,s_t,state,dashboard_score\n")
        
        print(f"[SESSION] Output file: {filename}")
    
    def log_sample(self, eda: float, hr: float, hrv: float, s_instant: float = None, 
                   s_t: float = None, state: str = None, dashboard_score: float = None):
        """Log a complete sample to the output CSV file."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        with open(self.output_file_path, 'a', newline='') as f:
            f.write(f"{timestamp},{self.phase},{self.patient_name},{self.patient_id},"
                   f"{eda:.4f},{hr:.4f},{hrv:.4f},"
                   f"{s_instant if s_instant is not None else 0.0:.4f},"
                   f"{s_t if s_t is not None else 0.0:.4f},"
                   f"{state if state else 'unknown'},"
                   f"{dashboard_score if dashboard_score is not None else 0.0:.2f}\n")
    
    def record_raw_sample(self, eda: float, hr: float, hrv: float):
        """Record a raw signal sample from acquisition."""
        timestamp = time.time() - self.session_start_time
        
        self.signal_history['eda'].append(eda)
        self.signal_history['hr'].append(hr)
        self.signal_history['hrv'].append(hrv)
        self.signal_history['timestamps'].append(timestamp)
        
        # Keep only recent history for dashboard
        if len(self.signal_history['eda']) > self.max_history_length:
            self.signal_history['eda'].pop(0)
            self.signal_history['hr'].pop(0)
            self.signal_history['hrv'].pop(0)
            self.signal_history['timestamps'].pop(0)
    
    def record_stress_metric(self, s_instant: float, s_t: float, state: str, dashboard_score: float):
        """Record computed stress metrics."""
        self.stress_history['s_instant'].append(s_instant)
        self.stress_history['s_t'].append(s_t)
        self.stress_history['state'].append(state)
        self.stress_history['dashboard_score'].append(dashboard_score)
    
    def update_phase_baseline(self, is_baseline_complete: bool):
        """Called when processing indicates baseline is complete."""
        if is_baseline_complete and self.phase == "BASELINE":
            self.phase = "LIVE"
            self.baseline_end_time = time.time()
            elapsed = self.baseline_end_time - self.session_start_time
            print(f"\n[SESSION] ✓ BASELINE COMPLETE at {elapsed:.1f}s")
            print(f"[SESSION] Phase transition: BASELINE → LIVE\n")
    
    def set_baseline_stats(self, personal_baselines: dict, artifacts_removed: dict):
        """Store baseline statistics after 3-sigma cleaning."""
        self.personal_baselines = personal_baselines
        self.artifacts_removed = artifacts_removed
        
        print(f"[SESSION] Personal Baselines:")
        print(f"  → EDA:  {personal_baselines['eda']:.2f} μS")
        print(f"  → HR:   {personal_baselines['hr']:.2f} BPM")
        print(f"  → HRV:  {personal_baselines['hrv']:.2f} ms")
    
    def set_thresholds(self, thresh_mild: float, thresh_high: float):
        """Store stress classification thresholds."""
        self.thresh_mild = thresh_mild
        self.thresh_high = thresh_high
    
    def get_session_duration_sec(self) -> float:
        """Elapsed time since session start."""
        return time.time() - self.session_start_time
    
    def get_session_duration_str(self) -> str:
        """Formatted session duration MM:SS."""
        elapsed = self.get_session_duration_sec()
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        return f"{minutes:02d}:{seconds:02d}"
    
    def get_current_state_summary(self) -> dict:
        """Return current session state as dictionary for dashboard."""
        return {
            'patient_name': self.patient_name,
            'patient_id': self.patient_id,
            'patient_info': self.patient_info,
            'phase': self.phase,
            'duration': self.get_session_duration_str(),
            'duration_sec': self.get_session_duration_sec(),
            'baseline_complete': (self.phase == "LIVE"),
            'personal_baselines': self.personal_baselines,
            'thresh_mild': self.thresh_mild,
            'thresh_high': self.thresh_high,
            'artifacts_removed': self.artifacts_removed,
            'signal_count': len(self.signal_history['eda']),
            'stress_events_count': len(self.stress_history['s_t']),
            'current_state': self.stress_history['state'][-1] if self.stress_history['state'] else None,
            'current_s_t': self.stress_history['s_t'][-1] if self.stress_history['s_t'] else None,
            'current_dashboard_score': self.stress_history['dashboard_score'][-1] if self.stress_history['dashboard_score'] else None,
            'output_file': self.output_file_path
        }
    
    def record_raw_sample(self, eda: float, hr: float, hrv: float):
        """Record a raw signal sample from acquisition."""
        timestamp = time.time() - self.session_start_time
        
        self.signal_history['eda'].append(eda)
        self.signal_history['hr'].append(hr)
        self.signal_history['hrv'].append(hrv)
        self.signal_history['timestamps'].append(timestamp)
        
        # Keep only recent history for dashboard
        if len(self.signal_history['eda']) > self.max_history_length:
            self.signal_history['eda'].pop(0)
            self.signal_history['hr'].pop(0)
            self.signal_history['hrv'].pop(0)
            self.signal_history['timestamps'].pop(0)
    
    def record_stress_metric(self, s_instant: float, s_t: float, state: str, dashboard_score: float):
        """Record computed stress metrics."""
        self.stress_history['s_instant'].append(s_instant)
        self.stress_history['s_t'].append(s_t)
        self.stress_history['state'].append(state)
        self.stress_history['dashboard_score'].append(dashboard_score)
    
    def update_phase_baseline(self, is_baseline_complete: bool):
        """Called when processing indicates baseline is complete."""
        if is_baseline_complete and self.phase == "BASELINE":
            self.phase = "LIVE"
            self.baseline_end_time = time.time()
            elapsed = self.baseline_end_time - self.session_start_time
            print(f"\n[SESSION] ✓ BASELINE COMPLETE at {elapsed:.1f}s")
            print(f"[SESSION] Phase transition: BASELINE → LIVE\n")
    
    def set_baseline_stats(self, personal_baselines: dict, artifacts_removed: dict):
        """Store baseline statistics after 3-sigma cleaning."""
        self.personal_baselines = personal_baselines
        self.artifacts_removed = artifacts_removed
        
        print(f"[SESSION] Personal Baselines:")
        print(f"  → EDA:  {personal_baselines['eda']:.2f} μS")
        print(f"  → HR:   {personal_baselines['hr']:.2f} BPM")
        print(f"  → HRV:  {personal_baselines['hrv']:.2f} ms")
    
    def set_thresholds(self, thresh_mild: float, thresh_high: float):
        """Store stress classification thresholds."""
        self.thresh_mild = thresh_mild
        self.thresh_high = thresh_high
    
    def get_session_duration_sec(self) -> float:
        """Elapsed time since session start."""
        return time.time() - self.session_start_time
    
    def get_session_duration_str(self) -> str:
        """Formatted session duration MM:SS."""
        elapsed = self.get_session_duration_sec()
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        return f"{minutes:02d}:{seconds:02d}"
    
    def get_current_state_summary(self) -> dict:
        """Return current session state as dictionary for dashboard."""
        return {
            'patient_id': self.patient_id,
            'phase': self.phase,
            'duration': self.get_session_duration_str(),
            'duration_sec': self.get_session_duration_sec(),
            'baseline_complete': (self.phase == "LIVE"),
            'personal_baselines': self.personal_baselines,
            'thresh_mild': self.thresh_mild,
            'thresh_high': self.thresh_high,
            'artifacts_removed': self.artifacts_removed,
            'signal_count': len(self.signal_history['eda']),
            'stress_events_count': len(self.stress_history['s_t']),
            'current_state': self.stress_history['state'][-1] if self.stress_history['state'] else None,
            'current_s_t': self.stress_history['s_t'][-1] if self.stress_history['s_t'] else None,
            'current_dashboard_score': self.stress_history['dashboard_score'][-1] if self.stress_history['dashboard_score'] else None
        }


if __name__ == "__main__":
    # Quick test of session manager
    print("\n=== SESSION MANAGER TEST ===\n")
    
    session = SessionManager("DEMO_USER")
    
    # Simulate some data
    for i in range(10):
        session.record_raw_sample(5.0 + i*0.1, 75.0 + i*0.5, 40.0)
        session.record_stress_metric(0.5, 0.3, "calm", 25.0)
    
    session.set_baseline_stats(
        {'eda': 5.0, 'hr': 75.0, 'hrv': 40.0},
        {'eda': 2, 'hr': 0, 'hrv': 1}
    )
    session.set_thresholds(1.33, 2.28)
    
    # Print summary
    summary = session.get_current_state_summary()
    print("\nCurrent State Summary:")
    for key, value in summary.items():
        print(f"  {key}: {value}")
