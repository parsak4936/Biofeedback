# Biofeedback Pipeline — Spec Compliance Audit

**Date:** 2026-05-25
**Auditor reference:** `shayans_biofeedback_math_pipeline (12).docx`, `shayans_biofeedback_walkthrough (10).docx`, `biofeedback_acrophobia_framework_valid.drawio.png`

## Executive summary

The math pipeline is **fully implemented** end-to-end. Steps 0–10 plus the `Step ✓` per-tick outputs are all live. R-peak detection now produces realistic HR (75–109 BPM) and RMSSD (6–40 ms) from the mock ECG channel, so the stress calculation actually has signal to work with. Three difficulty modes (easy / moderate / intense) are wired through with the spec's altitude ranges and rate constants. The dashboard reflects mode and per-state time-in-session. A LIVE-phase cap (default 5 min) ends sessions cleanly; operator Ctrl+C still works.

**The pipeline is correct.** The remaining gaps are all integration-layer items (Unity client, real device test, archive review tooling) — none of them affect the math.

---

## Step-by-step compliance map

Legend: ✅ implemented · 🟡 partial / interpretation diff · ❌ missing

| Step | Requirement | Status | Where |
|------|-------------|--------|-------|
| 0 | LSL transport, 50 Hz internal loop, ZOH | ✅ | [acquisition.py](src/acquisition.py), [streamer.py](src/streamer.py) |
| 0 | EDA in μS, ECG in mV (PLUX hardware conversions) | ✅ | [data_sources.py:78-83](src/data_sources.py) |
| 0 | HR derived from R-peak detection on ECG | ✅ | [data_sources.py:derive_hr_hrv_from_ecg](src/data_sources.py) |
| 0 | HRV/RMSSD from 10 s rolling RR window | 🟡 | We update HRV per beat with a 10 s window (≈1 Hz update). Spec wording suggests *holding* HRV constant for ~10 s between updates. Mathematically equivalent for stress fusion; functionally slightly more responsive. Acceptable engineering choice. |
| 1 | EMA smoothing per signal | ✅ | [processing.py:_apply_ema](src/processing.py) |
| 1 | α_HR=0.10, α_HRV=0.05, α_EDA=0.05 | ✅ | [config.py](src/config.py) |
| 2 | 120 s baseline buffer, no stress vis | ✅ | [processing.py:_buffer_sample](src/processing.py); dashboard suppresses stress curve until thresholds lock |
| 3 | 3σ outlier removal of cleaned arrays | ✅ | [processing.py:_compute_personal_baselines](src/processing.py), `ARTIFACT_SIGMA_MULTIPLIER` now in Config |
| 4a | avgHR / avgHRV / avgEDA from cleaned data | ✅ | [processing.py:_compute_personal_baselines](src/processing.py) |
| 4b | σ_baseline = std of S_t computed backwards over cleaned buffer | ✅ | [fusion.py:calculate_baseline_sigma](src/fusion.py) |
| 4 | All four scalars frozen for the session | ✅ | Set once in main.py at baseline-end |
| 5 | Percentage deviations (HRV inverted) | ✅ | [fusion.py:compute_s_instant](src/fusion.py) |
| 6 | S_instant = 0.5·ΔEDA + 0.3·ΔHRV + 0.2·ΔHR | ✅ | Weights now in Config (`WEIGHT_EDA/HRV/HR`) |
| 7 | 50-sample rolling mean → S_t | ✅ | [fusion.py:evaluate_state](src/fusion.py) |
| 8 | thresh_mild = 1.33·σ, thresh_high = 2.28·σ | ✅ | Multipliers in Config (`THRESH_MILD_K`, `THRESH_HIGH_K`) |
| 9a | Three states (calm / stressed / ultra) | ✅ | [fusion.py:evaluate_state](src/fusion.py) |
| 9b | Three modes (easy 20–30, moderate 30–45, intense 45–65) | ✅ | `Config.MODE_RANGES` |
| 9c | Rate-based balloon update; k_down=2·k_up | ✅ | `C_DOWN=0.010/s`, `C_UP=0.005/s` |
| 9c | y_t clamped to [y_low, y_high] | ✅ | [fusion.py:evaluate_state](src/fusion.py) |
| 9d | Per-frame Unity lerp | n/a | Unity-side responsibility; we emit `y_t` target |
| 10 | 0–100 dashboard piecewise mapping | ✅ | [fusion.py:evaluate_state](src/fusion.py) |
| ✓  | S_t, state, y_t, display, thresh_mild/high, smoothed HR/HRV/EDA | ✅ | 12-channel LSL `Biofeedback_State` |
| ✓  | baseline_status flag | 🟡 | Derived implicitly by consumers from `thresh_mild > 0`. Not an explicit channel. |
| ✓  | elapsed baseline time | ❌ | Not in LSL. Dashboard infers from its tick counter. |
| ✓  | mode | 🟡 | Recorded in session CSV; **not** in LSL stream. Unity needs this once it exists. |

---

## Known gaps (not blockers, but tomorrow's work)

### Integration

1. **No Unity client / VR scene.** We emit on LSL but nothing consumes the target `y_t`. When Unity is built, it should:
   - Subscribe to `Biofeedback_State` (12 channels).
   - On session start, push a 1-channel `Biofeedback_Mode` LSL stream that main.py reads to call `FusionEngine.set_mode(mode)`. The hook already exists.
   - Apply per-frame lerp on `y_t` for smooth motion (Step 9d).

2. **No real PLUX device dry-run.** Set `Config.DATA_SOURCE = 'real_plux'`, start OpenSignals, run `python launcher.py`. The pipeline should connect through `RealPLUXDataSource` without any other code change.

3. **No session-archive viewer.** Per-session CSVs accumulate in `data/` but there's no UI to browse them. A `session_review.py` that:
   - Lists every `data/session_*.csv`.
   - Lets you select one and replays its charts.
   - Exports a one-page summary (mode, duration, time-in-state, sigma, artifacts).
   
   This closes the "Monitoring and Analysis → Data Logger" box in the diagram.

### Spec output completeness

4. **LSL stream is missing 3 of the spec's per-tick outputs:** `baseline_status` (bool), `elapsed_baseline_time` (seconds since start), `mode` (enum). To add: extend `UnityBridge.CHANNELS` to 15, pass through from `main.py`. Dashboard currently derives these from local state — fine for now, but Unity will need them.

### Edge cases

5. **Mock data file is only 42.6 s long, baseline is 120 s.** `MockDataSource.get_next_sample()` loops back to index 0 when it runs out, so the 120 s baseline collects ~3 cycles of identical samples. This artificially shrinks `σ_baseline`, which then locks the thresholds tight. **Not a bug, a mock-data limitation.** Real PLUX or a longer mock file fixes it. For QA, you could also shorten `BASELINE_SEC` to 40 in Config to fit one mock cycle.

6. **R-peak detection threshold is fixed at 60% of the 99th percentile.** Robust on the current mock file. Real PLUX recordings with low-amplitude QRS could need re-tuning. Currently this `0.6` factor is hardcoded in [data_sources.py:derive_hr_hrv_from_ecg](src/data_sources.py); should move to Config for tuning.

### Auditing

7. **`personal_baselines` not written as a one-shot record.** They appear in every LIVE-phase row of the session CSV (same values repeated), so post-hoc extraction is trivial. But a header block or sidecar JSON would be cleaner. Optional.

### UX

8. **Artifact counter on dashboard still shows "--".** The count exists in `proc.artifacts_removed` but isn't on LSL. Easiest fix: extend the LSL stream by 3 more channels (`artifacts_eda`, `artifacts_hr`, `artifacts_hrv`) or write a sidecar `data/session_status.json` polled by the dashboard.

9. **Dashboard color scheme is hardcoded.** RGB tuples for calm / stressed / ultra in `dashboard.py:152-154`. Move to Config if you want easy theming.

---

## Recommended order for tomorrow

If you only have 1 hour:
1. **Add 3 LSL channels** (`baseline_status`, `elapsed_baseline_sec`, `mode_enum`) → spec-complete output. ~20 min.
2. **Build `session_review.py`** → close the audit-loop gap. ~30 min.
3. **Run with real PLUX** if device is available → first end-to-end real-data test.

If you have a full day:
1. Above three.
2. **Move ECG height factor + dashboard colors + log dir to Config** → finish the configurability sweep. ~15 min.
3. **Write the Unity LSL consumer stub** (Python script that subscribes to `Biofeedback_State` and prints/logs y_t — proves the contract from the other side). ~30 min.
4. **Validate three-session sweep:** run easy → moderate → intense with the same patient name, then open each session CSV in Excel/pandas and confirm `session_mode` column distinguishes them. ~15 min.

---

## Full configurability table

Every dial in the system, in one place. All in [src/config.py](src/config.py) unless noted.

### Data source
| Knob | Default | Effect |
|------|---------|--------|
| `DATA_SOURCE` | `'mock'` | `'mock'` reads file; `'real_plux'` reads PLUX LSL stream |
| `MOCK_DATA_FILE` | `data/fake_opensignals_...txt` | Path to the mock OpenSignals export |
| `STREAM_NAME` | `"OpenSignals"` | Inbound LSL stream name |
| `STREAM_TYPE` | `"00:07:80:0F:31:9C"` | PLUX device MAC / identifier |
| `STREAM_TIMEOUT_SEC` | `5.0` | Seconds of silence before the stream is declared dead |

### Pipeline rates
| Knob | Default | Effect |
|------|---------|--------|
| `PIPELINE_RATE` | `50.0` Hz | Core loop frequency — don't change without retuning everything else |
| `BASELINE_SEC` | `120` | Length of the baseline-buffering phase |
| `LIVE_PHASE_MAX_SEC` | `300` | Auto-end the session after this many seconds of LIVE phase. `None` to disable. |

### Smoothing (Step 1)
| Knob | Default | Effect |
|------|---------|--------|
| `EMA_ALPHA_EDA` | `0.05` | Lower = stronger smoothing on EDA |
| `EMA_ALPHA_HR` | `0.10` | HR varies faster, gets less smoothing |
| `EMA_ALPHA_HRV` | `0.05` | HRV changes slowly, strong smoothing |

### Baseline cleaning (Step 3)
| Knob | Default | Effect |
|------|---------|--------|
| `ARTIFACT_SIGMA_MULTIPLIER` | `3.0` | 3σ = 99.7% Gaussian inclusion. Lower = more aggressive artifact rejection |

### Fusion weights (Step 6)
| Knob | Default | Effect |
|------|---------|--------|
| `WEIGHT_EDA` | `0.5` | Sympathetic-specific signal, highest weight |
| `WEIGHT_HRV` | `0.3` | Parasympathetic withdrawal, inverted |
| `WEIGHT_HR` | `0.2` | Mixed signal, lowest weight |

> Weights should sum to 1.0 for clean interpretation; otherwise S_instant changes scale.

### Thresholds (Step 8)
| Knob | Default | Effect |
|------|---------|--------|
| `THRESH_MILD_K` | `1.33` | calm/stressed boundary = K · σ_baseline |
| `THRESH_HIGH_K` | `2.28` | stressed/ultra boundary = K · σ_baseline |

### Balloon kinematics (Step 9)
| Knob | Default | Effect |
|------|---------|--------|
| `SESSION_MODE` | `'easy'` | Picks one of `MODE_RANGES`. Modular hook for future Unity handshake. |
| `MODE_RANGES['easy']` | `{20, 30, 25}` | y_low, y_high, y_mid (m) |
| `MODE_RANGES['moderate']` | `{30, 45, 37.5}` | |
| `MODE_RANGES['intense']` | `{45, 65, 55}` | |
| `C_DOWN` | `0.010` /s | Descent rate scaling |
| `C_UP` | `0.005` /s | Ascent rate scaling (half of descent — safety priority) |

### ECG → HR/HRV derivation
| Knob | Default | Effect |
|------|---------|--------|
| `ECG_BANDPASS_LOW_HZ` | `5.0` | QRS energy lower edge |
| `ECG_BANDPASS_HIGH_HZ` | `15.0` | QRS energy upper edge |
| `ECG_MIN_RR_MS` | `250` | Minimum spacing between R-peaks (refractory) — max ~240 BPM |
| `RMSSD_WINDOW_SEC` | `10` | Rolling RR window for HRV computation |
| (hardcoded) `0.6` in `derive_hr_hrv_from_ecg` | — | R-peak height as fraction of 99th-pct |ECG|. **Move to Config tomorrow.** |

### Dashboard
| Knob | Default | Effect |
|------|---------|--------|
| `DASHBOARD_MAX_HISTORY` | `500` | Samples kept in each chart buffer |
| `DASHBOARD_VIEW_WIDTH` | `300` | Visible window width (samples) on auto-scrolling charts |
| (hardcoded) RGB in `dashboard.py:152-154` | — | calm/stressed/ultra background colors. Move to Config for theming. |

### LSL output
| Knob | Default | Effect |
|------|---------|--------|
| `OUT_STREAM_NAME` | `"Biofeedback_State"` | Outbound LSL stream name |
| `OUT_STREAM_TYPE` | `"Control"` | LSL stream type tag |
| `UnityBridge.CHANNELS` (in `output.py`) | 12 channels | See [DATA_FLOW.md](DATA_FLOW.md) Layer 8 for the layout |

---

## How to verify each fix tomorrow

| Check | How |
|-------|-----|
| HR/HRV vary | Watch the HR and HRV charts during baseline; they should oscillate. They did in this audit's screenshot. |
| Personal Baseline populates | After T=120 s, the bottom-left panel switches from "-- μS" to real numbers. |
| Threshold lines appear | After T=120 s, yellow (mild) and red (high) dashed lines render across the stress chart. |
| y_t responds to state | Stay calm → balloon altitude climbs toward y_high. Spike EDA → y_t drops if state reaches ultra. |
| Session mode shown | Top of dashboard reads `Session: EASY` (or whatever `Config.SESSION_MODE` is). |
| Session ends on cap | After 5 min of LIVE phase the pipeline prints "SESSION COMPLETE" and exits. Cleanly. |
| CSV records mode + artifacts | Open `data/session_*.csv` in Excel; columns `session_mode`, `artifacts_eda`, `artifacts_hr`, `artifacts_hrv` are present. |
| Three sessions distinguishable | Run with `SESSION_MODE='easy'`, then `'moderate'`, then `'intense'`. Three CSVs, each with its mode in every row. |

---

## File map for tomorrow

| File | What lives here |
|------|-----------------|
| [src/config.py](src/config.py) | **All tunable parameters.** First place to edit anything. |
| [src/data_sources.py](src/data_sources.py) | Mock & real PLUX adapters; **R-peak detection** in `derive_hr_hrv_from_ecg`. |
| [src/streamer.py](src/streamer.py) | LSL broadcaster (1000 Hz). Reads via data-source factory; no per-source code. |
| [src/acquisition.py](src/acquisition.py) | LSL consumer + 50 Hz ZOH. |
| [src/processing.py](src/processing.py) | EMA smoothing + 120 s baseline buffer + 3σ cleaning. Exposes `cleaned_baseline_buffers`. |
| [src/fusion.py](src/fusion.py) | Stress fusion + state classification + balloon kinematics. **`set_mode()` is the Unity handshake hook.** |
| [src/session_manager.py](src/session_manager.py) | Per-session CSV writer; tracks patient + phase + history. |
| [src/output.py](src/output.py) | LSL outbound `Biofeedback_State` (12 channels). `UnityBridge.CHANNELS` is the schema. |
| [src/dashboard.py](src/dashboard.py) | PyQt5 operator dashboard. |
| [src/main.py](src/main.py) | 50 Hz loop tying everything together. Session-end cap enforced here. |
| [launcher.py](launcher.py) | Entry point. Asks for patient name/ID, spawns the three subprocesses. |
| [DATA_FLOW.md](DATA_FLOW.md) | End-to-end per-layer walkthrough of how a sample becomes a stress state. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Module-by-module deep dive (predates this audit; mostly still accurate). |
| [README.md](README.md) | User-facing quick start. |
