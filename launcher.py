# launcher.py
"""
Entry point. This is the only file a normal user runs.

What it does, in order:
  1. Shows the patient intake dialog (a PyQt5 form from src/patient_intake.py).
     Validates name, last name, ID, gender, date, and session number.
     Cancel here exits cleanly without starting anything else.

  2. Writes the validated patient record to data/patient_<id>_<date>_s<n>.json
     and passes the path to the subprocesses via the PATIENT_INTAKE_JSON
     environment variable. (The legacy PATIENT_NAME / PATIENT_ID env vars
     are also set for backward compatibility.)

  3. Spawns three subprocesses in order, with delays between them so the
     LSL streams have time to come up before the next process attaches:
       (a) src/streamer.py — publishes the OpenSignals raw signal stream
       (b) src/main.py    — runs the 50 Hz state-machine pipeline
       (c) src/dashboard.py — operator UI with Start/Stop/Reset buttons

  4. Stays alive until Ctrl+C, then terminates the three subprocesses
     in reverse order.

Mock vs real-device selection is in src/config.py (Config.DATA_SOURCE).
Default is 'mock' for safe testing on the recorded files in data/.
Change to 'real_plux' when running with the PLUX hardware via OpenSignals.

Startup Sequence:
  1. streamer.py - Data acquisition (mock or real hardware)
  2. main.py - Signal processing & baseline calibration
  3. dashboard.py - Clinical visualization

All systems shut down automatically when you close this launcher.
"""

import json
import subprocess
import time
import sys
import os
from pathlib import Path


def preflight_checks(src_dir):
    """Verify a signal source is available BEFORE anything is created.

    Runs ahead of the intake dialogue on purpose. Collecting participant
    details and creating a session directory only to discover there is no
    signal source wastes the operator's time and leaves an empty folder
    behind that someone later has to work out the meaning of.

    Returns 0 to proceed, or a non-zero exit code.
    """
    # ============================================
    # PRE-FLIGHT CHECKS
    # ============================================
    # Fail loudly here, BEFORE spawning subprocesses, so a misconfiguration
    # surfaces in the operator's terminal instead of crashing silently in
    # the streamer subprocess (its error output gets drowned out by LSL
    # initialisation chatter, and main.py then hangs forever in
    # `resolve_stream("Biofeedback_Raw")` waiting for an outlet that will
    # never appear).
    sys.path.insert(0, src_dir)  # also lets us import config below
    from config import Config

    project_root = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(project_root, 'data')

    if Config.DATA_SOURCE in ('mock', 'mock2'):
        mock_path = os.path.join(project_root, Config.MOCK_DATA_FILE)
        if not os.path.isfile(mock_path):
            print("=" * 70)
            print("[LAUNCHER] CONFIGURATION ERROR - cannot start.")
            print("=" * 70)
            print(f"  Config.MOCK_DATA_FILE = {Config.MOCK_DATA_FILE!r}")
            print(f"  Resolves to:           {mock_path}")
            print(f"  This file does not exist.")
            print()
            print(f"  Available files in data/:")
            data_files = []
            for root, _dirs, files in os.walk(data_dir):
                for fn in files:
                    if fn.endswith('.txt'):
                        rel = os.path.relpath(os.path.join(root, fn),
                                              project_root)
                        data_files.append(rel.replace(os.sep, '/'))
            if data_files:
                for fn in sorted(data_files):
                    print(f"    {fn}")
            else:
                print(f"    (no .txt recordings found under data/)")
            print()
            print(f"  Fix: edit src/config.py and set MOCK_DATA_FILE to one of")
            print(f"  the paths above (with a leading 'data/' and the .txt extension).")
            print("=" * 70)
            return 1

    if Config.DATA_SOURCE in ('real_plux', 'real_plux2'):
        # The data source resolves the upstream OpenSignals stream with a
        # blocking call that has no timeout. If OpenSignals is not
        # streaming, the subprocess waits forever and the operator sees a
        # window that simply never does anything. Check here with a short
        # timeout so the reason is stated instead of guessed at.
        print(f"[LAUNCHER] Looking for the OpenSignals LSL stream "
              f"('{Config.PLUX_LSL_NAME}')...")
        try:
            from pylsl import resolve_byprop
            found = resolve_byprop('name', Config.PLUX_LSL_NAME, timeout=5.0)
        except Exception as e:
            print(f"[LAUNCHER] WARN: could not probe for LSL streams: {e}")
            found = None

        if not found:
            print("=" * 70)
            print("[LAUNCHER] NO SIGNAL SOURCE - cannot start.")
            print("=" * 70)
            print(f"  DATA_SOURCE is '{Config.DATA_SOURCE}', which needs a live")
            print(f"  PLUX device streaming through OpenSignals. No stream named")
            print(f"  '{Config.PLUX_LSL_NAME}' was found after 5 seconds.")
            print()
            print("  For a real session, check in order:")
            print("    1. The PLUX hub is powered on and paired over Bluetooth.")
            print("    2. OpenSignals is open and actively acquiring (not idle).")
            print("    3. Lab Streaming Layer is ENABLED in OpenSignals:")
            print("       Settings -> Integrations -> Lab Streaming Layer.")
            print()
            print("  To test WITHOUT the device, replay a recording instead:")
            print("       run_mock.bat")
            print("  which runs the same pipeline against a saved capture.")
            print("  It sets an environment variable and does not modify")
            print("  src/config.py, so real-device mode stays untouched.")
            print("=" * 70)
            return 1
        print(f"[LAUNCHER] Found it. Proceeding.")
    return 0


def launch_system():
    """Launch the complete biofeedback system."""

    print("\n" + "=" * 60)
    print("  BIOFEEDBACK VIRTUAL CLINIC - LAUNCHING SYSTEM           ")
    print("=" * 60 + "\n")

    # ============================================
    # PATIENT INTAKE
    # ============================================
    # Show a PyQt5 form to collect and validate patient info before anything
    # else starts. The dialog handles all the field-level error checking; we
    # only get here with a complete, valid dict.
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')
    sys.path.insert(0, src_dir)

    # Verify a signal source exists before asking for anything. Failing
    # after intake would leave an empty session directory behind and waste
    # the operator's time with a participant already seated.
    rc = preflight_checks(src_dir)
    if rc != 0:
        return rc

    from patient_intake import collect_patient_info

    patient = collect_patient_info()
    if patient is None:
        print("[LAUNCHER] Intake cancelled by operator. Nothing to do.")
        return 0

    # Per-session folder. All outputs for this launch go inside it so the
    # data/ directory stays organised (one folder per session instead of 7
    # flat files). Folder name is patient-first so sessions group naturally
    # in a file browser sorted by name:
    #     <first>_<last>_Session<n>_<YYYY-MM-DD>_<gender>
    # e.g.  Alice_Rossi_Session1_2026-06-02_F
    # If a folder with that exact name already exists (two runs of the same
    # session number on the same day for the same patient — rare), we
    # append the wall-clock time as a tiebreaker so nothing gets clobbered.
    project_root = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(project_root, 'data')
    os.makedirs(data_dir, exist_ok=True)

    from datetime import datetime as _dt
    _session_ts = _dt.now().strftime("%Y%m%d_%H%M%S")

    def _safe(s: str) -> str:
        """Filesystem-safe version of `s`: keep alphanumerics + hyphen,
        replace anything else with '_', collapse repeats, trim edges."""
        out = ''.join(c if (c.isalnum() or c == '-') else '_' for c in str(s))
        while '__' in out:
            out = out.replace('__', '_')
        return out.strip('_')

    # Folder name is built from the participant ID only. Names were used
    # here previously; they were removed so a directory listing cannot
    # identify anyone.
    _pid = _safe(patient.get('patient_id', 'X')) or 'X'
    _sn = patient.get('session_number', 1)
    _date = _safe(patient.get('session_date', _dt.now().strftime('%Y-%m-%d')))
    _gd = _safe(patient.get('gender', '')) or 'X'

    _parts = [_pid, f"Session{_sn}", _date, _gd]
    session_folder_name = "_".join(_parts)

    # Collision safety: same patient + same session number + same day = same
    # folder name. Append HHMMSS only if needed.
    _candidate = os.path.join(data_dir, session_folder_name)
    if os.path.exists(_candidate):
        session_folder_name = f"{session_folder_name}_{_session_ts.split('_')[1]}"
    session_folder = os.path.join(data_dir, session_folder_name)
    os.makedirs(session_folder, exist_ok=True)

    # metadata.json combines what used to be patient_*.json + baseline_*.json
    # in the old flat layout. We write the intake portion now; session_manager
    # appends the frozen-baseline portion at the 120 s lock.
    metadata_path = os.path.join(session_folder, 'metadata.json')
    metadata = {
        'patient': patient,
        'session_folder': session_folder_name,
        'session_timestamp': _session_ts,
        'baseline': None,  # filled in by session_manager.write_baseline_capture()
    }
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2)
    # Keep this name for backwards compat with anything reading the env var;
    # it now points at the in-folder metadata.json instead of a flat file.
    patient_json_path = metadata_path

    patient_id = patient['patient_id']
    print(f"[LAUNCHER] Participant: {patient_id}")
    print(f"[LAUNCHER] Session {patient['session_number']} on {patient['session_date']}")
    print(f"[LAUNCHER] Intake record: {os.path.basename(patient_json_path)}\n")


    # src_dir is already on sys.path (set above for the intake import).
    config_file = os.path.join(src_dir, 'config.py')
    
    # Read current config to show user. Match the assignment line so a
    # commented-out alternative doesn't fool the detection.
    _LABELS = {
        'mock':       "MOCK (recorded file, in-house adaptive detector)",
        'mock2':      "MOCK 2 (recorded file, colleague's NeuroKit2 chain)",
        'real_plux':  "REAL PLUX (live, in-house adaptive detector)",
        'real_plux2': "REAL PLUX 2 (live, colleague's NeuroKit2 chain)",
    }
    current_source = "UNKNOWN"
    try:
        # Ask the loaded Config rather than re-parsing config.py: the value
        # can come from an environment override, in which case the source
        # text no longer carries a literal to match.
        from config import Config as _Cfg
        m = type('M', (), {'group': staticmethod(lambda _i: _Cfg.DATA_SOURCE)})
        if m:
            current_source = _LABELS.get(m.group(1), f"UNKNOWN ({m.group(1)})")
    except Exception:
        pass
    
    print(f"[CONFIG] Active Data Source: {current_source}")
    print(f"[CONFIG] To change: Edit src/config.py and set DATA_SOURCE\n")
    
    # Startup sequence
    processes = []
    
    try:
        # 1. Data Acquisition (Mock or Real Hardware)
        print("[LAUNCHER] (1) Starting Data Acquisition Layer...")
        print("[LAUNCHER]    (Initializing data source...)\n")
        p_streamer = subprocess.Popen(
            [sys.executable, "streamer.py"], 
            cwd=src_dir,
            universal_newlines=True
        )
        processes.append(p_streamer)
        time.sleep(2)  # Give LSL network time to establish
        
        # 2. Processing Pipeline
        print("[LAUNCHER] (2) Starting signal processing pipeline...")
        print(f"[LAUNCHER]     Participant: {patient_id}")
        print("[LAUNCHER]     The pipeline will wait for you to click")
        print("[LAUNCHER]     'Start Baseline' on the dashboard.\n")
        
        # Pass patient info to subprocesses. SESSION_FOLDER is the canonical
        # path every subprocess writes inside. The PATIENT_INTAKE_JSON path
        # is kept for backwards compat (now points at metadata.json inside
        # the session folder); PATIENT_ID is kept. PATIENT_NAME was removed
        # along with name collection.
        env = os.environ.copy()
        env['SESSION_FOLDER'] = session_folder
        env['PATIENT_INTAKE_JSON'] = patient_json_path
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
        print("[LAUNCHER] (3) Starting clinical dashboard...")
        print("[LAUNCHER]     The dashboard hosts the Start / Stop / Reset")
        print("[LAUNCHER]     buttons that drive the rest of the pipeline.\n")
        p_dash = subprocess.Popen(
            [sys.executable, "dashboard.py"],
            cwd=src_dir,
            env=env
        )
        processes.append(p_dash)

        print("=" * 60)
        print(f"  SYSTEM ONLINE - Participant: {patient_id}")
        print("=" * 60)
        print("\nDashboard is open. Click 'Start Baseline' to begin the 120-second")
        print("calibration; once baseline finishes, click 'Start Live Session'.")
        print("Session data will be saved to: data/session_*.csv")
        print("\nTo shutdown: Close the Dashboard window or press Ctrl+C below.\n")

        # Block until either (a) the operator closes the dashboard window
        # (p_dash exits) or (b) Ctrl+C is pressed in this terminal. Both
        # paths fall through to the shared cleanup below — closing the
        # dashboard now actually kills the whole session, no orphan
        # streamer/main processes left running in the background.
        shutdown_reason = "Ctrl+C"
        try:
            while True:
                if p_dash.poll() is not None:
                    shutdown_reason = f"dashboard closed (exit {p_dash.returncode})"
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        print(f"\n\n[LAUNCHER] {shutdown_reason}. Shutting down all subsystems...")

        # On Ctrl+C: drop a shutdown sentinel file that main.py polls and
        # treats as SHUTDOWN_DISCARD_LIVE (same cleanup as the dashboard's
        # "Keep baseline only" close prompt). This is the cross-platform
        # alternative to SIGTERM, since Windows Popen.terminate is
        # uncatchable. We give main up to 4 s to detect the marker and
        # exit cleanly; the terminate() loop below is the fallback.
        if shutdown_reason == "Ctrl+C":
            try:
                marker_path = os.path.join(project_root, 'data', '.shutdown_marker')
                with open(marker_path, 'w') as _f:
                    _f.write("shutdown")
                if len(processes) >= 2 and processes[1].poll() is None:
                    try:
                        processes[1].wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        pass
            except OSError as e:
                print(f"[LAUNCHER] WARN: could not write shutdown marker: {e}")

        # Terminate processes in reverse order. Skip any that are already
        # gone (typically the dashboard, on the window-close path; or main,
        # on the Ctrl+C cleanup path above).
        for p in reversed(processes):
            try:
                if p.poll() is None:
                    p.terminate()
                    p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

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

    # Hold the window open on failure. Without this the console closes the
    # instant the process ends and the operator never reads the reason,
    # which is exactly what happened when the launcher started exiting
    # cleanly on a missing signal source instead of hanging. Relying on the
    # .bat wrapper to pause is not enough: it only pauses on a non-zero
    # exit code, and the exit code was never propagated (see below).
    if exit_code != 0:
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass

    # Propagate the code. This was previously assigned and discarded, so
    # every run reported success and the wrapper's error branch was dead.
    sys.exit(exit_code)
