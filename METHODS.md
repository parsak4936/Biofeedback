# Biofeedback Pipeline — Methods Reference

VRET acrophobia therapy with real-time biofeedback. This document covers,
in one place, every formula the pipeline applies to the raw biosignals
coming off the PLUX biosignalsplux hub, both backend variants the
operator can switch between, and the rationale for each choice.

Two reference implementations sit behind the pipeline:

- **NeuroKit2 backend** (default, active). Mirrors Shayan Itami's
  reference server `vret_server.py` (NISC Lab, University of Messina).
- **biosignalsnotebooks backend** (switchable). Implements HR / HRV /
  EDA extraction using only functions from the official PLUX
  `biosignalsnotebooks` package (`detect`, `extract`, `process`),
  per the professor's instruction to follow the procedures in
  <https://github.com/pluxbiosignals/biosignalsnotebooks>.

Both backends feed the **same fusion / threshold / classification
math**. Swapping backends does not change the stress score formula —
only the way HR / RMSSD / EDA are derived from raw ECG and EDA.

---

## 1. Signal acquisition

### 1.1 Hardware

| Component | Detail |
|---|---|
| Hub | PLUX biosignalsplux, 16-bit ADC, V_ref = 3.0 V |
| Sensors | ECG (3 leads), EDA (2 finger pads) |
| Transport | Bluetooth → OpenSignals → Lab Streaming Layer (LSL) |

### 1.2 Sampling rates

| Stage | Rate | Notes |
|---|---|---|
| Raw sensor sampling | **200 Hz** | configured in OpenSignals; matches biosignalsnotebooks examples |
| Internal pipeline tick | **10 Hz** | `Config.PIPELINE_RATE`; runs the fusion/state machine |
| Unity VR command rate | **1 Hz (max)** | `Config.UNITY_COMMAND_INTERVAL_SEC`; per professor's spec |

### 1.3 Where the ADC actually is

There are **two distinct input formats** depending on the source:

| Source | What our code receives | What our code does |
|---|---|---|
| **Live OpenSignals LSL** | Pre-converted physical units (uS for EDA, mV for ECG) | Treat as-is — no calibration formula applied |
| **Mock replay from `.txt` file** | Raw ADC integers (0–65535) | Apply biosignalsnotebooks calibration formulas |

This was the source of the long debugging chase in June 2026. OpenSignals's LSL
bridge silently sends the already-converted values (the same numbers the
OpenSignals UI displays), not raw ADC. The flag `Config.LSL_VALUES_PRECONVERTED`
controls this behaviour (default `True`).

### 1.4 ADC → physical unit conversion (mock mode only)

From `biosignalsnotebooks.conversion.raw_to_phy` for
`device='biosignalsplux'`:

| Sensor | Constants | Formula |
|---|---|---|
| EDA | vcc = 3.0, offset = 0, gain = 0.12 | `EDA_uS = (raw * 3.0 / 65536 - 3.0 * 0) / 0.12` |
| ECG | vcc = 3.0, offset = 0.5, gain = 1.019 | `ECG_mV = (raw * 3.0 / 65536 - 3.0 * 0.5) / 1.019` |

The general transfer function for any PLUX sensor:
```
physical = (raw * vcc / 2^resolution − vcc * offset) / gain
```

### 1.5 Channel resolution

LSL channel labels are read at startup. The data source picks the channel
whose label contains `"EDA"` or `"ECG"` (case-insensitive substring). If
labels are missing, a content-based fingerprint test runs:

- variance check (drops the digital-input line and sample counter)
- monotonicity check (drops the nSeq counter)
- beat-plausibility check on each candidate (real ECG produces RR
  intervals in 300–1500 ms; EDA forced through the detector does not)
- EDA µS-range check after the converter (real resting EDA in 0.1–30 µS)

A startup banner prints the chosen mapping along with the first second
of raw values for operator verification.

---

## 2. ECG → HR and HRV (RMSSD)

### 2.1 NeuroKit2 backend (default)

Verbatim chain — identical to `vret_server.py`:

```
ECG raw
   → nk.ecg_clean(ecg, sampling_rate)
        (0.5 Hz high-pass + 50 Hz powerline notch; cleaning is mandatory:
         nk.ecg_peaks finds zero peaks on raw mains-contaminated ECG)
   → nk.ecg_peaks(cleaned, sampling_rate, correct_artifacts=True)
        (Kubios artifact correction — Lipponen & Tarvainen 2019 —
         interpolates missed beats)
   → nk.ecg_rate(peaks, sampling_rate) → HR
        (alias of signal_rate; computes 60 / period between R-peaks)
   → _gated_rmssd_from_peaks(R_peaks, fs) → RMSSD
        (see §2.3 — Kubios + Malik 20% gate)
```

**Windows**

| Quantity | Trailing window | Recompute interval |
|---|---|---|
| HR | 30 s (`Config.HR_WINDOW_SEC`) | every 0.5 s |
| RMSSD | 60 s (`Config.RMSSD_WINDOW_SEC`) | every 0.5 s |

Before each window has filled (HR ~3 s, RMSSD 60 s), the value is
emitted as `NaN`. Downstream fusion substitutes the baseline mean
during warm-up so the score keeps its scale (see §5.2).

### 2.2 biosignalsnotebooks backend (switchable)

Strict implementation using only library functions:

```
ECG raw
   → detect.detect_r_peaks(ecg, fs)            # Pan-Tompkins (Selvaraj)
        (BP 5-15 Hz → differentiate → square → 80 ms integration
         → adaptive double-threshold)
   → detect.tachogram(peaks, fs)               # RR interval series
   → extract.remove_ectopy(rr, t)              # 20% adjacent-RR rule
        (compares RR_i to RR_{i-1}; when violated, removes TWO
         consecutive entries — Lippman, Stein & Lerman 1994)
   → HR  = 60 / mean(cleaned_RR_s)
   → RMSSD = sqrt(mean(diff(cleaned_RR_ms)**2))
```

**Notable**: biosignalsnotebooks does **not** expose RMSSD directly.
`extract.hrv_parameters` returns SDNN, SD1, SD2, NN20, pNN20, NN50,
pNN50, and frequency-domain power bands — but no RMSSD. The textbook
formula is applied here on the cleaned RR series the library does
expose.

This backend **deliberately omits** the Malik 20 % gate, the RR band
filter, and the RMSSD plausibility band the NeuroKit2 path applies.
Ectopy is handled exclusively by `remove_ectopy` upstream. Choice of
the professor.

### 2.3 RMSSD artifact gate (NeuroKit2 backend only)

After Kubios correction, additionally apply the Task Force 1996 /
ESC-NASPE Malik rule:

```
rr_ms      = diff(R_peaks) * 1000 / fs
rr_ms      = rr_ms in [RR_MIN_MS, RR_MAX_MS]            # [300, 1500] ms
rel_change = |diff(rr_ms)| / rr_ms[:-1]
diffs      = diff(rr_ms) where rel_change ≤ 0.20        # Malik 20% rule
RMSSD      = sqrt(mean(diffs ** 2))                     # surviving diffs
RMSSD      = RMSSD in [RMSSD_MIN_MS, RMSSD_MAX_MS]      # [5, 300] ms
```

Reference: Malik M. et al., Heart rate variability: standards of
measurement, *Eur Heart J* 17(3):354–381 (1996).

---

## 3. EDA processing

### 3.1 NeuroKit2 backend (default)

Phasic decomposition via convex optimisation:

```
raw_eda_window (rolling, 60 s)
   → nk.signal_resample(window, source_rate, 10 Hz)
   → nk.eda_clean(downsampled, 10 Hz)
   → nk.eda_phasic(cleaned, 10 Hz)['EDA_Phasic']
   → take the LAST sample = current phasic uS
   → reject if |phasic| > 1.0 µS (resampler-edge ringing artifact)
```

Constants:
- Decomposition rate: 10 Hz
- Update interval: 0.5 s
- Phasic ceiling: 1.0 µS (artifact rejection)

### 3.2 biosignalsnotebooks backend (switchable)

Two sub-modes via `Config.EDA_BSNB_METHOD`:

| Sub-mode | What it returns | Origin |
|---|---|---|
| `'raw'` (default) | The live EDA µS value itself, no decomposition | Mirrors `DataAnalyze.py` exactly |
| `'lowpass_subtract'` | `phasic = raw − lowpass(raw, 0.05 Hz)` | biosignalsnotebooks EDA tutorial |

biosignalsnotebooks has no phasic / tonic decomposition function;
`'lowpass_subtract'` reconstructs the tutorial recipe from
`process.lowpass`. 0.05 Hz is the standard SCL/SCR separation cut-off
in the EDA literature.

---

## 4. Baseline calibration (120 seconds at rest)

### 4.1 Per-signal averages

| Stat | Formula |
|---|---|
| `avg_EDA` | `mean(EDA_samples)` (3σ outlier removal applied) |
| `avg_HR` | `nk.ecg_rate(peaks, fs) → mean` over the cleaned baseline ECG |
| `avg_HRV` | `_gated_rmssd_from_peaks(...)` over the cleaned baseline ECG |

### 4.2 Per-signal sigmas

Sigma is the standard deviation of each signal's **delta** during
baseline. These divide the live delta when z-scoring (§5.1).

```
sigma_EDA  = std(phasic_EDA_baseline)            # uS
sigma_HR   = std((HR_baseline - avg_HR) / avg_HR * 100)        # %
sigma_HRV  = std((avg_HRV - HRV_baseline) / avg_HRV * 100)     # % (inverted)
```

### 4.3 Sigma floors (anti-false-alarm guards)

If a baseline is unusually flat, sigma collapses near zero and a
trivial live wobble becomes a huge z-score. Each sigma is clamped
upward to a physiological minimum:

| Sigma | Floor | Justification |
|---|---|---|
| `HR_SIGMA_FLOOR_PCT` | **2.0 %** | resting HR realistic spread |
| `HRV_SIGMA_FLOOR_PCT` | **5.0 %** | resting RMSSD spread |
| `EDA_PHASIC_SIGMA_FLOOR` | **0.02 µS** | phasic-EDA spread floor |
| `SIGMA_FLOOR` | **1e-6** | absolute divide-by-zero guard |

A warning prints if a measured sigma was below its floor — the floor
kept the score sane, but a sigma this small can also indicate a
baseline-collection problem worth checking.

---

## 5. Stress index

### 5.1 Per-signal deltas

Computed every tick during live:

```
delta_EDA  = phasic_EDA_now                                  # µS
delta_HR   = (HR_live  - avg_HR)  / avg_HR  * 100            # %, increase = stress
delta_HRV  = (avg_HRV  - HRV_live) / avg_HRV * 100           # %, decrease = stress (INVERTED)
```

The HRV inversion encodes physiology: parasympathetic withdrawal under
stress shortens vagally-mediated beat-to-beat variability, so a lower
RMSSD contributes positively to the stress index.

### 5.2 Z-scoring

Each delta is divided by its own baseline sigma so the three signals
share a common scale before the weights apply:

```
z_EDA = (delta_EDA - eda_mean) / eda_sigma
z_HR  = (delta_HR  - hr_mean)  / hr_sigma
z_HRV = (delta_HRV - hrv_mean) / hrv_sigma
```

### 5.3 Weighted composite (Moldoveanu 2023)

```
S_instant = 0.5 * z_EDA + 0.3 * z_HRV + 0.2 * z_HR
S_t       = mean of S_instant over the last 1 second (10 samples at 10 Hz)
```

Reference: Moldoveanu A. et al., *Immersive Phobia Therapy through
Adaptive Virtual Reality and Biofeedback*, Applied Sciences 13(18)
:10365 (2023).

### 5.4 Warm-up behaviour

| Phase | What is missing | What we do |
|---|---|---|
| First ~3 s of live | HR not yet computed | omit HR term, renormalise weights so score stays on scale |
| First 60 s of live | RMSSD window not filled | `delta_HRV = hrv_mean` (z = 0, no assumed deviation) |

No faked values, no dead minute.

---

## 6. Thresholds and state classification

### 6.1 Personal thresholds

Frozen at baseline lock:

```
MILD = mean_baseline_S_t + 1.28 * sigma_baseline_S_t          # 90th percentile (z)
HIGH = mean_baseline_S_t + 2.33 * sigma_baseline_S_t          # 99th percentile (z)
```

`sigma_baseline_S_t` is the standard deviation of the **raw
instantaneous** S_instant series during baseline, not the smoothed
S_t. Averaging shrinks variance by ~√N times; using the smoothed std
would compress the bands and every real arousal would skip past
"stressed" straight into "ultra-stressed". The mean is taken from the
smoothed series (matches the quantity classified live), the sigma
from the raw.

### 6.2 State classification

```
if S_t >= HIGH                    → state = "ultra_stressed"   → Unity: decrease
elif HIGH > S_t >= MILD           → state = "stressed"         → Unity: neutral (hold)
else                              → state = "calm"             → Unity: increase
```

The Unity command translates state to balloon-altitude policy:
calm = exposure advances, stressed = hold, ultra-stressed = back off.

---

## 7. Side-by-side comparison

When the operator wants a definitive answer to "how should I read this
HR / HRV / EDA number?", this is the table:

| Step | NeuroKit2 backend (default) | biosignalsnotebooks backend |
|---|---|---|
| ECG cleaning | `nk.ecg_clean` (0.5 Hz HP + 50 Hz notch) | internal to Pan-Tompkins (5–15 Hz bandpass) |
| R-peak detection | `nk.ecg_peaks(correct_artifacts=True)` (Kubios) | `detect.detect_r_peaks` (Pan-Tompkins / Selvaraj) |
| Ectopy / artifact handling | Kubios interpolation + Malik 20 % gate + plausibility bounds | `extract.remove_ectopy` (20 % adjacent-RR rule, removes pairs) |
| HR | `60 / mean(diff(R_peaks))` via `nk.ecg_rate` | `60 / mean(cleaned_RR_s)` from `tachogram` |
| RMSSD | `sqrt(mean(diffs²))` after Malik gate | `sqrt(mean(diffs²))` from `remove_ectopy` output (manual; library does not expose it) |
| HR window | 30 s trailing | 30 s trailing |
| RMSSD window | 60 s trailing | 60 s trailing |
| Recompute cadence | every 0.5 s | every 0.5 s |
| EDA phasic | `nk.eda_phasic` (cvxEDA convex optimisation) | `raw - lowpass(0.05 Hz)` (tutorial recipe) — or `raw` if `EDA_BSNB_METHOD='raw'` |
| EDA artifact clamp | yes (`|phasic|` ≤ 1 µS) | none (library decision) |

### 7.1 What the comparison runs show

On the `data/LiveTest/...` recording, both chains were verified offline
via `compare_backends.py`:

| Metric | Result |
|---|---|
| HR (Pearson r) | +0.76 |
| HR median | NeuroKit2 = 72.35 BPM, bsnb = 72.46 BPM (essentially identical) |
| RMSSD (Pearson r) | +0.87 on clean segments |
| RMSSD median | NeuroKit2 = 43.32 ms, bsnb = 108.04 ms (bsnb runs higher due to no Malik gate) |
| Stress (Pearson r) | +0.896 |

**Interpretation**: on clean ECG both backends produce indistinguishable
HR and well-correlated stress curves. RMSSD diverges by absolute value
because of the methodological difference in ectopy handling — that
divergence is documented and intentional.

### 7.2 Which one should I run?

The default is **NeuroKit2** because it is the chain validated against
Shayan's reference implementation (`vret_server.py`) and includes the
Task Force 1996 Malik gate that PLUX's own library does not enforce.

To switch to biosignalsnotebooks for an A/B comparison (e.g. before a
meeting with the professor):

```python
# src/config.py
HR_HRV_BACKEND = 'bsnb'        # default 'neurokit'
EDA_BACKEND    = 'bsnb'        # default 'neurokit'
EDA_BSNB_METHOD = 'raw'        # or 'lowpass_subtract'
```

Restart the launcher. Nothing else changes — fusion math, weights,
thresholds, dashboard, and CSV format are identical for both backends.

---

## 8. Pipeline-rate summary

| Stage | Rate / window | Source |
|---|---|---|
| Raw ECG / EDA sampling | 200 Hz | OpenSignals device config |
| R-peak / RMSSD recompute | every 0.5 s | `Config.HR_COMPUTE_INTERVAL_SEC` |
| HR window | 30 s trailing | `Config.HR_WINDOW_SEC` |
| RMSSD window | 60 s trailing | `Config.RMSSD_WINDOW_SEC` |
| EDA phasic decomposition (NeuroKit2) | every 0.5 s | `Config.EDA_PHASIC_UPDATE_INTERVAL_SEC` |
| Internal pipeline tick | 10 Hz | `Config.PIPELINE_RATE` |
| Stress smoothing window | 1 s | `Config.PIPELINE_RATE` × 1 |
| Baseline duration | 120 s | `Config.BASELINE_SEC` |
| Disk write (samples.csv) | 10 Hz | `Config.SAMPLES_CSV_RATE_HZ` |
| Unity VR command rate | 1 Hz max | `Config.UNITY_COMMAND_INTERVAL_SEC` |

---

## 9. Output files

One folder per session, under `data/`:

```
data/<first>_<last>_Session<n>_<YYYY-MM-DD>_<gender>/
    ├── metadata.json     intake + frozen baseline averages + thresholds
    ├── samples.csv       per-second clinical record (every signal, state)
    ├── diagnostic.csv    forensic raw + acquisition status per tick
    ├── unity_udp.csv     one row per UDP packet sent to Unity
    └── console.txt       full console log
```

---

## 10. Offline analysis tools

Companion scripts at the project root, all post-hoc:

| Script | What it does |
|---|---|
| `datasource_2.py <samples.csv>` | Applies Moldoveanu / `DataAnalyze.py` math to a saved session, plots stress + height with σ-multiple thresholds, saves PNG + intermediate CSV |
| `compare_backends.py <opensignals.txt>` | Runs both backends on the same raw recording, computes agreement metrics, saves comparison PNGs |
| `compare_backends.ipynb` | Jupyter version of the above with interactive cells |

---

## 11. Diagnostic scripts (`scripts/`)

| Script | Purpose |
|---|---|
| `lsl_inspect.py` | Continuously prints what OpenSignals's LSL stream is actually publishing per channel. Used to diagnose dead channels and the pre-converted-vs-ADC question. |
| `lsl_replay.py <opensignals.txt>` | Publishes a recorded file as a fake `OpenSignals` LSL stream. Lets the full pipeline run end-to-end without the device attached. |
| `plux_direct_inspect.py <MAC>` | Bypasses OpenSignals entirely; talks directly to the PLUX hub over Bluetooth using the official PLUX Python API. |

---

## References

- Moldoveanu A. et al. (2023). Immersive Phobia Therapy through
  Adaptive Virtual Reality and Biofeedback. *Applied Sciences*
  13(18):10365.
- Task Force of the European Society of Cardiology and the North
  American Society of Pacing and Electrophysiology (1996). Heart rate
  variability: standards of measurement, physiological interpretation,
  and clinical use. *European Heart Journal* 17(3):354–381.
- Pan J. and Tompkins W. J. (1985). A real-time QRS detection
  algorithm. *IEEE Transactions on Biomedical Engineering*
  32(3):230–236.
- Lippman N., Stein K. M. and Lerman B. B. (1994). Comparison of
  methods for removal of ectopy in measurement of heart rate
  variability. *American Journal of Physiology* 267(1):H411–H418.
- Lipponen J. A. and Tarvainen M. P. (2019). A robust algorithm for
  heart rate variability time series artefact correction using novel
  beat classification. *Journal of Medical Engineering & Technology*
  43(3):173–181 — the Kubios artifact-correction routine invoked by
  `nk.ecg_peaks(correct_artifacts=True)`.
- Boucsein W. (2012). *Electrodermal Activity*, 2nd edition. Springer.
- Itami S. `vret_server.py` — NeuroKit-grounded reference server
  (NISC Lab, University of Messina, 2026). Our `_compute_hr_rmssd_neurokit`
  and `_gated_rmssd_from_peaks` are byte-equivalent to its formulas.
- PLUX wireless biosignals — `biosignalsnotebooks`:
  <https://github.com/pluxbiosignals/biosignalsnotebooks>
