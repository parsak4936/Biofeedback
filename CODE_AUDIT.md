# Code-Level Audit — Error Handling & Spec Compliance

**Date:** 2026-05-25
**Scope:** every module in `src/`, every documented failure mode.
**Companion docs:** [AUDIT.md](AUDIT.md) (spec/feature audit), [DATA_FLOW.md](DATA_FLOW.md) (per-layer walkthrough).

This document is a **code audit**, not an output-file audit. It maps every plausible thing that can go wrong in production (network, hardware, math, user error) to the line of code that catches it. If a row says ❌, that's a known untreated failure mode; otherwise the link tells you where defense lives.

---

## Failure-mode matrix

| # | Failure mode | Caught? | Where | Behavior |
|---|---|---|---|---|
| 1 | LSL stream not present at startup | ✅ | [acquisition.py:_connect_to_stream](src/acquisition.py) | `RuntimeError` → launcher aborts with message. |
| 2 | LSL stream goes silent mid-session (device unplug, BT drop) | ✅ | [acquisition.py:get_synchronized_sample](src/acquisition.py) deadman switch | `ConnectionError` after `STREAM_TIMEOUT_SEC` (default 5 s). |
| 3 | LSL inlet pile-up (hardware rate > pipeline rate) | ✅ | [acquisition.py:get_synchronized_sample](src/acquisition.py) `pull_chunk()` | Drains inlet, uses latest sample. Matches walkthrough Step 0. |
| 4 | Mock file truncated / partial trailing row | ✅ | [data_sources.py:MockDataSource.__init__](src/data_sources.py) | `genfromtxt(invalid_raise=False)` + finite-mask filter. |
| 5 | Mock file header rewrites channel order (ECG/EDA swap) | ✅ | [data_sources.py:parse_opensignals_header](src/data_sources.py) | Channel index derived from `sensor` array, not hardcoded. |
| 6 | Mock file sample-rate change (200 Hz vs 1000 Hz) | ✅ | [data_sources.py:parse_opensignals_header](src/data_sources.py) | Rate read from JSON header, fed into both streamer pacing and R-peak detection. |
| 7 | **NaN or Inf in a sample** | ✅ | [acquisition.py:get_synchronized_sample](src/acquisition.py) `_is_valid_number` | Sample rejected, last good value held, `NAN_REJ` logged. `invalid_sample_count` incremented. |
| 8 | **Physiologically impossible value** (HR=400, EDA=−5) | ✅ | [acquisition.py:get_synchronized_sample](src/acquisition.py) `_within` checks | Sample rejected, last good held, `OOR_REJ` logged. `out_of_range_count` incremented. |
| 9 | **Electrode disconnect** (signal pinned to a rail / noise floor) | ✅ | [acquisition.py:_check_disconnect](src/acquisition.py) | Rolling-window variance < threshold → one-shot `[WARN] Possible electrode disconnect…` printed. Pipeline keeps running so partial data is still recorded. |
| 10 | ADC saturation (signal hits full-scale repeatedly) | 🟡 | Covered indirectly | Saturated values usually trip rule #8 or rule #9. A dedicated rail-clip detector would be nicer; currently not separated out. |
| 11 | Baseline buffer never fills (signal cut during baseline) | ✅ | acquisition deadman + general catch | `ConnectionError` triggers session-end + CSV flush via outer `except`. |
| 12 | **σ_baseline = 0** (signal perfectly flat through baseline) | ✅ | [fusion.py:set_thresholds](src/fusion.py) | Falls back to `SIGMA_FALLBACK=1.5` with loud WARN. Thresholds set to non-degenerate values; system stays usable. |
| 13 | **3σ filter rejects every sample** (numerical edge case) | ✅ | [processing.py:_compute_personal_baselines](src/processing.py) | Falls back to raw buffer with WARN. |
| 14 | Baseline average = 0 (divide-by-zero risk in fusion %) | ✅ | [fusion.py:compute_s_instant](src/fusion.py) | `base_xxx or 1e-6` guard. |
| 15 | R-peak detector finds 0 or 1 peaks (mock with no ECG) | ✅ | [data_sources.py:derive_hr_hrv_from_ecg](src/data_sources.py) | Returns default arrays (`MOCK_HR_BASE` / `MOCK_HRV_BASE`) instead of empty/NaN. |
| 16 | Missed beats producing RR-outlier spikes (artificial 1000 BPM) | ✅ | [data_sources.py:derive_hr_hrv_from_ecg](src/data_sources.py) | RR intervals >50 % off median are substituted with the median before HR/RMSSD calculation. |
| 17 | KeyboardInterrupt (operator Ctrl+C) | ✅ | [main.py](src/main.py) | Graceful: prints "TERMINATED BY OPERATOR", session CSV is already on disk. |
| 18 | Session-end cap reached | ✅ | [main.py](src/main.py) `live_tick_cap` | Prints "SESSION COMPLETE", exits loop, CSV flushed. |
| 19 | Any other unhandled exception | ✅ | [main.py](src/main.py) outer `except Exception` | Prints `[UNEXPECTED ERROR]`, flushes partial CSV, re-raises so launcher sees the failure. |
| 20 | Dashboard cannot reach `Biofeedback_State` stream | ✅ | [dashboard.py](src/dashboard.py) | Catches `Exception`, prints actionable message ("Ensure main.py is running"). |
| 21 | Pyqtgraph Y-range collapse on stable signal | ✅ | [dashboard.py:_create_signal_plot + update](src/dashboard.py) | Y-range locked every tick; autorange button hidden; menu disabled. |
| 22 | Dashboard click/zoom that breaks the layout | ✅ | [dashboard.py](src/dashboard.py) | Y mouse disabled per chart; menus disabled; auto-scale button hidden. |
| 23 | Network jitter / out-of-order packets | n/a | LSL middleware | LSL inherently provides timestamped samples and reordering; our consumer only uses values, not order, so jitter is invisible to us. |

> **Legend:** ✅ caught with defense in code · 🟡 caught indirectly via another rule · ❌ untreated (none right now).

---

## Diagnostics surfaced to the operator

`BiofeedbackAcquisition` now exposes three live counters:

| Counter | What it counts |
|---|---|
| `invalid_sample_count` | NaN / Inf samples rejected |
| `out_of_range_count` | Samples outside physiological bounds rejected |
| `disconnect_warnings_issued` | Electrode-disconnect events flagged |

These are not yet broadcast to the dashboard — easiest follow-up is one extra LSL channel each, or a sidecar JSON read by the dashboard once per second.

---

## Spec compliance recap (math pipeline, Steps 0–10)

This stays unchanged from [AUDIT.md](AUDIT.md), but here's the short version for completeness:

| Step | Description | Implementation | Status |
|------|---|---|---|
| 0 | LSL acquisition, ZOH, R-peak HR + RMSSD | `acquisition.py`, `data_sources.derive_hr_hrv_from_ecg` | ✅ |
| 1 | EMA smoothing α=0.10/0.05/0.05 | `processing.py:_apply_ema` | ✅ |
| 2 | 120 s baseline buffer | `processing.py:_buffer_sample` | ✅ |
| 3 | 3σ outlier removal | `processing.py:_compute_personal_baselines` | ✅ |
| 4 | Personal baselines + σ_baseline frozen | `processing.py` + `fusion.calculate_baseline_sigma` | ✅ |
| 5 | Per-sample % deviation (HRV inverted) | `fusion.compute_s_instant` | ✅ |
| 6 | Weighted fusion 0.5 / 0.3 / 0.2 | `fusion.compute_s_instant` (Config) | ✅ |
| 7 | 50-sample rolling mean → S_t | `fusion.evaluate_state` | ✅ |
| 8 | thresh_mild = 1.33·σ, thresh_high = 2.28·σ — **fixed for session** | `fusion.set_thresholds`, called once from `main.py` | ✅ |
| 9a | Three states with same thresholds | `fusion.evaluate_state` | ✅ |
| 9b | Three modes (easy / moderate / intense) | `Config.MODE_RANGES`, `FusionEngine._apply_mode` | ✅ |
| 9c | Rate-based balloon (k_down=2·k_up, clamped) | `fusion.evaluate_state` | ✅ |
| 9d | Per-frame Unity lerp | n/a — Unity's responsibility | ✅ |
| 10 | 0–100 piecewise dashboard score | `fusion.evaluate_state` | ✅ |
| ✓ | Per-tick outputs on LSL | `output.UnityBridge` (12 channels) | 🟡 missing `baseline_status`, `elapsed_baseline_sec`, `mode_enum` |

---

## What remains before "done"

In priority order, with rough effort estimates:

### High-value, lab-relevant
1. **Real PLUX dry-run.** Flip `Config.DATA_SOURCE = 'real_plux'`, start OpenSignals, run `launcher.py`. Verify the deadman switch fires correctly when you unplug the device. ~30 min once hardware is available.
2. **Surface the diagnostic counters.** Add `invalid_sample_count` / `out_of_range_count` / `disconnect_warnings_issued` to the dashboard's Session Statistics panel — either via 3 more LSL channels or a sidecar JSON polled at 1 Hz. ~30 min.
3. **Three remaining spec outputs on LSL** (`baseline_status`, `elapsed_baseline_sec`, `mode_enum`). Unity needs these. ~20 min.

### Quality of life
4. **Session-review viewer.** `python session_review.py` that lists past `data/session_*.csv`, lets you pick one, replays the charts, exports a one-page PDF summary. ~1 hr.
5. **ECG plot.** Add a fourth chart that shows the actual ECG waveform — this is the "squiggly hospital monitor" view, useful for operators to visually confirm electrode contact. ~30 min.
6. **Move last hardcoded knobs.** R-peak height factor (0.6), dashboard color scheme, log directory — all currently hardcoded. ~15 min.

### Stretch
7. **Unity client.** Subscribes to `Biofeedback_State`, applies per-frame lerp, sends `mode` handshake at start. **`FusionEngine.set_mode()` is the runtime hook.** Effort: depends on Unity scene scope, hours to days.
8. **Live recalibration mode.** Optional ability to re-baseline mid-session (clinical use only; spec says baseline is frozen, so this would be a *new* operating mode rather than a change to the existing pipeline).

---

## What's modular and what isn't

Modular (swap with config edit only, no code touch):
- ✅ Data source — `Config.DATA_SOURCE = 'mock' | 'real_plux'`
- ✅ Mock file path — `Config.MOCK_DATA_FILE` (any OpenSignals export works; header parser self-configures)
- ✅ Session mode — `Config.SESSION_MODE` plus `set_mode()` runtime hook
- ✅ All math constants — EMA alphas, 3σ multiplier, fusion weights, threshold K's, mode ranges, rate constants C_DOWN/C_UP
- ✅ Physiological bounds — `Config.HR_MIN_BPM` etc.
- ✅ Disconnect-detection thresholds
- ✅ Dashboard Y-range halfranges
- ✅ Session timing — `BASELINE_SEC`, `LIVE_PHASE_MAX_SEC`, `PIPELINE_RATE`

Requires touching code:
- 🔧 LSL channel layout — `UnityBridge.CHANNELS` schema (output.py + dashboard.py both need updating to add a channel)
- 🔧 Dashboard color theme — RGB tuples in `dashboard.py:152-154`
- 🔧 R-peak detector tuning — bandpass orders, height factor (mostly Config, height factor still inline)
- 🔧 Logging directory — `"data/"` hardcoded in `session_manager.py`, `acquisition.py`, `processing.py`
- 🔧 New session mode — adding a 4th mode means editing `Config.MODE_RANGES` (just a dict entry) AND no other change needed

---

## Quick verification you can run right now

```powershell
# 1. Confirm degenerate-σ guard
python -c "import sys; sys.path.insert(0,'src'); from fusion import FusionEngine; fe=FusionEngine(); fe.set_thresholds(0.0)"
# Expected: 'WARN: σ_baseline=0.0 is degenerate. Using fallback σ=1.5.'

# 2. Confirm header parser works on both files
python -c "import sys; sys.path.insert(0,'src'); from data_sources import parse_opensignals_header as p; print(p('data/opensignals_2026-05-25_14-57-56.txt'))"
# Expected: fs_hz=200, sensor_order=['ECG','EDA']

# 3. Sample-rate sanity check from the file
python -c "import sys; sys.path.insert(0,'src'); import numpy as np; data=np.genfromtxt('data/opensignals_2026-05-25_14-57-56.txt',skip_header=3,invalid_raise=False); print(f'rows={data.shape[0]}, duration={data.shape[0]/200:.1f}s')"
# Expected: rows=104205, duration=521.0s

# 4. Run a full session and confirm no exceptions
python launcher.py
```
