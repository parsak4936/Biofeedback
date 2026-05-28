# How the code handles things going wrong

Real recordings are messy: electrodes lose contact, Bluetooth drops, people move, the occasional sample comes through as garbage. This document lists everything that can plausibly go wrong and points at the exact place in the code that deals with it. If you're ever asked "what happens if the signal cuts out mid-session?", this is the file to open.

The short answer to the general question is: the pipeline is built to degrade gracefully. It rejects bad input rather than letting it corrupt the math, it warns the operator when data quality drops, and if something truly fatal happens it still saves whatever was recorded before stopping.

## Failure-mode matrix

The "Handled" column means there's explicit code for it; the "Where" column is the file and function to look at.

| Failure mode | Handled | Where | What happens |
|---|---|---|---|
| LSL stream not present at startup | yes | `acquisition.py` `_connect_to_stream` | Raises an error; the launcher aborts with a clear message |
| Stream goes silent mid-session (unplug, Bluetooth drop) | yes | `acquisition.py` `get_synchronized_sample` | After 5 seconds of silence (`STREAM_TIMEOUT_SEC`) it raises a connection error and the session ends cleanly |
| Device streams faster than the pipeline reads | yes | `acquisition.py` `get_synchronized_sample` | Drains the inlet each tick and uses the most recent sample, so we never fall behind real time |
| Recording file has a truncated final row | yes | `data_sources.py` `MockDataSource` | Loads with a tolerant parser that skips malformed lines |
| File has a different ECG/EDA channel order | yes | `data_sources.py` `parse_opensignals_header` | Channel positions are read from the file header, never hardcoded |
| File has a different sample rate (200 vs 1000 Hz) | yes | `data_sources.py` `parse_opensignals_header` | Rate is read from the header and fed into pacing and R-peak detection |
| NaN or infinite value in a sample | yes | `acquisition.py` `_is_valid_number` | Sample rejected, previous value held, logged as `NAN_REJ`, counter incremented |
| Physiologically impossible value (HR 400, EDA -5) | yes | `acquisition.py` `_within` checks | Sample rejected, previous value held, logged as `OOR_REJ`, counter incremented |
| Electrode disconnect (signal pinned flat) | yes | `acquisition.py` `_check_disconnect` | If a signal's variance stays near zero for 15 seconds, prints a one-time warning; the session keeps running so partial data is still saved |
| ADC saturation (signal stuck at full scale) | partial | covered indirectly | Usually trips the out-of-range or disconnect rule above; there's no dedicated rail-clip detector yet |
| Baseline never finishes (signal cut during the first 2 minutes) | yes | acquisition timeout + main loop | The connection error ends the session and flushes the CSV |
| Flat signal through the whole baseline (sigma comes out zero) | yes | `fusion.py` `set_thresholds` | Falls back to a safe default sigma with a loud warning, so the thresholds aren't set to zero (which would mark everything ultra-stressed) |
| The 3-sigma cleaning rejects every sample | yes | `processing.py` `_compute_personal_baselines` | Falls back to the raw buffer with a warning |
| Baseline average of zero (would divide by zero) | yes | `fusion.py` `compute_s_instant` | Guarded with a tiny floor value |
| R-peak detector finds no beats | yes | `data_sources.py` `derive_hr_hrv_from_ecg` | Returns sensible default HR/HRV instead of empty or NaN |
| Missed or doubled beats spiking HR/HRV | yes | `data_sources.py` `derive_hr_hrv_from_ecg` | RR intervals far off the running median are replaced by the median before HR and HRV are computed |
| Operator presses Ctrl+C | yes | `main.py` | Stops cleanly, the session CSV is already on disk |
| Session-end cap reached | yes | `main.py` `live_tick_cap` | Prints "SESSION COMPLETE", exits, CSV flushed |
| Any other unexpected error | yes | `main.py` outer exception handler | Prints the error, flushes the partial CSV, re-raises so the failure is visible |
| Dashboard can't find the output stream | yes | `dashboard.py` | Catches it and prints "ensure main.py is running" |
| Chart Y-axis collapsing on flat data | yes | `dashboard.py` `_create_signal_plot` | Y-range is locked each tick; the autorange button and right-click menu are disabled |
| Network jitter / out-of-order packets | n/a | LSL itself | LSL timestamps and reorders samples; we only use the values, so jitter is invisible to us |

## What the operator can see

The acquisition layer keeps three running counters and broadcasts them on the live stream, so the dashboard's Session Statistics panel shows them in real time and turns them red if they go above zero:

- `invalid_sample_count` — NaN or infinite samples rejected
- `out_of_range_count` — samples outside the physiological bounds rejected
- `disconnect_warnings_issued` — electrode-disconnect episodes flagged

On a clean session these all stay at zero. Any nonzero value points at a hardware or contact problem rather than a software one.

## Spec compliance, in brief

Every numbered step of the math-pipeline document is implemented. The detailed step-by-step is in `DATA_FLOW.md`; this is just the checklist.

| Step | What | Where |
|---|---|---|
| 0 | Acquisition, zero-order hold, HR and HRV from ECG | `acquisition.py`, `data_sources.derive_hr_hrv_from_ecg` |
| 1 | EMA smoothing | `processing.py` `_apply_ema` |
| 2 | 120-second baseline buffer | `processing.py` `_buffer_sample` |
| 3 | 3-sigma outlier removal | `processing.py` `_compute_personal_baselines` |
| 4 | Personal baselines and frozen sigma | `processing.py` and `fusion.calculate_baseline_sigma` |
| 5 | Per-sample percentage deviation (HRV inverted) | `fusion.compute_s_instant` |
| 6 | Weighted fusion (0.5 / 0.3 / 0.2) | `fusion.compute_s_instant` |
| 7 | One-second rolling mean to get S_t | `fusion.evaluate_state` |
| 8 | Thresholds at 1.33 and 2.28 times sigma, frozen for the session | `fusion.set_thresholds` |
| 9 | State classification and rate-based balloon control, three modes | `fusion.evaluate_state`, `Config.MODE_RANGES` |
| 10 | 0-100 dashboard score | `fusion.evaluate_state` |
| outputs | 18-channel LSL stream | `output.UnityBridge` |

## What's modular versus what needs a code change

You can change all of this by editing `src/config.py` alone, no pipeline code touched: the data source, the mock file path, the session mode, every math constant (smoothing factors, the 3-sigma multiplier, the fusion weights, the threshold multipliers, the mode altitude ranges, the balloon rate constants), the physiological bounds, the disconnect-detection thresholds, the dashboard chart ranges, and the session timing. Adding a fourth difficulty mode is just one more entry in the mode-ranges dictionary.

These still require editing code: the LSL channel layout (changing it means updating both `output.py` and `dashboard.py`), the dashboard color theme (RGB values in `dashboard.py`), the Butterworth filter order in the R-peak detector, and the logging directory (the string "data/" appears in a few modules).

## What's left before the project is fully done

The pipeline and the math are finished and verified. What remains is integration work, not pipeline work:

The real PLUX device path is written but needs a hands-on dry run — flip `Config.DATA_SOURCE` to `'real_plux'`, start OpenSignals, run a session, and confirm the disconnect detection fires when you unplug an electrode. The Unity scene is a separate project being built against the output stream described in `OUTPUTS.md`. And there are a few small polish items: a dedicated ECG waveform plot for visually confirming electrode contact, and moving the last couple of hardcoded values into config.

## Checks you can run right now

```
# Degenerate-baseline guard kicks in
python -c "import sys; sys.path.insert(0,'src'); from fusion import FusionEngine; fe=FusionEngine(); fe.set_thresholds(0.0)"

# Header parser reads rate and channel order from any file
python -c "import sys; sys.path.insert(0,'src'); from data_sources import parse_opensignals_header as p; print(p('data/14_minute_test_of_myself_2026-05-26_16-47-36.txt'))"

# Full session, confirm no errors
python launcher.py
```
