# Biofeedback Data Flow — From Raw File to Stress State

This document traces a single sample from the mock OpenSignals file all the way to the balloon altitude `y_t`, so the pipeline matches the architecture diagram (`biofeedback_acrophobia_framework_valid.drawio.png`) and the math-pipeline / walkthrough documents.

## Layer 0 — The raw mock file

`data/fake_opensignals_2026-05-13_15-24-44.txt` is a real PLUX OpenSignals export. Three header lines followed by tab-separated samples at 1000 Hz:

```
# OpenSignals Text File Format. Version 1
# {"00:07:80:0F:31:9C": {... "channels": [1, 2], "sensor": ["EDA", "ECG"], ...}}
# EndOfHeader
0    0    37416    33726
1    0    37424    33783
2    0    37416    33791
...
```

Columns (matching the JSON `column` field):

| Index | Field | Meaning |
|-------|-------|---------|
| 0 | `nSeq` | Sample counter (0, 1, 2, ...) |
| 1 | `DI` | Digital IO bit |
| 2 | `CH1` | Raw EDA ADC (16-bit) |
| 3 | `CH2` | Raw ECG ADC (16-bit) |

The file contains **only EDA and ECG**. HR and HRV/RMSSD do not exist as columns — they must be **derived** from the ECG channel. This is exactly what the framework diagram's "Python Middleware: HR, HRV/RMSSD, EDA, Baseline" box demands.

## Layer 1 — ADC → physical units (`src/data_sources.py`)

`MockDataSource.__init__` loads the file with `np.loadtxt(skiprows=3)` and applies the PLUX hardware conversion formulas:

```python
self.eda_uS = (data[:, 2] / 65536) * 3.0 / 0.132          # μS
self.ecg_mV = ((data[:, 3] / 65536) - 0.5) * 3.0 / 1100 * 1000  # mV
```

`65536` is the 16-bit ADC full-scale, `3.0` is the V_ref, `0.132` is the EDA sensor transfer constant, and the ECG formula recenters around zero and scales to mV.

## Layer 2 — ECG → HR + HRV (`derive_hr_hrv_from_ecg`)

Per math-pipeline Step 0, the server is responsible for turning ECG into HR (BPM) and HRV/RMSSD (ms). At file load time:

1. **Bandpass filter** the ECG at 5–15 Hz (the QRS energy band) with a second-order Butterworth (`scipy.signal.filtfilt`).
2. **R-peak detection** with `scipy.signal.find_peaks`, height = 60th percentile of |ECG|, minimum spacing = `Config.ECG_MIN_RR_MS` (250 ms = max ~240 BPM).
3. **RR intervals**: `np.diff(peaks) * (1000 / fs)` gives the inter-beat interval in ms.
4. **RR sanitation**: any interval more than 50% off the median is replaced by the median, to suppress missed-beat artifacts that would otherwise inflate RMSSD.
5. **HR per beat**: `60_000 / RR_ms` BPM.
6. **RMSSD**: `sqrt(mean(diff(RR_window)^2))` over a rolling 10-second RR window (`Config.RMSSD_WINDOW_SEC`).
7. **Zero-Order Hold**: between successive R-peaks the HR/HRV value is held constant until the next beat (matches the walkthrough's description — HR holds for ~50 ticks between beats, HRV for ~500 ticks).

The result is two per-sample arrays `hr_series` and `hrv_series` of the same length as the EDA trace.

`get_next_sample()` then simply emits `(eda_uS[i], hr_series[i], hrv_series[i])` once per millisecond.

## Layer 3 — LSL broadcast (`src/data_sources.py` → `src/acquisition.py`)

The data source pushes 3-channel samples (`[EDA, HR, HRV]`) onto the LSL stream named `Config.STREAM_NAME` ("OpenSignals") at 1000 Hz.

`BiofeedbackAcquisition` (running inside `main.py`) pulls from that stream **at 50 Hz** with a Zero-Order Hold strategy. If no new sample arrives in a tick, the last value is held; if 5 s pass with no data, a `ConnectionError` is raised.

## Layer 4 — Smoothing + baseline (`src/processing.py`)

Per math-pipeline Step 1:

```
y_t = α·x_t + (1 − α)·y_(t−1)
```

with `α_EDA = 0.05`, `α_HR = 0.10`, `α_HRV = 0.05` (from `Config`).

For the first 120 s (`BASELINE_SEC * PIPELINE_RATE = 6000 ticks`) the smoothed samples accumulate in `self.buffers['eda' | 'hr' | 'hrv']`.

At T = 120 s, `_compute_personal_baselines()` runs **once**:

1. For each signal, compute `μ` and `σ`.
2. Keep only samples within `μ ± 3σ` — math-pipeline Step 3, the 99.7 % inclusion interval of a normal distribution.
3. Average the cleaned array → `personal_averages[signal]` (Step 4a).
4. Store cleaned arrays in `cleaned_baseline_buffers[signal]` so fusion can compute σ_baseline.
5. Record how many samples were rejected (`artifacts_removed[signal]`).

### Per-person uniqueness

`personal_averages` is computed **at runtime** from the first 120 s of *this specific session's* data. Two different participants produce two different baselines because their resting EDA, HR, and HRV differ. In mock mode the file is the same on every run, so a given mock patient always produces the same numbers — but with real PLUX hardware the baseline is genuinely per-person.

## Layer 5 — Dynamic σ_baseline (`src/fusion.py`)

Math-pipeline Step 4b says: to set thresholds you need the standard deviation of S_t **as if the live pipeline had been running during baseline**.

`FusionEngine.calculate_baseline_sigma(cleaned_buffers, personal_averages)`:

1. Iterates over the cleaned baseline arrays sample-by-sample.
2. Calls `compute_s_instant([eda, hr, hrv], personal_averages)` on each → 6000-sample `S_instant` series.
3. Convolves with a 50-sample box filter (1-second rolling mean per Step 7) → `S_t` series.
4. Returns `np.std(S_t_series)` — this is **σ_baseline**.

Thresholds (Step 8):

```
thresh_mild = 1.33 · σ_baseline
thresh_high = 2.28 · σ_baseline
```

Both are frozen for the rest of the session — they do not adapt during live, by design, to avoid masking the very stress responses they exist to detect.

## Layer 6 — Live stress fusion (`src/fusion.py`)

On every LIVE tick:

1. **Percentage deviation** (Step 5, HRV inverted):
   ```
   ΔEDA = (EDA − avgEDA) / avgEDA · 100
   ΔHR  = (HR  − avgHR ) / avgHR  · 100
   ΔHRV = (avgHRV − HRV) / avgHRV · 100   ← inverted
   ```
2. **Weighted fusion** (Step 6): `S_instant = 0.5·ΔEDA + 0.3·ΔHRV + 0.2·ΔHR`.
3. **1-second rolling mean** (Step 7): `S_t = mean(S_instant_buffer[-50:])`.
4. **State classification** (Step 9a):
   - `S_t > thresh_high` → `ultra_stressed`
   - `thresh_mild < S_t ≤ thresh_high` → `stressed` (goal zone)
   - otherwise → `calm`
5. **Balloon update** (Step 9c):
   - ultra_stressed → `y_t −= k_down · dt`  (relief, fast)
   - stressed → hold
   - calm → `y_t += k_up · dt`  (re-exposure, gentle)
   - clamp to `[y_low, y_high]`
6. **Dashboard score** (Step 10): piecewise 0–100 mapping of S_t against the thresholds.

## Layer 7 — Session mode

`Config.SESSION_MODE` selects one of:

| Mode | y_low | y_high | y_mid | range |
|------|-------|--------|-------|-------|
| easy | 20 m | 30 m | 25 m | 10 m |
| moderate | 30 m | 45 m | 37.5 m | 15 m |
| intense | 45 m | 65 m | 55 m | 20 m |

`FusionEngine._apply_mode()` reads the chosen mode from `Config.MODE_RANGES` and scales the rate constants per the math-pipeline:

```
k_down = C_DOWN · (y_high − y_low)
k_up   = C_UP   · (y_high − y_low)
```

with `C_DOWN = 0.010 /s` and `C_UP = 0.005 /s` — the asymmetry (descent twice as fast as ascent) is deliberate: relief from acute overwhelm is a safety priority, while re-exposure is therapeutic dosing.

**Modular hook for future Unity handshake.** Right now the mode is set in `Config.SESSION_MODE`. When a Unity client exists, it will publish the mode on an LSL handshake channel at session start; `main.py` will then call `fusion.set_mode(mode_name)` once before entering the 50 Hz loop. No other code change is needed — the dashboard's "Session: EASY" label and the balloon range will both update.

## Layer 8 — Output stream (`src/output.py`)

Every 50 Hz tick `UnityBridge.broadcast_state(...)` pushes a 12-channel vector on LSL stream `Biofeedback_State`:

| # | Channel | Meaning |
|---|---------|---------|
| 0 | s_t | Smoothed stress index |
| 1 | state_enum | 0=calm, 1=stressed, 2=ultra_stressed |
| 2 | dashboard_score | 0–100 operator display |
| 3 | y_t | Balloon target altitude (m) |
| 4 | eda | Smoothed EDA (μS) |
| 5 | hr | Smoothed HR (BPM) |
| 6 | hrv | Smoothed RMSSD (ms) |
| 7 | avg_eda | Personal baseline EDA |
| 8 | avg_hr | Personal baseline HR |
| 9 | avg_hrv | Personal baseline RMSSD |
| 10 | thresh_mild | Locked mild threshold |
| 11 | thresh_high | Locked high threshold |

Channels 7–11 are zero during the baseline and become live the instant calibration completes.

## Layer 9 — Persistence

Three CSV files are written per session, all under `data/`:

| File | Source | Contents |
|------|--------|----------|
| `acquisition_log_{ts}.csv` | `acquisition.py` | Raw EDA/HR/HRV per 50 Hz tick + ZOH status (NEW_DATA / HOLD_LAST) |
| `processing_log_{ts}.csv` | `processing.py` | Smoothed EDA/HR/HRV per tick + phase (BASELINE / LIVE) |
| `session_{ts}_{patient}.csv` | `session_manager.py` | Full row per tick: patient, phase, raw signals, s_instant, s_t, state, score |

The third file is the canonical audit record per participant.

## Session lifecycle

```
T=0   Launch
T=0   Stream resolves, baseline buffering begins
T=120 _compute_personal_baselines() runs once → averages locked
T=120 calculate_baseline_sigma() runs once → thresholds locked
T=120 Phase transitions BASELINE → LIVE
...   Live therapy: S_t computed every tick, y_t updates, dashboard reflects state
T=120+Config.LIVE_PHASE_MAX_SEC   Auto-terminate (default 5 min LIVE; configurable)
*OR*  Operator presses Ctrl+C   Graceful terminate at any time
```

Both termination paths flush the session CSV and print the output filename.
