# Tuning guide — per-participant calibration

Everything tunable lives in `src/config.py`. Edit, save, re-run.
Nothing else needs to change for any of the values listed here.

This document is read-only reference — the values currently in the
file are the ones validated against Shayan's `vret_server.py` and the
professor's recipe. Move them deliberately, one at a time, and watch
how the stress index responds.

---

## 1. What the OpenSignals UI does NOT control

You already noticed there are no per-participant knobs in OpenSignals.
That is correct. OpenSignals exposes only:

- Sampling rate (10 / 20 / 50 / 100 / 200 / ... 4000 Hz)
- Resolution (12 or 16 bit)
- Per-channel sensor type (EDA / ECG / EMG / ...)
- LSL Integration on / off
- Record start / stop

Sensor *range* is a hardware property of the biosignalsplux EDA
sensor — saturates at **25 µS** — and cannot be moved.

All per-participant calibration happens in our Python config below.

---

## 2. Fusion weights (Moldoveanu 2023)

```python
WEIGHT_EDA = 0.5
WEIGHT_HRV = 0.3
WEIGHT_HR  = 0.2
```

These are the per-signal contributions to the composite stress index:

```
S_instant = WEIGHT_EDA · z_EDA + WEIGHT_HRV · z_HRV + WEIGHT_HR · z_HR
```

When to change them:

| Situation | Suggested tweak |
|---|---|
| Participant has very stable HR but expressive EDA | Bump EDA to 0.6, drop HR to 0.1 |
| Participant has dry skin and weak EDA response | Drop EDA to 0.3, raise HRV to 0.4 and HR to 0.3 |
| Participant on beta-blockers (HR can't rise) | Drop HR to 0.0 (renormalise: 0.6 / 0.4 / 0.0) |
| Strict Moldoveanu replication | Keep 0.5 / 0.3 / 0.2 |

**Rule**: weights must sum to 1.0. If you change one, redistribute the
others so they still total 1.0, otherwise the threshold formula needs
re-derivation.

---

## 3. Threshold multipliers

```python
THRESH_MILD_K = 1.28      # z-score for 90th percentile
THRESH_HIGH_K = 2.33      # z-score for 99th percentile
```

These are the z-multipliers applied to `sigma_baseline` to set the
MILD and HIGH stress thresholds. The defaults flag "the top 10 %
deviation" as stressed and "the top 1 %" as ultra-stressed.

| Situation | Suggested tweak |
|---|---|
| Score reaches ULTRA too easily on calm tasks | Raise both: MILD=1.50, HIGH=2.58 (tail percentiles 93 %, 99.5 %) |
| Score never crosses MILD even during clear stress | Lower MILD to 1.04 (top 15 %) |
| Sensitive participant (anxious baseline) | Raise both — gives them more headroom before alarm |
| Stoic participant | Lower both — exposes subtler arousal |

The professor's reference `DataAnalyze.py` uses **1.33 / 2.28**. Close
enough that either is defensible; we kept 1.28 / 2.33 because those
are the exact z-scores from the standard-normal table.

---

## 4. Sigma floors (anti-false-alarm guards)

```python
HR_SIGMA_FLOOR_PCT     = 2.0      # %  resting HR spread floor
HRV_SIGMA_FLOOR_PCT    = 5.0      # %  resting RMSSD spread floor
EDA_PHASIC_SIGMA_FLOOR = 0.02     # µS phasic-EDA spread floor
SIGMA_FLOOR            = 1e-6     # absolute divide-by-zero guard
```

When a baseline is unusually flat, sigma collapses near zero and a
trivial live wobble becomes a huge z-score (a 1 % HR deviation reads
as "ultra-stressed"). Each sigma is clamped UPWARD to the
physiological floor below.

| Situation | Suggested tweak |
|---|---|
| Score still false-alarms on tiny movements | Raise the relevant floor (e.g. HR floor 2.0 → 3.0 %) |
| Score never reacts to mild arousal | Lower the relevant floor (e.g. HR floor 2.0 → 1.0 %) |
| Athletes / very HRV-rich participants | Raise HRV floor to 8–10 % |
| Very nervous baseline (high EDA fluctuation) | Raise EDA floor to 0.05 µS |

A warning prints to the console when a floor engaged. If that happens
every session for the same participant, consider raising the relevant
floor permanently for that person.

---

## 5. Baseline + window lengths

```python
BASELINE_SEC      = 120          # personal calibration window
HR_WINDOW_SEC     = 30           # trailing HR estimation
RMSSD_WINDOW_SEC  = 60           # trailing RMSSD estimation
HR_COMPUTE_INTERVAL_SEC = 0.5    # how often to recompute HR/RMSSD
```

| Situation | Suggested tweak |
|---|---|
| Participant cannot sit still for 2 minutes | Drop `BASELINE_SEC` to 90 (RMSSD will be unreliable) |
| Score reacts too slowly | Drop `HR_WINDOW_SEC` to 15 (HR responds faster, noisier) |
| Score reacts too fast / jumpy | Raise `RMSSD_WINDOW_SEC` to 90 (RMSSD smoother, more lag) |

**Constraint**: `RMSSD_WINDOW_SEC` ≥ 30 s is the practical minimum from
Shaffer & Ginsberg 2017 / Munoz 2015. Below that, RMSSD estimator
noise overwhelms physiological signal.

---

## 6. EDA phasic decomposition (NeuroKit2 backend only)

```python
EDA_PHASIC_WINDOW_SEC        = 60     # rolling window into nk.eda_phasic
EDA_DECOMP_RATE_HZ           = 10     # downsample for decomposition
EDA_PHASIC_UPDATE_INTERVAL_SEC = 0.5
EDA_PHASIC_MAX_US            = 1.0    # plausibility ceiling (µS)
```

The cvxEDA decomposition is invoked once every 0.5 s on the last 60 s
of raw EDA, returning the most recent phasic value. Values above
`EDA_PHASIC_MAX_US` are filter-edge ringing artifacts and get
rejected.

| Situation | Suggested tweak |
|---|---|
| Real SCRs are being rejected as artifacts | Raise `EDA_PHASIC_MAX_US` to 1.5 (rare — most SCRs are < 0.5 µS) |
| Phasic chart looks noisy | Raise `EDA_PHASIC_WINDOW_SEC` to 90 (smoother, slower) |

---

## 7. RR-interval artifact gate (Malik 20 %)

```python
RR_MIN_MS              = 300
RR_MAX_MS              = 1500
RR_MAX_RELATIVE_CHANGE = 0.20    # 20 % Malik rule
```

Applied AFTER Kubios artifact correction inside the NeuroKit2 backend.
Reject any RR interval whose change from the previous one exceeds
20 % (a missed or ectopic beat).

| Situation | Suggested tweak |
|---|---|
| Athletes with very pronounced HRV (sinus arrhythmia) | Raise to 0.25 — their natural RR change exceeds 20 % |
| Noisy ECG, many spurious peaks | Tighten to 0.15 |

biosignalsnotebooks backend does NOT use this gate — its own
`remove_ectopy` (also a 20 % rule, applied differently) is the
equivalent.

---

## 8. Backend selection

```python
HR_HRV_BACKEND  = 'neurokit'   # 'neurokit' | 'bsnb'
EDA_BACKEND     = 'neurokit'   # 'neurokit' | 'bsnb'
EDA_BSNB_METHOD = 'raw'        # 'raw' | 'lowpass_subtract'
```

Switching backends is the quickest A/B you can run on the same
participant. Neither changes the fusion math; only HR / RMSSD / EDA
extraction differs. Full comparison in [METHODS.md](METHODS.md).

---

## 9. Chart Y-axis ranges (display only — no math impact)

```python
EDA_PLOT_DEFAULT_RANGE = (0.0, 25.0)       # full PLUX sensor span
HR_PLOT_DEFAULT_RANGE  = (40.0, 180.0)
HRV_PLOT_DEFAULT_RANGE = (0.0, 200.0)
HEIGHT_PLOT_DEFAULT_RANGE = (0.0, 150.0)
```

EDA is now pinned at full sensor span (0–25 µS) and no longer
auto-narrows — saturation is visible at the top of the chart.

The stress index chart locks its Y range when thresholds are computed
at baseline lock:

```
y_min = -3 × (HIGH - MILD)           # generous negative headroom
y_max = HIGH + 3 × (HIGH - MILD)     # generous positive headroom
```

So both a very-calm participant (deep negative S_t) and a very-
stressed participant (well above HIGH) stay visible on the chart.

---

## 10. Per-person calibration workflow

Suggested loop for each new participant:

1. Run with defaults. Observe the first 30 s of LIVE phase.
2. If the dashboard's `Score 0–100` is pinned at 100 in the first
   minute → sigma floors too tight. Raise the relevant floor.
3. If `Score` is always 0 even during obvious arousal → sigma floors
   too loose, OR weights wrong for this person. Inspect which signal
   moves visibly and shift weight toward it.
4. If thresholds look wrong (every breath crosses MILD) → raise
   `THRESH_MILD_K` to 1.50.
5. Save the new config alongside the patient record so next session
   uses the same tuning.

If you want per-participant configs without manually swapping, the
cleanest path is to add a `Config.PATIENT_PROFILE` flag and load
per-profile overrides from `data/<patient>/calibration.json`. Not
implemented yet; ask if you want it.

---

## 11. What I am NOT recommending you touch

These knobs have downstream consequences and should stay where they
are unless a clinician asks:

- `PIPELINE_RATE` (10 Hz) — controls everything else by ratio
- `LSL_VALUES_PRECONVERTED` (True) — discovered 2026-06-19; flipping it
  re-introduces the double-conversion bug
- `UDP_GATE_WARMUP_SEC` (1.5 s) — keeps Unity from receiving fake
  "calm" during the buffer warm-up
- `STREAM_TIMEOUT_SEC` (50 s) — Bluetooth dropout watchdog; lowering
  it aborts on minor hiccups
