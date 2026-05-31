# src/session_manager.py
"""
Per-session bookkeeping and output writing.

One SessionManager instance per pipeline run. Responsibilities:

  - Reads the patient intake JSON written by the launcher (path comes
    via the PATIENT_INTAKE_JSON env var) so every CSV row and the
    baseline JSON include the full demographics: first/last name, ID,
    gender, session date, session number.

  - Writes data/session_<timestamp>_<patient_id>.csv, the clinical
    record. Header is set at construction time; rows are appended per
    tick by main.py via log_sample(). 21 columns covering patient info,
    phase, signals, deltas, stress index, state, score, and artifact
    counts. A held file handle is used so the per-tick CSV write is
    cheap; opening + closing every tick was visibly slowing the loop.
    Rotated to a new file on Start Live after a Stop, so each live run
    lands in its own CSV.

  - Writes data/baseline_<timestamp>_<patient_id>.json once at the
    moment the 120-second baseline finishes (via
    write_baseline_capture()). Contains the personal averages,
    sigma_baseline, the locked thresholds, the bonus HRV metrics, the
    artifact counts, the source recording label, and a snapshot of
    every pipeline constant in force, so a baseline can be reproduced
    exactly later.

  - Writes data/live_log_<timestamp>_<patient_id>.txt during LIVE: one
    line per second with smoothed signals, deltas, S_t, and state. A
    human-readable transcript convenient for terminal review and
    pasting into clinical notes.

  - Tracks per-session history (signal_history, stress_history) for
    diagnostic / review use.

  - Exposes delete_session_csv / delete_baseline_json /
    delete_patient_json / delete_live_log helpers for the shutdown
    path, which closes open handles before the unlink so Windows
    allows it.

There is some defensive overhead here (the get_current_state_summary
method and a few duplicated method definitions from an earlier
version) that the current state-machine architecture does not need,
but it hurts nothing.
"""

import json
import time
import os
from datetime import datetime
from config import Config


def _load_patient_from_env() -> dict:
    """
    Read the intake JSON file written by the launcher. If the env var isn't
    set (e.g. running a module directly without the launcher), return a
    minimal dict so tests still work.
    """
    path = os.environ.get('PATIENT_INTAKE_JSON')
    if path and os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        'first_name':     os.environ.get('PATIENT_NAME', 'PATIENT'),
        'last_name':      '',
        'patient_id':     os.environ.get('PATIENT_ID', '000'),
        'gender':         '',
        'session_date':   datetime.now().strftime('%Y-%m-%d'),
        'session_number': 1,
    }


class SessionManager:
    """Centralized session state tracking."""

    def __init__(self, patient_name: str = "PATIENT", patient_id: str = "000"):
        """
        Initialize a new session.

        The full patient record (first name, last name, ID, gender, date,
        session number) is loaded from the intake JSON the launcher wrote,
        via the PATIENT_INTAKE_JSON env var. The positional args are kept for
        backwards compatibility but are overridden by the JSON if present.
        """
        self.patient = _load_patient_from_env()
        # Allow positional args to override (they're how main.py currently calls us).
        if patient_name != "PATIENT":
            self.patient['first_name'] = patient_name
        if patient_id != "000":
            self.patient['patient_id'] = patient_id

        self.patient_name = self.patient['first_name']
        self.patient_id = self.patient['patient_id']
        self.patient_info = f"{self.patient_name}_{self.patient_id}"
        
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
        
        bar = '=' * 58
        print(f"\n[SESSION] {bar}")
        print(f"[SESSION] New session started")
        print(f"[SESSION]   Patient            : {self.patient_info}")
        print(f"[SESSION]   Start time         : "
              f"{self.session_start_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"[SESSION]   Phase              : {self.phase}")
        print(f"[SESSION]   Baseline duration  : "
              f"{Config.BASELINE_SEC}s ({self.baseline_ticks_remaining} ticks)")
        print(f"[SESSION] {bar}\n")
    
    def _setup_output_file(self):
        """Create output CSV file for this session."""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        data_dir = os.path.join(project_root, 'data')
        os.makedirs(data_dir, exist_ok=True)

        # Filename format: session_YYYYMMDD_HHMMSS_PatientName_PatientID.csv
        filename = f"session_{self.session_timestamp}_{self.patient_info}.csv"
        self.output_file_path = os.path.join(data_dir, filename)
        # Baseline JSON path is set when write_baseline_capture() succeeds;
        # we hold the slot so shutdown cleanup can find the file.
        self.baseline_capture_path = None

        # Keep one open handle for the session CSV's lifetime. Opening +
        # closing the file every tick (the old behavior) cost a few ms
        # each on Windows — enough at 50 Hz to drag the main loop down to
        # 40-48 Hz, which in turn meant the 6000-sample baseline target
        # never quite got hit at the 2-minute mark. With a held handle
        # the per-tick CSV write is essentially free.
        self._csv_handle = None
        self._open_csv(mode='w', write_header=True)

        print(f"[SESSION] Output file: {filename}")

    _CSV_HEADER = ("timestamp,phase,"
                   "patient_first_name,patient_last_name,patient_id,"
                   "gender,session_date,session_number,"
                   "eda,hr,hrv,"
                   "delta_eda,delta_hr,delta_hrv,"
                   "s_instant,s_t,state,dashboard_score,"
                   "artifacts_eda,artifacts_hr,artifacts_hrv\n")

    def _open_csv(self, mode: str, write_header: bool):
        """(Re-)open the session CSV. Used at __init__ ('w' + header),
        after flush_live_data ('w' + header), and after delete_session_csv
        if anyone wants to keep writing (we don't, but defensive)."""
        self._close_csv()
        self._csv_handle = open(self.output_file_path, mode, newline='', buffering=1)
        if write_header:
            self._csv_handle.write(self._CSV_HEADER)
            self._csv_handle.flush()

    def _close_csv(self):
        try:
            if self._csv_handle is not None:
                self._csv_handle.flush()
                self._csv_handle.close()
        except Exception:
            pass
        self._csv_handle = None

    def rotate_session_csv(self):
        """Close the current session CSV and open a fresh one with a new
        timestamp. Called when the operator clicks Start Live a second
        time after a Stop — so each live session lands in its own file
        rather than concatenating into the first one."""
        self._close_csv()
        self.session_start_datetime = datetime.now()
        self.session_timestamp = self.session_start_datetime.strftime("%Y%m%d_%H%M%S")
        filename = f"session_{self.session_timestamp}_{self.patient_info}.csv"
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        data_dir = os.path.join(project_root, 'data')
        self.output_file_path = os.path.join(data_dir, filename)
        self._open_csv(mode='w', write_header=True)
        # The previous run's stress-history is no longer relevant to the
        # new file; drop it so print_session_summary reflects only the
        # session that is about to begin.
        self.stress_history = {
            's_instant': [], 's_t': [], 'state': [], 'dashboard_score': [],
        }
        print(f"[SESSION] Rotated to new session CSV: {filename}")

    def flush_live_data(self):
        """Drop in-memory live-phase stress history and rewrite the session
        CSV from scratch with only its header. Called on LIVE_RESTART so the
        operator's next live run isn't contaminated by the previous one.
        Baseline JSON is left untouched."""
        self.stress_history = {
            's_instant': [], 's_t': [], 'state': [], 'dashboard_score': [],
        }
        # Truncate the CSV via _open_csv (which closes the current handle,
        # reopens in 'w' mode, rewrites the header). Writing continues
        # through the new handle on the next log_sample call.
        self._open_csv(mode='w', write_header=True)
        print("[SESSION] flush_live_data: stress history and session CSV reset.")

    def delete_session_csv(self):
        """Remove the per-tick session CSV from disk. Closes the held
        handle first so Windows allows the delete."""
        self._close_csv()
        try:
            if self.output_file_path and os.path.exists(self.output_file_path):
                os.remove(self.output_file_path)
                print(f"[SESSION] Deleted: {os.path.basename(self.output_file_path)}")
        except OSError as e:
            print(f"[SESSION] WARN: could not delete session CSV: {e}")

    def delete_baseline_json(self):
        """Remove the baseline JSON capture from disk, if one was written."""
        try:
            if self.baseline_capture_path and os.path.exists(self.baseline_capture_path):
                os.remove(self.baseline_capture_path)
                print(f"[SESSION] Deleted: {os.path.basename(self.baseline_capture_path)}")
        except OSError as e:
            print(f"[SESSION] WARN: could not delete baseline JSON: {e}")

    def log_live_line(self, line: str):
        """Append a human-readable line to the per-session live log
        (data/live_log_<ts>_<patient>.txt) AND echo it to stdout.

        Opened lazily on first call so an aborted baseline doesn't leave
        an empty live log on disk. Each line carries a wall-clock
        timestamp so the file is a clean, paste-friendly transcript of
        what the operator saw during a session — useful in clinical
        notes and downstream research without parsing the CSV."""
        try:
            if not hasattr(self, '_live_log_handle') or self._live_log_handle is None:
                current_dir = os.path.dirname(os.path.abspath(__file__))
                project_root = os.path.dirname(current_dir)
                data_dir = os.path.join(project_root, 'data')
                os.makedirs(data_dir, exist_ok=True)
                self.live_log_path = os.path.join(
                    data_dir,
                    f"live_log_{self.session_timestamp}_{self.patient_info}.txt",
                )
                self._live_log_handle = open(self.live_log_path, 'a',
                                             encoding='utf-8', buffering=1)
                print(f"[SESSION] Live log: {os.path.basename(self.live_log_path)}")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            full = f"{ts} | {line}"
            self._live_log_handle.write(full + "\n")
            print(full)
        except Exception as e:
            print(f"[SESSION] WARN: live log write failed: {e}")

    def close_live_log(self):
        """Flush and close the live-log file handle, if open."""
        try:
            if getattr(self, '_live_log_handle', None) is not None:
                self._live_log_handle.flush()
                self._live_log_handle.close()
                self._live_log_handle = None
        except Exception:
            pass

    def delete_live_log(self):
        """Remove the live log file from disk."""
        self.close_live_log()
        try:
            path = getattr(self, 'live_log_path', None)
            if path and os.path.exists(path):
                os.remove(path)
                print(f"[SESSION] Deleted: {os.path.basename(path)}")
        except OSError as e:
            print(f"[SESSION] WARN: could not delete live log: {e}")

    def delete_patient_json(self):
        """Remove the patient-intake JSON the launcher wrote. We find its
        path via the PATIENT_INTAKE_JSON env var that the launcher set."""
        path = os.environ.get('PATIENT_INTAKE_JSON')
        try:
            if path and os.path.exists(path):
                os.remove(path)
                print(f"[SESSION] Deleted: {os.path.basename(path)}")
        except OSError as e:
            print(f"[SESSION] WARN: could not delete patient JSON: {e}")

    def print_session_summary(self):
        """Console summary at LIVE -> STOPPED. Compact clinical-style block."""
        states = self.stress_history.get('state') or []
        s_t = self.stress_history.get('s_t') or []
        if not states:
            print("\n[SESSION REVIEW] No live samples recorded — nothing to summarize.\n")
            return
        n = len(states)
        rate = float(Config.PIPELINE_RATE)
        live_sec = n / rate
        n_calm = sum(1 for s in states if s == 'calm')
        n_stress = sum(1 for s in states if s == 'stressed')
        n_ultra = sum(1 for s in states if s == 'ultra_stressed')

        def _pct(k):
            return 100.0 * k / n if n else 0.0

        mean_st = sum(s_t) / len(s_t) if s_t else 0.0
        max_st = max(s_t) if s_t else 0.0

        print()
        print("=" * 56)
        print("  SESSION REVIEW")
        print("=" * 56)
        print(f"  Patient        : {self.patient_info}")
        print(f"  Live duration  : {live_sec:6.1f} s  ({n} samples)")
        print(f"  Time in CALM   : {n_calm/rate:6.1f} s  ({_pct(n_calm):5.1f}%)")
        print(f"  Time in STRESS : {n_stress/rate:6.1f} s  ({_pct(n_stress):5.1f}%)")
        print(f"  Time in ULTRA  : {n_ultra/rate:6.1f} s  ({_pct(n_ultra):5.1f}%)")
        print(f"  Mean S_t       : {mean_st:+6.2f}")
        print(f"  Max  S_t       : {max_st:+6.2f}")
        print(f"  CSV            : {os.path.basename(self.output_file_path)}")
        if self.baseline_capture_path:
            print(f"  Baseline JSON  : {os.path.basename(self.baseline_capture_path)}")
        print("=" * 56)
        print()
    
    def log_sample(self, eda: float, hr: float, hrv: float,
                   delta_eda: float = 0.0, delta_hr: float = 0.0, delta_hrv: float = 0.0,
                   s_instant: float = None, s_t: float = None,
                   state: str = None, dashboard_score: float = None):
        """Log a complete sample to the output CSV file (held handle)."""
        if self._csv_handle is None:
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        p = self.patient
        self._csv_handle.write(
            f"{timestamp},{self.phase},"
            f"{p['first_name']},{p['last_name']},{p['patient_id']},"
            f"{p['gender']},{p['session_date']},{p['session_number']},"
            f"{eda:.4f},{hr:.4f},{hrv:.4f},"
            f"{delta_eda:.4f},{delta_hr:.4f},{delta_hrv:.4f},"
            f"{s_instant if s_instant is not None else 0.0:.4f},"
            f"{s_t if s_t is not None else 0.0:.4f},"
            f"{state if state else 'unknown'},"
            f"{dashboard_score if dashboard_score is not None else 0.0:.2f},"
            f"{self.artifacts_removed.get('eda', 0)},"
            f"{self.artifacts_removed.get('hr', 0)},"
            f"{self.artifacts_removed.get('hrv', 0)}\n"
        )
    
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
            print(f"\n[SESSION] BASELINE COMPLETE at {elapsed:.1f}s")
            print(f"[SESSION] Phase transition: BASELINE -> LIVE\n")
    
    def set_baseline_stats(self, personal_baselines: dict, artifacts_removed: dict):
        """Store baseline statistics after 3-sigma cleaning."""
        self.personal_baselines = personal_baselines
        self.artifacts_removed = artifacts_removed

        print(f"[SESSION] Personal Baselines:")
        print(f"  EDA: {personal_baselines['eda']:.2f} uS")
        print(f"  HR:  {personal_baselines['hr']:.2f} BPM")
        print(f"  HRV: {personal_baselines['hrv']:.2f} ms")

    def write_baseline_capture(self, sigma_baseline: float,
                                thresh_mild: float, thresh_high: float,
                                source_label: str = "",
                                hrv_summary: dict = None):
        """
        Persist the complete baseline state to a JSON file at the moment
        calibration finishes. This file is the canonical record of the
        patient's resting state for this session and can be loaded later
        for follow-up sessions or post-hoc analysis.
        """
        import json as _json
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        data_dir = os.path.join(project_root, 'data')
        os.makedirs(data_dir, exist_ok=True)

        filename = (f"baseline_{self.session_timestamp}_"
                    f"{self.patient['patient_id']}.json")
        path = os.path.join(data_dir, filename)

        record = {
            "patient": self.patient,
            "session_timestamp": self.session_timestamp,
            "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "baseline_duration_sec": Config.BASELINE_SEC,
            "sample_rate_pipeline_hz": Config.PIPELINE_RATE,
            "personal_baselines": {
                "eda_uS": float(self.personal_baselines.get('eda', 0.0)),
                "hr_bpm": float(self.personal_baselines.get('hr', 0.0)),
                "hrv_ms": float(self.personal_baselines.get('hrv', 0.0)),
            },
            "sigma_baseline": float(sigma_baseline),
            "thresholds": {
                "mild": float(thresh_mild),
                "high": float(thresh_high),
            },
            "hrv_metrics": hrv_summary or {},
            "artifacts_removed": {
                "eda": int(self.artifacts_removed.get('eda', 0)),
                "hr":  int(self.artifacts_removed.get('hr', 0)),
                "hrv": int(self.artifacts_removed.get('hrv', 0)),
            },
            "source": source_label,
            "pipeline_constants": {
                "ema_alpha_eda": Config.EMA_ALPHA_EDA,
                "ema_alpha_hr":  Config.EMA_ALPHA_HR,
                "ema_alpha_hrv": Config.EMA_ALPHA_HRV,
                "artifact_sigma_multiplier": Config.ARTIFACT_SIGMA_MULTIPLIER,
                "weight_eda": Config.WEIGHT_EDA,
                "weight_hrv": Config.WEIGHT_HRV,
                "weight_hr":  Config.WEIGHT_HR,
                "thresh_mild_k": Config.THRESH_MILD_K,
                "thresh_high_k": Config.THRESH_HIGH_K,
            },
        }
        with open(path, 'w', encoding='utf-8') as f:
            _json.dump(record, f, indent=2)
        self.baseline_capture_path = path
        print(f"[SESSION] Baseline captured: {os.path.basename(path)}")
        return path

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
            print(f"\n[SESSION] BASELINE COMPLETE at {elapsed:.1f}s")
            print(f"[SESSION] Phase transition: BASELINE -> LIVE\n")
    
    def set_baseline_stats(self, personal_baselines: dict, artifacts_removed: dict):
        """Store baseline statistics after 3-sigma cleaning."""
        self.personal_baselines = personal_baselines
        self.artifacts_removed = artifacts_removed
        
        print(f"[SESSION] Personal Baselines:")
        print(f"  -> EDA:  {personal_baselines['eda']:.2f} uS")
        print(f"  -> HR:   {personal_baselines['hr']:.2f} BPM")
        print(f"  -> HRV:  {personal_baselines['hrv']:.2f} ms")
    
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
