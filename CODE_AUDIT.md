# How the code handles things going wrong

Real recordings are messy: electrodes lose contact, Bluetooth drops, people move, the occasional sample comes through as garbage. This document lists everything that can plausibly go wrong and points at the exact place in the code that deals with it. When the question is "what happens if the signal cuts out mid-session?", this is the file to open.

Short answer to the general question: the pipeline is built to degrade gracefully. It rejects bad input rather than letting it corrupt the math, warns the operator when data quality drops, and if something truly fatal happens it still saves whatever was recorded before stopping.

## Failure-mode matrix

The "Handled" column means there is explicit code for it; the "Where" column is the file and function to look at.

| Failure mode | Handled | Where | What happens |
|---|---|---|---|
| LSL stream not present at startup | yes | `acquisition.py` `_connect_to_stream` | Raises an error; the launcher aborts with a clear message |
| Stream goes silent mid-session (unplug, Bluetooth drop) | yes | `acquisition.py` `get_synchronized_sample` | After 5 seconds of silence (`STREAM_TIMEOUT_SEC`) it raises a connection error and the session ends cleanly |
| Device streams faster than the pipeline reads | yes | `acquisition.py` `get_synchronized_sample` | Drains the inlet each tick and uses the most recent sample, so the pipeline never falls behind real time |
| Dashboard timer can't keep up with the 50 Hz stream | yes | `dashboard.py` `update_dashboard` | Drains the inlet to the latest sample and counts the chunk size for time-in-state, so the UI stays responsive and the per-state counters stay accurate |
| Recording file has a truncated final row | yes | `data_sources.py` `MockDataSource` | Loads with a tolerant parser that skips malformed lines |
| Mock file has a different ECG/EDA channel order | yes | `data_sources.py` `parse_opensignals_header` | Channel positions are read from the file header, never hardcoded |
| Live PLUX stream has a different ECG/EDA channel order | yes | `data_sources.py` `resolve_plux_channels` | Channel labels are read from the LSL stream metadata; if missing, the Config indices are the fallback |
| File / stream has a different sample rate (200 vs 1000 Hz) | yes | `data_sources.py` `parse_opensignals_header` / `RealPLUXDataSource*` | Rate is read at startup and fed into pacing and R-peak detection |
| NaN or infinite value in a sample | yes | `acquisition.py` `_is_valid_number` | Sample rejected, previous value held, logged as `NAN_REJ`, counter incremented |
| Physiologically impossible value (HR 400, EDA -5) | yes | `acquisition.py` `_within` checks | Sample rejected, previous value held, logged as `OOR_REJ`, counter incremented |
| Electrode disconnect (signal pinned flat) | yes | `acquisition.py` `_check_disconnect` | If a signal's variance stays near zero for 15 seconds, prints a one-time warning; the session keeps running so partial data is still saved |
| ADC saturation (signal stuck at full scale) | partial | covered indirectly | Usually trips the out-of-range or disconnect rule above; no dedicated rail-clip detector yet |
| Baseline never finishes (signal cut during the first 2 minutes) | yes | acquisition timeout + main loop | The connection error ends the session and flushes the CSV |
| Main loop runs slower than 50 Hz at the baseline mark | yes | `processing.py` `finalize_baseline_now` + `main.py` | Wall-clock 120 s safety net forces the baseline lock on whatever samples accumulated, so the duration counter and the lock always agree |
| Flat signal through the whole baseline (sigma comes out zero) | yes | `fusion.py` `set_thresholds` | Falls back to a safe default sigma with a loud warning, so the thresholds aren't set to zero (which would mark everything ultra-stressed) |
| The 3-sigma cleaning rejects every sample | yes | `processing.py` `_compute_personal_baselines` | Falls back to the raw buffer with a warning |
| Baseline average of zero (would divide by zero) | yes | `fusion.py` `compute_s_instant` | Guarded with a tiny floor value |
| R-peak detector finds no beats (adaptive path) | yes | `data_sources.py` `derive_hr_hrv_from_ecg` | Returns sensible default HR/HRV instead of empty or NaN |
| R-peak detector finds no beats (NeuroKit path) | yes | `data_sources.py` `derive_hr_hrv_from_ecg_nk` | Returns default series and logs the failure; downstream sees defaults instead of NaN |
| RMSSD outside physiological band (5-300 ms) on NeuroKit path | yes | `derive_hr_hrv_from_ecg_nk` / `_recompute_hr_hrv` | Treated as a detector failure; the value is not reported and previous value is held |
| Missed or doubled beats spiking HR/HRV | yes | `data_sources.py` `derive_hr_hrv_from_ecg` | RR intervals far off the running median are replaced by the median before HR and HRV are computed |
| Operator presses Ctrl+C | yes | `main.py` + `launcher.py` | Subprocesses terminated; the session CSV is already on disk. Patient JSON and diagnostic logs are not cleaned up automatically on Ctrl+C; use the dashboard's close-window prompt for the save/discard choice. |
| Operator closes the dashboard window during a session | yes | `dashboard.py` `closeEvent` + `main.py` `_handle_shutdown` | Prompts for save vs discard, sends the appropriate SHUTDOWN_* command, main deletes per the choice and exits cleanly |
| Any other unexpected error | yes | `main.py` outer exception handler | Prints the error, flushes the partial CSV, re-raises so the failure is visible |
| Dashboard can't find the output stream | yes | `dashboard.py` | Catches it and prints "ensure main.py is running" |
| Chart Y-axis collapsing on flat data | yes | `dashboard.py` `_create_signal_plot` | Y-range is locked each tick; the autorange button and right-click menu are disabled |
| UDP send fails (Windows ICMP reset, no receiver) | yes | `unity_bridge.py` `send_raw` / `send_state` | Caught and logged; the socket survives for subsequent sends |
| Network jitter / out-of-order packets | n/a | LSL itself | LSL timestamps and reorders samples; the pipeline only uses the values, so jitter is invisible |

## What the operator can see

The acquisition layer keeps three running counters and broadcasts them on the live stream, so the dashboard's data-quality strip shows them in real time and turns them red if they go above zero:

- `invalid_sample_count`: NaN or infinite samples rejected
- `out_of_range_count`: samples outside the physiological bounds rejected
- `disconnect_warnings_issued`: electrode-disconnect episodes flagged

The UDP bridge also tracks its own:

- `commands_sent`: UDP packets actually delivered (lifecycle + state)
- `commands_throttled`: state-driven packets suppressed by the 1-second throttle
- `commands_gated`: packets held back during the wait-for-calm gate

`commands_sent` is on the dashboard live panel as part of the Unity last-command tracker. On a clean session the QA counters all stay at zero. Any nonzero value points at a hardware or contact problem rather than a software one.

## Spec compliance, in brief

Every numbered step of the math-pipeline document is implemented. The detailed step-by-step is in `DATA_FLOW.md`; this is just the checklist.

| Step | What | Where |
|---|---|---|
| 0 | Acquisition, zero-order hold, HR and HRV from ECG | `acquisition.py`, `data_sources.derive_hr_hrv_from_ecg` (adaptive) and `derive_hr_hrv_from_ecg_nk` (NeuroKit chain) |
| 1 | Smoothing — REMOVED per PDF (vret_server.py: "EDA is intentionally NOT smoothed"). Raw values flow through unchanged; phasic EDA decomposition lives in `processing._update_phasic_eda`. |
| 2 | 120-second baseline buffer | `processing.py` `_buffer_sample` + `main.py` wall-clock safety net |
| 3 | 3-sigma outlier removal | `processing.py` `_compute_personal_baselines` |
| 4 | Personal baselines and frozen sigma | `processing.py` and `fusion.calculate_baseline_sigma` |
| 5 | Per-sample percentage deviation (HRV inverted) | `fusion.compute_s_instant` |
| 6 | Weighted fusion (0.5 / 0.3 / 0.2) | `fusion.compute_s_instant` |
| 7 | One-second rolling mean to get S_t | `fusion.evaluate_state` |
| 8 | Thresholds at `mean_baseline + 1.28·sigma` and `mean_baseline + 2.33·sigma` (true 90th / 99th-percentile z, centred on baseline mean per PDF Bug 6); frozen for the session | `fusion.set_thresholds` |
| 9 | State classification (calm / stressed / ultra_stressed). Balloon control was removed from Python; Unity owns altitude now. | `fusion.evaluate_state` |
| 10 | 0-100 dashboard score | `fusion.evaluate_state` |
| outputs | 24-channel LSL stream + UDP command bridge to Unity with audit log | `output.UnityBridge`, `unity_bridge.UnityUDPBridge` |
| operator control | 5-state machine driven by dashboard buttons via a separate LSL stream | `session_control.SessionState`, `Biofeedback_Control` |

## What is modular versus what needs a code change

Editable in `src/config.py` alone, no pipeline code touched: the data source (`mock` / `real_plux`), the HR/HRV/EDA backend (`neurokit` / `bsnb`), the mock file path, every math constant (3-sigma multiplier, fusion weights, threshold multipliers, sigma floors), the HR / RMSSD window sizes, the physiological sanity bounds, the disconnect-detection thresholds, the dashboard chart ranges, the baseline duration, the UDP target and throttle, the wait-for-calm warmup, and the upper bound on intake session number. Altitude logic and difficulty modes are no longer in this codebase: they moved to Unity.

These still require editing code: the LSL channel layout (changing it means updating `output.py` and `dashboard.py` in sync), the dashboard color theme (RGB values in `dashboard.py`), the Butterworth filter order in the R-peak detector, the logging directory (the string `data/` appears in a few modules), and the state-machine transitions (`_TRANSITIONS` in `session_control.py`).

## What is left before the project is fully done

The pipeline and the math are finished and verified. What remains is integration work, not pipeline work:

The real PLUX device path runs end-to-end against live hardware (verified June 2026). The Unity scene is a separate project, built against the output described in `OUTPUTS.md`. A few small polish items remain: the duplicate method definitions in `session_manager.py` are dead code that could be removed, and the launcher's Ctrl+C path could be wired to send a SHUTDOWN command so file cleanup matches the dashboard's close-window path.

## Checks that can be run right now

```
# Degenerate-baseline guard kicks in (prints a sigma warning, doesn't crash)
env\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); from fusion import FusionEngine; fe=FusionEngine(); fe.set_thresholds(0.0)"

# Header parser reads rate and channel order from any file
env\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); from data_sources import parse_opensignals_header as p; print(p('data/14_minute_test_of_myself_2026-05-26_16-47-36.txt'))"

# Full session, confirm no errors
env\Scripts\python.exe launcher.py
```
