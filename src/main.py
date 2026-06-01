# src/main.py
"""
Main 50 Hz pipeline loop, state-machine driven.

The operator drives transitions via the dashboard's Start / Stop
buttons (one pair on each panel). Buttons publish commands on the
`Biofeedback_Control` LSL stream; this loop reads them and applies a
lookup table of valid transitions, plus side effects on entry to each
state:

  IDLE           -> samples flow but nothing accumulates (waiting for click)
  BASELINE       -> baseline buffer fills (gated by accumulate_baseline)
  BASELINE_DONE  -> baseline captured, JSON written, thresholds locked,
                    waiting for Start Live (operator is putting on the VR
                    headset, getting settled, etc.)
  LIVE           -> stress fusion runs, UDP commands go to Unity, per-tick
                    CSV row written, one human-readable line per second to
                    the live transcript log
  STOPPED        -> live session ended, session summary printed,
                    ready for another Start Live or close

Two independent clocks (baseline_started_at and live_started_at) keep
the dashboard's per-panel duration counters honest: each starts at zero
on the corresponding state entry and freezes on exit. The baseline
auto-lock fires at the 6000-sample target, or at the wall-clock 120 s
mark (whichever comes first) so the lock and the displayed counter
always agree.

LSL broadcast happens on every tick regardless of state, so the
dashboard can keep showing live signal traces even when nothing else is
happening.

SHUTDOWN_* commands sent by the dashboard's close-window prompt are
handled out-of-band by `_handle_shutdown`: it deletes the files the
operator asked to discard (closing open handles first so Windows allows
it), then breaks the main loop. The launcher's poll-loop notices the
dashboard exit and terminates the remaining subprocesses.
"""

import os
import time

from acquisition import BiofeedbackAcquisition
from config import Config
from fusion import FusionEngine
from output import UnityBridge
from processing import SignalProcessor
from session_control import (Command, ControlSubscriber, SessionState,
                             apply_command, SHUTDOWN_COMMANDS)
from session_manager import SessionManager
from unity_bridge import UnityUDPBridge


# File sentinel for cross-platform shutdown on launcher Ctrl+C. Windows
# Popen.terminate() uses TerminateProcess (uncatchable), so we can't rely
# on SIGTERM. Instead the launcher writes this file on Ctrl+C and the main
# loop polls for it once per second. Treated as SHUTDOWN_DISCARD_LIVE,
# matching the dashboard's "Keep baseline only" close prompt.
_SHUTDOWN_MARKER_FILENAME = ".shutdown_marker"


def run_pipeline():
    print("=" * 50)
    print("  BIOFEEDBACK PIPELINE STARTING")
    print("=" * 50 + "\n")

    # Shutdown sentinel path (see _SHUTDOWN_MARKER_FILENAME). The launcher
    # writes here on Ctrl+C; we poll it once per second below and treat it
    # as SHUTDOWN_DISCARD_LIVE so the cleanup path matches the dashboard's
    # close-window "Keep baseline only" prompt.
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _shutdown_marker_path = os.path.join(_project_root, 'data', _SHUTDOWN_MARKER_FILENAME)
    # Clear any stale marker from a previous launch.
    try:
        if os.path.exists(_shutdown_marker_path):
            os.remove(_shutdown_marker_path)
    except OSError:
        pass

    patient_name = os.environ.get('PATIENT_NAME', 'PATIENT')
    patient_id = os.environ.get('PATIENT_ID', '000')
    session = SessionManager(patient_name=patient_name, patient_id=patient_id)

    # Startup order matters here. The dashboard subscribes to Biofeedback_State
    # (our output) and publishes Biofeedback_Control (our input). To avoid a
    # circular-dependency deadlock and to preserve mock-mode reproducibility:
    #   1. Open our Biofeedback_State outlet first so the dashboard can attach.
    #   2. Open the UDP bridge (no LSL involved).
    #   3. Wait for the dashboard's Biofeedback_Control outlet to appear.
    #   4. ONLY THEN create the OpenSignals inlet — this is the trigger for
    #      the streamer's consumer-wait handshake; samples start flowing from
    #      file index 0 the moment we're ready.
    fusion = FusionEngine()
    out = UnityBridge()
    # Audit log lives alongside the session CSV so it's trivial to
    # correlate UDP packets with the recorded stress trace.
    _audit_dir = os.path.dirname(session.output_file_path)
    _audit_name = (f"unity_udp_log_{session.session_timestamp}"
                   f"_{session.patient_info}.csv")
    unity = UnityUDPBridge(audit_log_path=os.path.join(_audit_dir, _audit_name))
    control = ControlSubscriber()  # blocks until dashboard publishes
    acq = BiofeedbackAcquisition()
    proc = SignalProcessor()

    print("\n[MAIN] All modules online. Waiting for operator commands from the dashboard.")
    print(f"[MAIN] Logging to: {os.path.basename(session.output_file_path)}\n")

    tick_duration = 1.0 / Config.PIPELINE_RATE
    state = SessionState.IDLE
    baseline_locked = False  # True once thresholds are derived (whether or not we're live)

    # Independent clocks. They start only on the corresponding state entry,
    # so all the "pipeline-uptime included" confusion goes away.
    #   baseline_started_at: when state most recently became BASELINE
    #   baseline_elapsed_frozen: time-in-BASELINE captured at the moment
    #     baseline finished (held thereafter)
    #   live_started_at / live_elapsed_frozen: same for LIVE.
    baseline_started_at = None
    baseline_elapsed_frozen = 0.0
    live_started_at = None
    live_elapsed_frozen = 0.0

    def _baseline_elapsed_now():
        if state == SessionState.BASELINE and baseline_started_at is not None:
            return min(float(Config.BASELINE_SEC),
                       time.time() - baseline_started_at)
        return baseline_elapsed_frozen

    def _live_elapsed_now():
        if state == SessionState.LIVE and live_started_at is not None:
            return time.time() - live_started_at
        return live_elapsed_frozen

    def _handle_transition(old_state, new_state):
        """Side-effects for specific state transitions."""
        nonlocal baseline_locked
        nonlocal baseline_started_at, baseline_elapsed_frozen
        nonlocal live_started_at, live_elapsed_frozen
        if old_state == new_state:
            return
        print(f"[STATE] {old_state.name} -> {new_state.name}")
        # Keep the processing audit log's phase column honest. Without this
        # the column was derived from baseline_complete (saying "BASELINE"
        # during IDLE and "LIVE" during BASELINE_DONE/STOPPED).
        proc.set_phase(new_state.name)

        # ---- IDLE on entry: clear everything if we came from data. ----
        if new_state == SessionState.IDLE and old_state in (
                SessionState.BASELINE, SessionState.BASELINE_DONE, SessionState.STOPPED):
            proc.reset()
            fusion.reset()
            unity.reset()
            baseline_locked = False
            baseline_started_at = None
            baseline_elapsed_frozen = 0.0
            live_started_at = None
            live_elapsed_frozen = 0.0

        # Stop DURING baseline (before the 120 s lock): the diagnostic
        # logs contain partial garbage. Discard them.
        if new_state == SessionState.IDLE and old_state == SessionState.BASELINE:
            proc.discard_log()
            acq.discard_log()

        # ---- BASELINE on entry: zero everything that's baseline-scoped. ----
        if new_state == SessionState.BASELINE:
            # Coming back from BASELINE_DONE or STOPPED → wipe the old baseline
            # so the new run starts fresh (otherwise proc.baseline_complete
            # stays True and the new BASELINE flips to BASELINE_DONE in one tick).
            if old_state in (SessionState.BASELINE_DONE, SessionState.STOPPED):
                proc.reset()
                fusion.reset()
                unity.reset()
                baseline_locked = False
            baseline_started_at = time.time()
            baseline_elapsed_frozen = 0.0
            live_started_at = None
            live_elapsed_frozen = 0.0
            session.phase = "BASELINE"

        # ---- BASELINE_DONE on entry: freeze the baseline clock. ----
        if new_state == SessionState.BASELINE_DONE:
            if old_state == SessionState.BASELINE and baseline_started_at is not None:
                baseline_elapsed_frozen = min(
                    float(Config.BASELINE_SEC),
                    time.time() - baseline_started_at,
                )
            # Coming back from LIVE/STOPPED via LIVE_RESTART: flush the live
            # results but keep the baseline.
            if old_state in (SessionState.LIVE, SessionState.STOPPED):
                session.flush_live_data()
                fusion.reset()
                # Re-lock thresholds against the still-valid baseline so a
                # subsequent LIVE_START reuses them.
                if proc.cleaned_baseline_buffers and proc.personal_averages:
                    mean_b, sigma_b = fusion.calculate_baseline_sigma(
                        proc.cleaned_baseline_buffers,
                        proc.personal_averages,
                        phasic_buffer=proc.cleaned_phasic_buffer,
                    )
                    fusion.set_thresholds(mean_baseline=mean_b, sigma_baseline=sigma_b)
                unity.reset()
                if old_state == SessionState.LIVE:
                    unity.send_raw("stop")  # tell Unity the previous live ended
            live_started_at = None
            live_elapsed_frozen = 0.0
            session.phase = "BASELINE_DONE"

        # ---- LIVE on entry ----
        if new_state == SessionState.LIVE:
            # If the operator is starting ANOTHER live session against the
            # same baseline (STOPPED -> LIVE), rotate the CSV to a fresh
            # file, reset the fusion buffer + UDP gate so the new run
            # starts statistically clean, and re-arm Unity with a fresh
            # "start". The baseline / thresholds are preserved.
            if old_state == SessionState.STOPPED:
                session.rotate_session_csv()
                fusion.reset()
                # Re-lock thresholds against the still-valid baseline so
                # the new live session uses the same calibration.
                if proc.cleaned_baseline_buffers and proc.personal_averages:
                    mean_b, sigma_b = fusion.calculate_baseline_sigma(
                        proc.cleaned_baseline_buffers,
                        proc.personal_averages,
                        phasic_buffer=proc.cleaned_phasic_buffer,
                    )
                    fusion.set_thresholds(mean_baseline=mean_b, sigma_baseline=sigma_b)
                unity.reset()

            live_started_at = time.time()
            live_elapsed_frozen = 0.0
            session.phase = "LIVE"
            # Tell Unity we're live regardless of which prior state we came
            # from (BASELINE_DONE on first run, STOPPED on subsequent ones).
            if old_state in (SessionState.BASELINE_DONE, SessionState.STOPPED):
                unity.send_raw("start")

        # ---- STOPPED on entry: freeze the live clock, save summary. ----
        if new_state == SessionState.STOPPED and old_state == SessionState.LIVE:
            if live_started_at is not None:
                live_elapsed_frozen = time.time() - live_started_at
            unity.send_raw("stop")
            session.phase = "STOPPED"
            session.print_session_summary()

    def _handle_shutdown(cmd):
        """Process an out-of-band SHUTDOWN_* command. Cleans up files
        according to the operator's choice from the dashboard close prompt,
        then returns True so the main loop can exit."""
        print(f"[STATE] shutdown requested: {cmd.name}")
        # Always tell Unity we're done if a live session was active.
        try:
            unity.send_raw("stop")
        except Exception:
            pass
        # Close diagnostic-log file handles before deletion (Windows lock).
        try:
            if proc.log_file is not None:
                proc.log_file.flush(); proc.log_file.close()
        except Exception:
            pass
        try:
            if acq.log_file is not None:
                acq.log_file.flush(); acq.log_file.close()
        except Exception:
            pass

        if cmd == Command.SHUTDOWN_DISCARD_BOTH:
            session.delete_baseline_json()
            session.delete_session_csv()
            session.delete_patient_json()
            session.delete_live_log()
            # final=True: don't reopen the log files we just deleted.
            proc.discard_log(final=True)
            acq.discard_log(final=True)
        elif cmd == Command.SHUTDOWN_DISCARD_LIVE:
            # Keep baseline JSON, patient JSON, and diagnostic logs from
            # the captured baseline. Only the per-tick session CSV and
            # the live transcript (mostly live-phase) get removed.
            session.delete_session_csv()
            session.delete_live_log()
        elif cmd == Command.SHUTDOWN_SAVE_BOTH:
            # Just close the live log handle so the file is flushed; no
            # delete. The session CSV, baseline JSON, etc. stay.
            session.close_live_log()
        return True

    try:
        while True:
            start_time = time.time()

            # ---- Phase A: read operator commands and transition ----
            shutdown_requested = False
            # File-sentinel poll for launcher Ctrl+C (cross-platform; Windows
            # Popen.terminate is uncatchable so this is the cheapest reliable
            # mechanism). Polled once per second to keep the stat() cost
            # negligible. Treated as SHUTDOWN_DISCARD_LIVE.
            if (acq.tick_counter % int(Config.PIPELINE_RATE) == 0
                    and os.path.exists(_shutdown_marker_path)):
                print("[MAIN] Shutdown marker detected; treating as SHUTDOWN_DISCARD_LIVE.")
                _handle_shutdown(Command.SHUTDOWN_DISCARD_LIVE)
                try:
                    os.remove(_shutdown_marker_path)
                except OSError:
                    pass
                break
            for cmd in control.poll():
                if cmd in SHUTDOWN_COMMANDS:
                    _handle_shutdown(cmd)
                    shutdown_requested = True
                    break
                new_state = apply_command(state, cmd)
                _handle_transition(state, new_state)
                state = new_state
            if shutdown_requested:
                print("[MAIN] Shutdown command processed. Exiting main loop.")
                break

            # ---- Phase B: pull the latest sample (always, regardless of state) ----
            raw_vector = acq.get_synchronized_sample()
            session.record_raw_sample(raw_vector[0], raw_vector[1], raw_vector[2])

            # ---- Phase C: smooth (EMA needs continuity) ----
            # We always smooth so the smoothed traces keep flowing on the
            # dashboard. The baseline buffer is gated explicitly by state:
            # without this gate, _buffer_sample would fire every tick
            # baseline_complete was False — including during IDLE — and the
            # 120 s lock would fire before the operator clicked Start.
            proc.accumulate_baseline = (state == SessionState.BASELINE)
            smoothed_vector, ready_now = proc.process_sample(raw_vector)

            # ---- Phase D: state-specific work ----
            deltas = {'eda': 0.0, 'hr': 0.0, 'hrv': 0.0}
            s_inst, s_t, state_label, dashboard = 0.0, 0.0, "calm", 0.0

            if state == SessionState.BASELINE:
                # Two independent paths to "baseline done":
                #   (a) buffer reached 6000 samples (50 Hz nominal),
                #       handled inside process_sample → ready_now=True.
                #   (b) wall-clock 120 s has elapsed (the path the user
                #       actually sees on the duration label). On Windows
                #       the per-tick loop often runs at 40-48 Hz, so the
                #       buffer can be 200-1200 samples short of 6000 at
                #       the 2-minute mark — without this safety net the
                #       auto-lock never fires and the operator is stuck.
                if (not ready_now
                        and not baseline_locked
                        and _baseline_elapsed_now() >= float(Config.BASELINE_SEC)):
                    if proc.finalize_baseline_now():
                        ready_now = True

                # Auto-finish: when ready (by either path above), compute
                # thresholds, write the baseline JSON, transition.
                if ready_now and not baseline_locked:
                    # Detector-failure sentinel check: when the data source
                    # cannot find R-peaks (loose electrode, fully muted ECG)
                    # it returns the MOCK_HR_BASE / MOCK_HRV_BASE seed
                    # values for every sample. A baseline locked on those
                    # is unusable — print a loud warning so the operator
                    # restores contact and re-baselines before going live.
                    import numpy as _np
                    _hr_buf = proc.cleaned_baseline_buffers.get('hr')
                    _hrv_buf = proc.cleaned_baseline_buffers.get('hrv')
                    if _hr_buf is not None and len(_hr_buf) > 0:
                        _hr_var = float(_np.std(_hr_buf))
                        _hrv_var = float(_np.std(_hrv_buf)) if _hrv_buf is not None else 0.0
                        _hr_mean = float(_np.mean(_hr_buf))
                        _hrv_mean = float(_np.mean(_hrv_buf)) if _hrv_buf is not None else 0.0
                        # Sentinel pattern: variance ~0 AND value at the seed default.
                        if (_hr_var < 1e-3 and abs(_hr_mean - Config.MOCK_HR_BASE) < 0.01
                                and _hrv_var < 1e-3 and abs(_hrv_mean - Config.MOCK_HRV_BASE) < 0.01):
                            print("\n" + "!" * 60)
                            print("!! [MAIN] WARN: baseline locked on detector-failure")
                            print(f"!! defaults (HR={_hr_mean:.1f} BPM, HRV={_hrv_mean:.1f} ms,")
                            print("!! both pinned). The R-peak detector found no usable")
                            print("!! beats during baseline — likely loose ECG electrode")
                            print("!! contact. Restore contact and click Start Baseline")
                            print("!! again before going live, or the live phase will be")
                            print("!! measuring against meaningless reference values.")
                            print("!" * 60 + "\n")

                    mean_b, sigma_b = fusion.calculate_baseline_sigma(
                        proc.cleaned_baseline_buffers,
                        proc.personal_averages,
                        phasic_buffer=proc.cleaned_phasic_buffer,
                    )
                    fusion.set_thresholds(mean_baseline=mean_b, sigma_baseline=sigma_b)
                    baseline_locked = True

                    session.set_baseline_stats(
                        proc.personal_averages,
                        proc.artifacts_removed if hasattr(proc, 'artifacts_removed')
                        else {'eda': 0, 'hr': 0, 'hrv': 0},
                    )
                    session.set_thresholds(fusion.thresh_mild, fusion.thresh_high)

                    is_mock = Config.DATA_SOURCE in ('mock', 'mock2')
                    source_label = (
                        f"{Config.DATA_SOURCE}:{Config.MOCK_DATA_FILE}"
                        if is_mock
                        else f"{Config.DATA_SOURCE}@{Config.STREAM_NAME}"
                    )
                    session.write_baseline_capture(
                        sigma_baseline=sigma_b,
                        thresh_mild=fusion.thresh_mild,
                        thresh_high=fusion.thresh_high,
                        source_label=source_label,
                    )

                    state = SessionState.BASELINE_DONE
                    print("[STATE] BASELINE -> BASELINE_DONE (operator: click Start on the Live panel)")

            elif state == SessionState.LIVE:
                # Full pipeline only when LIVE. Phasic EDA is sourced from
                # the processor's rolling decomposition (PDF §7 Cause 1
                # fix); it replaces raw EDA in the fusion math.
                phasic_eda = proc.current_phasic_eda
                deltas = fusion.compute_deltas(
                    smoothed_vector, phasic_eda, proc.personal_averages)
                s_inst = fusion.compute_s_instant(
                    smoothed_vector, phasic_eda, proc.personal_averages)
                s_t, state_label, dashboard = fusion.evaluate_state(s_inst)
                session.record_stress_metric(s_inst, s_t, state_label, dashboard)
                # Pass s_t through so the audit log can show why each
                # increase/decrease was emitted.
                unity.send_state(state_label, s_t=s_t)

                # Human-readable per-second LIVE line. Format mirrors the
                # vret_server_v2 reference output so research / clinical
                # review can compare runs across both pipelines easily.
                # Tick counter on the acquisition module is at PIPELINE_RATE
                # so modulo gives roughly 1 line per second.
                if int(acq.tick_counter) % int(Config.PIPELINE_RATE) == 0:
                    session.log_live_line(
                        f"[LIVE] "
                        f"EDA={smoothed_vector[0]:.3f}uS  "
                        f"HR={smoothed_vector[1]:.1f}BPM  "
                        f"RMSSD={smoothed_vector[2]:.1f}ms  "
                        f"phasicEDA={deltas['eda']:+.3f}uS  "
                        f"dHR={deltas['hr']:+.1f}%  "
                        f"dHRV={deltas['hrv']:+.1f}%  "
                        f"S_t={s_t:+.2f}  "
                        f"state={state_label}"
                    )

            # IDLE / BASELINE_DONE / STOPPED: no extra work, defaults emitted.

            # ---- Phase E: LSL broadcast (always) ----
            avg_eda = proc.personal_averages.get('eda', 0.0) if proc.personal_averages else 0.0
            avg_hr = proc.personal_averages.get('hr', 0.0) if proc.personal_averages else 0.0
            avg_hrv = proc.personal_averages.get('hrv', 0.0) if proc.personal_averages else 0.0
            out.broadcast_state(
                s_t, state_label, dashboard,
                smoothed_vector[0], smoothed_vector[1], smoothed_vector[2],
                deltas['eda'], deltas['hr'], deltas['hrv'],
                avg_eda, avg_hr, avg_hrv,
                fusion.thresh_mild, fusion.thresh_high,
                baseline_locked=baseline_locked,
                elapsed_baseline_sec=_baseline_elapsed_now(),
                qa_invalid=acq.invalid_sample_count,
                qa_out_of_range=acq.out_of_range_count,
                qa_disconnects=acq.disconnect_warnings_issued,
                udp_gate_open=unity.gate_open,
                session_state=int(state),
                elapsed_live_sec=_live_elapsed_now(),
                unity_last_command_code=unity.last_command_code(),
                unity_commands_sent=unity.commands_sent,
            )

            # ---- Phase F: CSV row ----
            if state == SessionState.LIVE:
                session.log_sample(smoothed_vector[0], smoothed_vector[1], smoothed_vector[2],
                                   deltas['eda'], deltas['hr'], deltas['hrv'],
                                   s_inst, s_t, state_label, dashboard)
            else:
                session.log_sample(smoothed_vector[0], smoothed_vector[1], smoothed_vector[2])

            # ---- Phase G: pace ----
            tick_elapsed = time.time() - start_time
            sleep_time = tick_duration - tick_elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n" + "=" * 50)
        print("  PIPELINE STOPPED BY OPERATOR (Ctrl+C)")
        print("=" * 50)
        print(f"[SESSION] Output saved to: {os.path.basename(session.output_file_path)}")
    except ConnectionError as e:
        print("\n" + "=" * 50)
        print("  SIGNAL ACQUISITION LOST")
        print("=" * 50)
        print(f"\n{str(e)}")
        print(f"[SESSION] Output saved to: {os.path.basename(session.output_file_path)}")
    except Exception as e:
        print("\n" + "=" * 50)
        print(f"  UNEXPECTED ERROR: {type(e).__name__}")
        print("=" * 50)
        print(f"  {e}")
        print(f"[SESSION] Partial output saved to: {os.path.basename(session.output_file_path)}")
        raise
    finally:
        try:
            unity.send_raw("stop")
        except Exception:
            pass
        unity.close()


if __name__ == "__main__":
    run_pipeline()
