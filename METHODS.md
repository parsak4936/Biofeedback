# Biofeedback Pipeline — Methods Reference

VRET acrophobia therapy with real-time biofeedback. This document
describes how the raw biosignals captured by the PLUX biosignalsplux
hub are processed into a single stress index that drives the VR
scenario in Unity 3D.

The pipeline follows the procedures illustrated by the official PLUX
tutorial library `biosignalsnotebooks`
(<https://github.com/pluxbiosignals/biosignalsnotebooks>) for all
raw-signal feature extraction. Stress-index computation follows the
methodology of Moldoveanu et al. (2023).

---

## 1. Signal acquisition

### 1.1 Hardware
- Device: PLUX biosignalsplux hub, 16-bit ADC, reference voltage 3.0 V.
- Sensors used: 1× ECG, 1× EDA.
- Transport: OpenSignals software publishing a Lab Streaming Layer (LSL)
  stream named `OpenSignals` containing the raw ADC samples.

### 1.2 Sampling rates
- Raw sensor sampling: **200 Hz** (PLUX default).
- Internal processing rate: **10 Hz** (HR / HRV / EDA derivation, stress
  classification).
- VR scenario update rate to Unity: **1 Hz** (one balloon
  increase/decrease command per second at most).

### 1.3 ADC-to-physical-unit conversion (biosignalsnotebooks)
From `biosignalsnotebooks.conversion.raw_to_phy` for
`device='biosignalsplux'`:

| Sensor | Constants | Formula |
|---|---|---|
| EDA | vcc = 3.0, offset = 0, gain = 0.12 | `EDA_uS = (raw * 3.0 / 65536 - 3.0 * 0) / 0.12` |
| ECG | vcc = 3.0, offset = 0.5, gain = 1.019 | `ECG_mV = (raw * 3.0 / 65536 - 3.0 * 0.5) / 1.019` |

The same general transfer function applies to all PLUX sensors:
```
physical = (raw * vcc / 2^resolution - vcc * offset) / gain
```
The constants above are the biosignalsplux-specific values. Different
PLUX devices (BITalino_rev / BITalino_riot) use slightly different
constants — `gain = 0.132` for EDA and `gain = 1.1` for ECG — and would
produce values approximately 10 % and 8 % lower respectively if used by
mistake on a biosignalsplux recording.

### 1.4 Channel mapping
The OpenSignals LSL stream typically publishes either a 2- or
4-channel layout (`[CH1, CH2]` or `[nSeq, DI, CH1, CH2]`). At startup
the data source resolves which channel carries EDA and which carries
ECG using two complementary methods:

1. **Label-based**: read the per-channel `label` metadata in the LSL
   stream and pick the channel whose label contains "EDA" or "ECG".
2. **Content-based fingerprinting**: pull a 5-second window of raw
   samples; for each candidate channel, run the Pan-Tompkins R-peak
   detector and measure the fraction of resulting RR intervals that
   fall in a physiologically plausible band (300–1500 ms ≈
   40–200 BPM). The channel with the highest score is ECG; the
   remaining non-digital, non-monotonic channel with µS values in
   0.1–30 is EDA.

The resolved mapping is printed in a banner at session start, and the
session is aborted with a clear error if the chosen EDA channel
produces a mean below 0.1 µS over the first three seconds (interpreted
as either electrode contact loss or a mis-mapped channel).

---

## 2. ECG → HR and HRV (RMSSD)

The chain below mirrors `biosignalsnotebooks` exactly. Every function
referenced is exported by the library.

### 2.1 R-peak detection
`biosignalsnotebooks.detect.detect_r_peaks(ecg_signal, sample_rate)`
implements the Pan-Tompkins algorithm (Selvaraj implementation), with
internal stages:

1. Bandpass filter 5–15 Hz (isolates the QRS frequency band).
2. First-order differentiation (emphasises QRS slope).
3. Sample-by-sample squaring (positive-only, exaggerates QRS energy).
4. 80 ms moving-window integration.
5. Adaptive double-threshold (`spk1` running signal-peak estimate,
   `npk1` running noise-peak estimate, threshold computed from both).

Returns `(peak_indices, peak_amplitudes)`. Only `peak_indices` is used
downstream.

### 2.2 Tachogram (RR intervals)
`biosignalsnotebooks.detect.tachogram(peaks, sample_rate)` converts the
peak-index array into the RR-interval series:

```
rr_seconds = diff(peaks / sample_rate)
rr_time    = (peaks / sample_rate)[1:]
```

### 2.3 Ectopic-beat removal
`biosignalsnotebooks.extract.remove_ectopy(rr, t)` cleans the RR series
using a 20 % adjacent-interval rule (Lippman, Stein, Lerman):

```
for each beat i = 1, 2, …:
    max_thresh = RR[i-1] * 1.20
    min_thresh = RR[i-1] * 0.80
    if RR[i] > max_thresh or RR[i] < min_thresh:
        remove RR[i] and RR[i+1] both
        (eliminates the suspect beat plus its propagated next interval)
```

### 2.4 HR computation
Heart rate is derived as the mean inverse of the cleaned RR series:
```
HR_BPM = 60.0 / mean(cleaned_RR_seconds)
```
Over a 30-second trailing ECG window.

### 2.5 RMSSD computation
Root mean square of successive differences (Task Force of ESC / NASPE,
1996), computed manually from the cleaned RR series since
`biosignalsnotebooks` does not expose it directly — the library's
`extract.hrv_parameters` returns SDNN, SD1, SD2, NN20, pNN20, NN50,
pNN50, and frequency-domain power bands (ULF, VLF, LF, HF, LF/HF,
Total) but RMSSD is not in that set.

```
diffs = diff(cleaned_RR_milliseconds)
RMSSD = sqrt(mean(diffs ** 2))
```

Over a 60-second trailing ECG window. The 60-second figure is the
standard short-term HRV recommendation in the Task Force 1996
guidelines (Eur Heart J 17(3):354-381): shorter windows produce
estimators with standard deviation larger than the resting RMSSD itself
(approximately 56 ms at 10 s, dropping to approximately 12 ms at 60 s).

If only SDNN is desired in place of RMSSD, the formula is
`SDNN = std(cleaned_RR_milliseconds)` and the library exposes it
directly via `extract.hrv_parameters(...)['SDNN']`.

### 2.6 Update cadence
HR and RMSSD are recomputed every 0.5 seconds on a rolling ECG buffer
and held with zero-order hold between recomputes. HR comes online
within approximately 2–3 seconds of session start (just needs two
R-peaks). RMSSD is published as NaN until 60 seconds of ECG have
accumulated; downstream code substitutes the baseline mean during this
warm-up so the stress-index weights remain fixed.

---

## 3. EDA

EDA is published in microsiemens (µS) using the conversion in §1.3.
Two processing modes are supported.

### 3.1 Raw EDA (default)
The instantaneous µS reading is used directly. No phasic / tonic
decomposition. The stress-index normalisation in §5 baselines this
value against the resting mean, so slow tonic drift is partially
absorbed by the baseline subtraction.

### 3.2 Lowpass-subtract phasic decomposition (optional)
The biosignalsnotebooks EDA tutorial recommends a simple
phasic / tonic separation:

```
tonic_SCL  = lowpass(raw_EDA, f_c = 0.05 Hz, order = 2,
                     fs = sampling_rate, filtfilt = True)
phasic_SCR = raw_EDA - tonic_SCL
```

Implemented via `biosignalsnotebooks.process.lowpass`. The 0.05 Hz
cut-off is the standard SCL/SCR separation in the biosignals
literature (Boucsein 2012, *Electrodermal Activity*, Springer):
tonic drift varies on a timescale of tens of seconds and below
0.05 Hz, while phasic skin-conductance responses (SCRs) last
1–5 seconds and lie in the 0.05–0.5 Hz band.

---

## 4. Baseline calibration

### 4.1 Duration
The participant sits at rest, no VR headset, for a fixed window of
**120 seconds**.

### 4.2 Per-signal averages
During the baseline window, every accepted sample of EDA, HR, and
RMSSD is appended to a per-signal buffer. At the 120 s mark:

```
baseline_EDA  = mean(EDA_samples_during_baseline)
baseline_HR   = mean(HR_samples_during_baseline)
baseline_HRV  = mean(RMSSD_samples_during_baseline)
```

Samples whose values are NaN (HR/HRV warm-up before the detector
stabilises) are excluded.

### 4.3 Per-signal sigmas
Standard deviation of each cleaned baseline buffer is also stored.
These are used as denominators when z-scoring live deltas in §5 and
to derive the personal stress thresholds in §6.

```
sigma_EDA  = std(baseline_EDA_samples)
sigma_HR   = std(baseline_HR_samples)
sigma_HRV  = std(baseline_HRV_samples)
```

---

## 5. Stress index

### 5.1 Per-signal normalised deltas
For each live sample of EDA, HR, RMSSD, compute the percent deviation
from the personal baseline mean. Signs are oriented so that **higher
delta = more stress** for all three signals:

```
norm_EDA  = (EDA_live - baseline_EDA)   / baseline_EDA * 100
norm_HR   = (HR_live  - baseline_HR)    / baseline_HR  * 100
norm_HRV  = (baseline_HRV - HRV_live)   / baseline_HRV * 100   [inverted]
```

The HRV inversion reflects the physiology: parasympathetic withdrawal
under stress shortens vagally-mediated beat-to-beat variability, so
RMSSD *decreases* under stress and the sign is flipped so the term
contributes positively.

### 5.2 Weighted composite (instantaneous)
Per Moldoveanu et al. (2023), *Immersive Phobia Therapy through
Adaptive Virtual Reality and Biofeedback*, Applied Sciences 13(18):

```
S_instant = 0.5 * norm_EDA + 0.3 * norm_HRV + 0.2 * norm_HR
```

Weights reflect the published specificity of each signal to
sympathetic arousal. EDA dominates because eccrine sweat-gland
activity has no parasympathetic counter-signal; HRV (parasympathetic
proxy) and HR (mixed sympathetic / parasympathetic) carry lower
weights.

### 5.3 Smoothed stress index
A 1-second rolling mean over the instantaneous values:

```
S_t = mean of S_instant over the last second
    (window = 1 * processing_rate samples)
```

At a 10 Hz processing rate this is a 10-sample rolling mean. The
smoothing removes per-sample jitter without introducing meaningful
delay (1 s).

---

## 6. Thresholds and state classification

### 6.1 Personal thresholds
Derived from the standard deviation of the smoothed stress index
during baseline:

```
sigma_S = std(S_t over the baseline window)
MILD = 1.33 * sigma_S        (90th percentile of a normal distribution)
HIGH = 2.28 * sigma_S        (99th percentile of a normal distribution)
```

Both thresholds are absolute (anchored at zero) — this assumes the
stress index sits near zero at rest, which is the case after baseline
subtraction in §5.1.

The multipliers `1.33` and `2.28` are the z-scores corresponding to
the 90th and 99th percentiles of a standard normal distribution (more
precisely, 1.282 and 2.326). Using them as fixed multipliers of the
personal sigma scales the bands to the participant's own resting
variability — a participant with naturally noisy resting physiology
gets wider bands.

### 6.2 State classification
At each processing tick:

```
if S_t > HIGH:        state = ULTRA_STRESSED
elif S_t > MILD:      state = STRESSED
else:                 state = CALM
```

### 6.3 Stress-correlation metric
For post-session analysis, the Pearson correlation between `S_t` and
the balloon altitude over the live window is reported:

```
r = Pearson(S_t, balloon_height) over all live samples
```

A positive `r` indicates the stress index successfully tracks the
altitude exposure.

---

## 7. Pipeline-rate summary

| Stage | Rate | Notes |
|---|---|---|
| Raw ECG / EDA sampling | 200 Hz | PLUX biosignalsplux default |
| R-peak detection recompute | every 0.5 s | held between recomputes |
| HR window | 30 s trailing | mean of cleaned RR |
| RMSSD window | 60 s trailing | NaN before window fills |
| EDA phasic decomposition (when used) | every 0.5 s | held between recomputes |
| Internal pipeline (state machine, fusion, LSL) | 10 Hz | one sample / 100 ms |
| Stress smoothing window | 1 s | 10-sample rolling mean |
| Disk write (samples CSV) | 1 Hz | clinical record |
| Unity VR command rate | 1 Hz (max) | balloon increase / decrease |

---

## 8. Output files

Per session, in `data/<participant>_Session<n>_<date>_<gender>/`:

- `samples.csv` — per-second clinical record of EDA, HR, HRV, deltas,
  S_instant, S_t, state, dashboard score, balloon height.
- `diagnostic.csv` — per-tick forensic record of raw EDA / HR / HRV
  values and acquisition status code.
- `metadata.json` — participant info + frozen baseline values + chosen
  thresholds for the session.
- `console.txt` — full console log from session start to end.
- `unity_udp.csv` — per-command audit of every UDP packet sent to
  Unity.

---

## 9. Optional post-session comparison

Offline scripts in the project root replay any saved OpenSignals
`.txt` file or `samples.csv` and produce:

- `datasource_2.py <session_csv>` — applies the Moldoveanu (2023)
  stress-index math to a saved session and outputs a PNG of stress
  vs. balloon height plus a CSV trace of every intermediate
  computation. Useful for verifying that the live pipeline's output
  matches the reference math.
- `compare_backends.py <opensignals_txt>` — re-runs HR / HRV / EDA
  extraction with both the biosignalsnotebooks and an alternative
  (NeuroKit2) chain on the same raw ECG and reports per-signal
  agreement metrics (Pearson r, RMSE, bias) plus side-by-side plots.

---

## References

- Moldoveanu A. et al. (2023). Immersive Phobia Therapy through
  Adaptive Virtual Reality and Biofeedback. *Applied Sciences*
  13(18), 10365.
- Task Force of the European Society of Cardiology and the North
  American Society of Pacing and Electrophysiology (1996). Heart rate
  variability: standards of measurement, physiological
  interpretation, and clinical use. *European Heart Journal* 17(3),
  354–381.
- Pan, J. and Tompkins, W. J. (1985). A real-time QRS detection
  algorithm. *IEEE Transactions on Biomedical Engineering* 32(3),
  230–236.
- Lippman, N., Stein, K. M. and Lerman, B. B. (1994). Comparison of
  methods for removal of ectopy in measurement of heart rate
  variability. *American Journal of Physiology* 267(1), H411–H418.
- Boucsein, W. (2012). *Electrodermal Activity*, 2nd edition.
  Springer.
- PLUX wireless biosignals — `biosignalsnotebooks`:
  <https://github.com/pluxbiosignals/biosignalsnotebooks>
