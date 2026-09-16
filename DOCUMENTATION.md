# Biofeedback pipeline — documentation

Complete reference for the biofeedback engine. This is the file to open
when you want to understand how the code works, what each Python file
does, what the output files contain, and what happens when things go
wrong. For the paper draft see `METHODOLOGY.md`; for physical operator
setup see `LAB_CHECKLIST.md`; for the day-to-day session workflow see
`HOW_TO_RUN.md`.

Consolidated in September 2026 from six older MD files. Nothing has
been lost, just organised.

Table of contents:

1. Overview
2. Terminology
3. Architecture at a glance
4. Data flow, device to Unity
5. Input side, what OpenSignals sends
6. Output side, files and network streams
7. Per-participant tuning
8. Error handling and failure modes
9. File-by-file guide
10. Where to find more

---

## 1. Overview

The system is a Python biofeedback server that reads physiological
signals from a PLUX biosignalsplux hub, computes a personalised stress
index, and drives a VR exposure scene running in Unity. It supports
multiple phobia scenarios through a scenario-agnostic Unity contract:
whatever scene Unity is running, its own telemetry fields land in the
recording automatically, no biofeedback code change required.

Both raw-signal extraction backends live in the code. NeuroKit2 is the
default and matches the reference implementation from the lab's earlier
work. biosignalsnotebooks is the alternative and mirrors what the
PLUX-official package would produce. Both feed the same downstream
math, so switching backends is an A/B validation tool, not a decision
point during a session.

Mock mode is preserved. The `MockDataSource` class replays a recorded
OpenSignals `.txt` file at its native rate, publishing on the same LSL
stream a real device would use. The entire pipeline runs against mock
data with no other code change, which is what makes offline analysis
and regression testing possible.

---

## 2. Terminology

Plain-language glossary. Grouped roughly in the order a signal travels
through the system: body and sensors first, then the numbers computed
from them, then the plumbing.

### 2.1 The physiology and the sensors

**EDA (electrodermal activity).** Also called skin conductance.
Measures how easily a small electrical current passes across the
skin, which changes with sweating. Sweat glands are controlled only
by the sympathetic ("fight or flight") branch of the nervous
system, so EDA is a clean, direct readout of arousal. Measured in
microsiemens. When someone gets stressed, EDA goes up. Read from
two electrodes on the fingertips.

**ECG (electrocardiogram).** The electrical activity of the heart,
measured in millivolts from chest / collarbone electrodes. Looks
like a repeating wave with a sharp tall spike on each heartbeat.
The ECG voltage is not used directly for stress; it is used to
find the heartbeats, which give heart rate and heart-rate
variability.

**Sympathetic and parasympathetic nervous system.** Two opposing
branches of the autonomic nervous system. Sympathetic is the
accelerator (stress, arousal, fight-or-flight); parasympathetic is
the brake (rest, recovery). Stress is sympathetic going up and
parasympathetic going down. EDA tracks the accelerator; HRV tracks
the brake. Using both gives a fuller picture than either alone.

### 2.2 From ECG to heart numbers

**R-peak.** The tall sharp spike in the ECG that happens once per
heartbeat. In medical terms it is the R wave of the QRS complex.
"Finding the R-peaks" means locating each heartbeat in the voltage
trace.

**RR interval.** The time between two consecutive R-peaks in
milliseconds. An 800 ms interval means the heart beats once every
800 ms.

**HR (heart rate).** Beats per minute, computed from the RR interval
as 60000 divided by the RR interval in milliseconds. Because it is
computed per beat, HR is naturally a stair-step signal: it gets a
new value each heartbeat and holds steady in between. Not a glitch.

**HRV (heart-rate variability), measured as RMSSD.** Heartbeats are
not perfectly evenly spaced; the tiny variations in the gaps carry
information. RMSSD (root mean square of successive differences) is
one standard way to quantify that. Take the differences between
consecutive RR intervals, square them, average, and square-root.
A metronome-like heart gives a low RMSSD; a relaxed heart that
varies beat-to-beat gives higher RMSSD. Stress lowers HRV. Computed
over a rolling `Config.RMSSD_WINDOW_SEC` window (default 60 s), in
milliseconds. Typical resting adults sit in the 20-80 ms range.

The window length is a clinical trade-off. Task Force 1996
guidance says 5 minutes (300 s) for stable short-term HRV, which
is too laggy for real-time biofeedback. Ultra-short-term HRV
literature (Shaffer & Ginsberg 2017, Munoz 2015) treats 60 s as
the practical minimum. Anything shorter and the estimator noise
overwhelms the physiology.

Why RMSSD and not the more famous LF/HF ratio? RMSSD reflects the
parasympathetic brake specifically, stabilises within about 10
seconds, and does not assume signal stationarity. The LF/HF ratio
needs 20-30 seconds and assumes stationarity, which is false during
VR exposure; the modern literature considers it an unreliable
stress measure.

### 2.3 Signal cleaning

**ADC (analog-to-digital converter).** The chip in the PLUX hub
that turns a sensor's voltage into a number. PLUX uses a 16-bit
ADC, so each reading is an integer from 0 to 65535. Those integers
are raw ADC counts and mean nothing physical until converted with
the sensor's transfer formula.

**Bandpass filter.** A filter that keeps only a chosen band of
frequencies. For R-peak detection the ECG is bandpassed to
5-15 Hz, where the sharp QRS energy lives. This throws away slow
baseline drift and high-frequency noise, leaving the heartbeats
easy to find.

**Zero-order hold.** When no new sample has arrived this instant,
reuse the last one. HR updates once per heartbeat but the loop
runs many times per second, so most of the time there is no new
heart value and the previous one is held. The diagnostic log marks
these ticks as `HOLD_LAST`.

**Peak prominence.** How much a peak stands out from the dips on
either side of it, as opposed to its absolute height. Prominence
is robust to a drifting baseline: a peak that rises 0.3 mV above
its surroundings has a prominence of 0.3 mV whether the baseline
is at zero or has wandered up.

**Refractory period.** A minimum enforced gap between detected
peaks. After a real heartbeat there is a smaller bump (the T-wave)
a few hundred milliseconds later; without a refractory window the
detector might count it as a second beat. The pipeline requires at
least 300 ms between peaks, which caps the maximum detectable rate
at 200 BPM.

**3-sigma cleaning.** During baseline, any sample more than three
standard deviations from the mean is dropped as an artifact
(a motion spike or electrode glitch) rather than treated as real
physiology. Runs once, at the end of the baseline.

### 2.4 The baseline

**Baseline (personal baseline).** The first 120 seconds of every
session, during which the participant sits still and data is just
collected. From it the pipeline computes that specific person's
resting average for EDA, HR, and HRV. Everything afterward is
measured relative to these personal numbers, not to population
averages, because "high stress" for a calm person and an anxious
person look completely different in absolute terms. The baseline is
recomputed fresh every session.

**sigma_baseline (the noise floor).** Even at rest, the stress
index does not sit perfectly still; it jitters a little.
sigma_baseline is how much it jitters at rest, computed by running
the resting data through the full stress calculation and taking
the standard deviation of the result. It is the reference for
deciding what counts as a real stress response versus normal noise.
Frozen at the end of the baseline and never changes during the
session: if it adapted, a big stress response would raise the bar
and hide itself.

### 2.5 The stress numbers

**Percentage deviation.** For each signal, how far the current
reading is from the personal baseline, as a percent. HR and EDA
deviations are positive when above baseline. HRV is inverted
(baseline minus current) so that "more stressed" is positive for
all three, since stress lowers HRV.

**delta_EDA.** The phasic EDA value in microsiemens (see 2.6).
Recorded in samples.csv as `delta_eda`, LSL channel 6.

**delta_HR.** The percentage deviation for heart rate,
`(HR_now - avg_HR) / avg_HR * 100`. Positive when the heart is
beating faster than at rest. Smallest contributor to S_t (weight
0.2) because heart rate moves for many non-stress reasons.

**delta_HRV.** The percentage deviation for HRV, inverted:
`(avg_HRV - HRV_now) / avg_HRV * 100`. Positive when HRV has
dropped below baseline (which means more stress, because lower
HRV signals parasympathetic withdrawal). Middle contributor to S_t
(weight 0.3).

**S_instant.** Weighted combination of the three z-scored deltas
per tick: `0.5 * z_EDA + 0.3 * z_HRV + 0.2 * z_HR`. Noisy at the
per-tick level.

**S_t (the stress index).** S_instant smoothed over a rolling
window (default 3 seconds, `Config.S_T_SMOOTH_SEC`). This is the
canonical stress number that everything downstream uses. Zero
means at baseline; positive means more aroused. Small negatives
are possible (slightly calmer than baseline) and harmless.

**Thresholds (mild and high).** Two cutoffs centred on the resting
S_t mean, not on zero. Mild is `mean_baseline + 1.28 * sigma_baseline`
(true 90th-percentile z), high is `mean_baseline + 2.33 * sigma_baseline`
(99th-percentile z). Centring on `mean_baseline` matters because
resting S_t is generally not zero; measuring from zero would flag
a calm participant whose resting drift is slightly positive. Both
frozen at the 120 s lock and never adapt during the session.

**State (calm, stressed, ultra_stressed).** S_t bucketed by the
thresholds. Calm is at or below mild. Stressed sits between the
thresholds and is actually the therapeutic target zone. Ultra
stressed is above high, the signal to back off. The state goes to
Unity; Unity decides what to do with it.

**Dashboard score (0-100).** Cosmetic remap of S_t for quick
reading by the operator: 0 at baseline, 50 at the mild boundary,
100 at ultra. Display-only.

### 2.6 EDA decomposition

**Tonic and phasic EDA.** Raw skin conductance is the sum of two
components. Tonic drifts over minutes and depends on electrode
hydration, temperature, and general arousal baseline. Phasic
consists of faster, seconds-scale bumps corresponding to individual
sympathetic events. Only phasic feeds the stress index; tonic is
discarded.

**cvxEDA.** The convex-optimisation algorithm by Greco et al. 2016
that decomposes raw EDA into tonic + phasic. Runs inside
NeuroKit2's `nk.eda_phasic`. The pipeline invokes it every 0.5 s
on the most recent 60 s of raw EDA, resampled to 10 Hz.

**Phasic ceiling.** Real skin conductance responses rarely exceed
1 microsiemens. Anything larger is filter-edge ringing from the
resampler at the window edge and gets rejected.

### 2.7 The plumbing

**LSL (Lab Streaming Layer).** A small open-source networking
protocol for streaming time-stamped signal data between programs
on a network. OpenSignals publishes raw signals over LSL; the
biofeedback pipeline publishes its results over LSL for the
dashboard and audit log. Handles timestamps and ordering.

**UDP bridge.** A second output channel, dedicated to Unity.
Plain UDP socket sending one of four short text strings:
`start`, `stop`, `increase`, `decrease`. Unity listens on port
5005 by default.

**Telemetry receiver.** JSON receiver on UDP port 5006. Every VR
scene sends `{"scenario": "<name>", "data": {...}}` per tick;
the receiver parses the packet, exposes the current snapshot to
the pipeline, and forwards it on a dedicated LSL string stream
for the dashboard scene panel.

**Command throttle.** Minimum gap between two consecutive
`increase` or `decrease` packets sent to Unity. Default 1 second.
Prevents flooding.

**Wait-for-calm gate.** After the live phase starts, the UDP
bridge holds every `increase` and `decrease` command until the
system observes a genuine calm state. Two reasons: the patient
may still be adjusting when the session begins, and the fusion
engine returns a synthetic "calm" during its buffer-warmup that
should not count as a real reading.

**SessionState.** The five possible states the pipeline can be
in: IDLE, BASELINE, BASELINE_DONE, LIVE, STOPPED. The state is
driven by the operator via dashboard buttons, not computed by
the pipeline. Exposed on LSL channel 20.

**Pipeline rate.** The core loop runs at `Config.PIPELINE_RATE`
(default 10 Hz). The device may sample faster (200 or 1000 Hz),
but the pipeline downsamples to 10 Hz, which is plenty for
physiological signals that change over seconds.

### 2.8 One-liners if you only need the essence

EDA is skin sweat (stress up). ECG is the heart's electrical trace,
used to find heartbeats. An R-peak is one heartbeat. HR is beats
per minute; HRV (RMSSD) is the beat-to-beat variation, which drops
under stress. The baseline is the person's first two resting
minutes. S_t is the single stress number built from how far the
signals have moved from that baseline. Thresholds turn S_t into
calm/stressed/ultra. That state goes to Unity, which decides how to
change the scene. LSL is how the programs talk to each other; UDP
is how commands go to Unity.

---

## 3. Architecture at a glance

Server-and-clients model. The biofeedback engine is a server running
on the lab workstation; Unity scenes are clients. Adding a phobia
scene is a Unity-side task, no server code change needed.

```
   Participant  ---  ECG, EDA  --->  PLUX biosignalsplux
                                            |
                                            | Bluetooth
                                            v
                                       OpenSignals
                                            |
                                            | Lab Streaming Layer
                                            v
   Operator Dashboard <--- LSL ----   Biofeedback Server   ---> Session files (CSV, JSON)
                                        ^          |
                                        |          |
                             JSON       |          |    text commands
                             telemetry  |          v
                             (5006)   Unity VR scene (5005)
                                          |
                                          | render
                                          v
                                       Meta Quest HMD
                                          |
                                     back to Participant
```

The Python side does signal processing and stress classification. Unity
owns the scene: altitude for acrophobia, spider behaviour for
arachnophobia, audience for public speaking. Python only sends the
state (`calm` / `stressed` / `ultra_stressed`) and the four command
strings. Unity decides how to translate that into scene changes.

For the detailed architecture description and the mermaid diagrams
suitable for a paper, see `METHODOLOGY.md` Section 4.

---

## 4. Data flow, device to Unity

End-to-end walk of a single sample. Every layer numbered so you can
find where a specific transformation happens.

### Layer 0: the device

A PLUX biosignalsplux hub sits on the patient with two sensors: ECG
(heart electrical activity) and EDA (skin conductance). OpenSignals
talks to it over Bluetooth. The hub delivers voltage; nothing
physiological has been computed yet.

Sampling is 200 Hz. Which sensor is on which channel varies between
recordings; both `parse_opensignals_header()` in
`src/data_sources.py` (mock path) and `resolve_plux_channels()`
(live PLUX path) read the mapping from the metadata rather than
hardcoding it.

### Layer 1: physical-unit conversion

OpenSignals' LSL bridge publishes already-converted values by
default (microsiemens for EDA, millivolts for ECG). The mock-mode
path (recorded `.txt` file) still holds raw ADC integers and needs
the transfer functions:

```
EDA (microsiemens) = (ADC / 65536) * 3.0 / 0.132
ECG (millivolts)   = ((ADC / 65536) - 0.5) * 3.0 / 1100 * 1000
```

The 3.0 is the reference voltage, 0.132 is the EDA sensor constant,
1100 is the ECG amplifier gain. The `- 0.5` recenters ECG because
it swings both ways around zero. `Config.LSL_VALUES_PRECONVERTED`
controls which path applies.

### Layer 2: ECG → HR and HRV

The device does not compute HR or HRV. The pipeline derives them
from ECG by finding R-peaks (the tall spikes, one per heartbeat)
and measuring the time between them.

Both backends run the same NeuroKit2 chain in the default path:

```
nk.ecg_clean
  → nk.ecg_peaks(correct_artifacts=True)   # Kubios inside
  → nk.ecg_rate(peaks, ...)                # HR
  → _gated_rmssd_from_peaks(...)           # RMSSD, Malik-gated
```

HR is the mean over a trailing `HR_WINDOW_SEC` of ECG (default
30 s). RMSSD is over `RMSSD_WINDOW_SEC` (default 60 s). Both
recompute every 0.5 s and hold with zero-order hold between
updates. RMSSD values outside 5-300 ms are rejected as detector
failures.

For mock mode the detection runs once over the whole recording at
load time. For the live PLUX path the same logic runs
incrementally on a rolling ECG buffer (~65 s, covering the RMSSD
window plus margin).

EDA needs no derivation: the converted microsiemens value is used
directly.

### Layer 3: onto the internal network

`MockDataSource` (or the live `RealPLUXDataSource`) publishes three
channels (EDA, HR, HRV) on the LSL stream named in
`Config.STREAM_NAME`. A side stream `OpenSignals_ECG` carries the
raw ECG voltage for the dashboard's waveform chart.

`BiofeedbackAcquisition` (inside `main.py`) consumes the stream at
the pipeline rate of 10 Hz. It drains the inlet to the most recent
sample each tick, so it never falls behind when the device streams
faster. Every incoming sample is validated: NaN or infinite values
are rejected, physiologically impossible values are rejected, and
if a signal goes flat for more than 15 seconds it flags a probable
electrode disconnect.

### Layer 4: phasic EDA decomposition + baseline buffering

`SignalProcessor` no longer smooths anything (removed per PDF).
Two things happen here per tick:

Phasic EDA decomposition. Raw EDA is appended to a rolling
60-second window. Every `EDA_PHASIC_UPDATE_INTERVAL_SEC`
(default 0.5 s), `nk.eda_phasic` runs on the window to extract
the current phasic component. The value is held between recomputes.
A plausibility ceiling of 1 microsiemens rejects filter-ringing
artifacts.

Baseline buffering. For the 120 seconds inside the BASELINE state,
raw EDA / HR / HRV values accumulate in per-signal buffers.
Accumulation is gated by `accumulate_baseline` (True only while
state == BASELINE), so IDLE samples never leak in.

At the 120-second mark, `_compute_personal_baselines()` runs once.
For each signal, samples more than three sigma from the mean are
dropped, then what remains is averaged. Those three averages are
the patient's personal resting baseline.

### Layer 5: sigma and per-signal z-score stats

`calculate_baseline_sigma()` in `src/fusion.py` does two passes:

Pass 1: for each baseline sample, build raw per-signal deltas
(phasic EDA in µS, HR percent, HRV percent inverted). Compute each
signal's own mean and sigma. Apply sigma floors so a flukey
baseline cannot make tiny live wobbles z-score huge.

Pass 2: z-score each delta against its own baseline, weight
(0.5 / 0.3 / 0.2), accumulate the raw `S_inst` series, smooth to
`S_t` with the same rolling mean the live path uses. Only
fully-valid windows contribute (both HR and HRV non-NaN, past
the 60 s warm-up).

Thresholds come out of this:

```
thresh_mild = mean_baseline + 1.28 * sigma_baseline   (90th-pct z)
thresh_high = mean_baseline + 2.33 * sigma_baseline   (99th-pct z)
```

`mean_baseline` uses the smoothed S_t series (matches what the live
classification operates on). `sigma_baseline` uses the raw S_inst
series (smoothing would shrink variance and collapse the bands).

### Layer 6: per-tick fusion

Each live sample turns into a percentage deviation from the
personal baseline. HRV is inverted so all three deltas increase
during stress:

```
delta_EDA = phasic_EDA_now                                     (µS)
delta_HR  = (HR - avg_HR) / avg_HR * 100                       (%)
delta_HRV = (avg_HRV - HRV) / avg_HRV * 100                    (%, inverted)
```

Each delta is z-scored against its own baseline mean and sigma,
then combined with the fixed weights:

```
S_instant = 0.5 * z_EDA + 0.3 * z_HRV + 0.2 * z_HR
```

### Layer 7: smoothing

`S_instant` is smoothed by a rolling mean over
`Config.S_T_SMOOTH_SEC` seconds (default 3 s at 10 Hz = 30 samples)
to produce `S_t`, the canonical stress index.

### Layer 8: classification

`evaluate_state()` classifies S_t. At or below `thresh_mild` is
calm. Between the thresholds is stressed (the therapeutic target
zone). Above `thresh_high` is ultra_stressed.

### Layer 9: dashboard score

Cosmetic remap of S_t to 0-100 for the operator's dashboard: 0 at
baseline, ~50 at the stressed boundary, 100 at ultra. Display-only.

### Layer 10: LSL output

Every tick, `UnityBridge.broadcast_state()` pushes the 25-channel
vector on the LSL stream `Biofeedback_State`. The dashboard
subscribes; any audit tool can too. The channel list is fixed and
lives in `output.py`. See Section 6 below for the full table.

In parallel, `UnityBridge.broadcast_telemetry()` publishes the
current scenario + data payload as JSON on a separate string LSL
stream `Biofeedback_Telemetry`.

### Layer 11: UDP command bridge to Unity

A separate feed runs alongside the LSL output: plain-text commands
over UDP to Unity on port 5005. Four strings, chosen from the
current stress state:

- calm → `increase` (scene escalates)
- stressed → nothing (scene holds — silence is the command)
- ultra_stressed → `decrease` (scene backs off)
- entering LIVE → `start`
- leaving LIVE → `stop`

Two safety mechanisms wrap this. A command throttle prevents
flooding (default: at most one `increase` / `decrease` per second).
A wait-for-calm gate holds all state-driven commands when the LIVE
phase first starts, until the patient hits a genuine calm reading.

Every packet is written to `unity_udp.csv` in the session folder
with timestamp, kind, command, current state, current S_t, and
gate-open flag.

### Layer 12: operator control state machine

The pipeline does not auto-transition. The operator drives
transitions via Start and Stop buttons on the dashboard. The
Biofeedback_Control LSL stream carries commands from dashboard to
main pipeline. States: IDLE, BASELINE, BASELINE_DONE, LIVE,
STOPPED. Transitions trigger side effects (reset counters, rotate
CSV, send `start` / `stop` to Unity, write the final summary).

Complete failure-mode matrix in Section 8. Complete file-by-file
guide in Section 9.

---

## 5. Input side, what OpenSignals sends

Notes on what the PLUX hub actually delivers, what a recorded file
looks like, and how the LSL network stream differs from the file.

### 5.1 Two outputs of OpenSignals

OpenSignals produces the same content in two packages: a `.txt` file
on disk (used by mock mode for replay) and a live LSL network stream
(used by real-device mode). They carry the same sensor values; the
file adds a couple of bookkeeping columns and a metadata header.

### 5.2 The .txt file

First few lines of a real recording:

```
# OpenSignals Text File Format. Version 1
# {"00:07:80:0F:31:9C": {"sampling rate": 200, ...full JSON metadata...}}
# EndOfHeader
0    0    32948    15163
1    0    33060    15198
2    0    32996    15216
```

The first three lines are header. The second is the interesting one:
one line of JSON describing the recording. Fields that matter:

- `sampling rate`: samples per second per channel. Usually 200.
- `resolution`: bit depth of the ADC. 16-bit (integers 0 to 65535).
- `sensor`: order matters. `["ECG", "EDA"]` means channel 1 is
  ECG and channel 2 is EDA. Some older files have this reversed,
  which is why the code parses the header instead of hardcoding.
- `column`: `["nSeq", "DI", "CH1", "CH2"]` for a two-sensor
  recording.
- `convertedValues`: 0 means raw ADC integers (needs downstream
  conversion), 1 means already-converted physical units.

After `EndOfHeader`, one line per sample, tab-separated:

- `nSeq`: monotonic sample counter. Increments by 1 every sample.
  A jump (say 1000 to 1003) means three samples were dropped,
  usually because of a Bluetooth hiccup.
- `DI`: state of a digital input pin on the hub. Not used.
- `CH1`, `CH2`: the raw 16-bit ADC counts for the two sensors.

### 5.3 The LSL stream version

Stripped down compared to the file: no `nSeq`, no `DI`, just the
sensor values, one tuple per sample, at the configured rate.

Stream settings:

- Name: `OpenSignals` by default (configurable).
- Type: usually the device MAC.
- Channel count: matches the number of active sensors.
- Sample rate: matches the file's `sampling rate`.
- Channel format: 32-bit floats.

Important discovery: **the LSL bridge publishes already-converted
physical units, not raw ADC integers**. Documentation is silent on
this and a "constant 25 microsiemens" reading investigated for
several days turned out to be a correctly-converted saturated ADC.
The flag `Config.LSL_VALUES_PRECONVERTED` controls this behaviour
and defaults to True; flip it to False for archived recordings that
carry raw ADC.

### 5.4 Channel auto-detection

Live PLUX: `resolve_plux_channels()` reads channel labels from the
LSL stream's metadata. Maps EDA / ECG by name (case-insensitive
substring). If labels are missing, `Config.REAL_PLUX_EDA_CHANNEL`
and `REAL_PLUX_ECG_CHANNEL` are the fallback. Resolved indices are
printed at startup so the operator can verify.

Mock: `parse_opensignals_header()` reads the file's JSON header and
maps sensors by name. Switching to a recording with a different
channel order requires no edits.

---

## 6. Output side, files and network streams

The system produces four files per session, two live network streams,
and one live UDP feed to Unity.

### 6.1 Session folder layout

One folder per session under `data/`:

```
data/<folder name>/
    ├── metadata.json     intake + frozen baseline + thresholds
    ├── samples.csv       clinical record (dynamic columns per scenario)
    ├── diagnostic.csv    forensic per-tick raw trace
    └── unity_udp.csv     audit log of every UDP packet sent to Unity
```

Current folder-name format is patient-first, will change to
ID-first once the GDPR name-removal refactor lands (see
`FUTURE_CHANGES.md` Section 1.1).

### 6.2 samples.csv columns

Fixed columns first, then dynamic per-scenario columns learned from
the first telemetry packet Unity sends, then fixed columns again.

| Column | Unit | Meaning |
|---|---|---|
| `sample_n` | integer | 1-indexed row counter |
| `patient_first_name` | text | From intake |
| `patient_last_name` | text | From intake |
| `patient_id` | text | From intake |
| `gender` | text | F or M |
| `session_date` | YYYY-MM-DD | From intake |
| `session_number` | integer | From intake |
| `phase` | text | BASELINE or LIVE |
| `state` | text | baseline / calm / stressed / ultra_stressed |
| `dashboard_score` | 0-100 | Operator display value |
| `eda` | µS | Raw skin conductance |
| `hr` | BPM | Heart rate |
| `hrv` | ms | RMSSD |
| `delta_eda` | µS | Phasic EDA |
| `delta_hr` | percent | HR deviation from baseline |
| `delta_hrv` | percent | HRV deviation, inverted |
| `s_instant` | z-weighted | Raw per-tick stress |
| `s_t` | z-weighted | Smoothed rolling mean |
| `<dynamic 1>` | scenario-defined | e.g. `height` for acrophobia |
| `<dynamic 2>` | scenario-defined | e.g. `size` for arachnophobia |
| `<dynamic N>` | ... | any additional fields Unity sends |
| `artifacts_eda` | count | 3-sigma rejects during baseline |
| `artifacts_hr` | count | Same for HR |
| `artifacts_hrv` | count | Same for HRV |

During baseline the stress-related columns are zero by design;
fusion math does not run until baseline locks. Artifact counts stay
zero until the baseline finishes, then hold their final value.

### 6.3 Live Biofeedback_State LSL stream (25 channels)

Fixed layout, one 32-bit float per channel:

| # | Channel | Unit | Meaning |
|---|---|---|---|
| 0 | `s_t` | z-weighted | Smoothed stress index |
| 1 | `state_enum` | 0/1/2 | 0=calm, 1=stressed, 2=ultra_stressed |
| 2 | `dashboard_score` | 0-100 | Operator display |
| 3 | `eda` | µS | Skin conductance |
| 4 | `hr` | BPM | Heart rate |
| 5 | `hrv` | ms | RMSSD |
| 6 | `delta_eda` | µS | Phasic EDA |
| 7 | `delta_hr` | percent | HR deviation |
| 8 | `delta_hrv` | percent | HRV deviation (inverted) |
| 9 | `avg_eda` | µS | Personal baseline EDA |
| 10 | `avg_hr` | BPM | Personal baseline HR |
| 11 | `avg_hrv` | ms | Personal baseline HRV |
| 12 | `thresh_mild` | z-weighted | Locked mild threshold |
| 13 | `thresh_high` | z-weighted | Locked high threshold |
| 14 | `baseline_status` | 0/1 | 0 during baseline, 1 after lock |
| 15 | `elapsed_baseline_sec` | seconds | Time in BASELINE |
| 16 | `qa_invalid_count` | count | NaN or infinite samples rejected |
| 17 | `qa_out_of_range_count` | count | Samples outside physiological bounds |
| 18 | `qa_disconnect_warnings` | count | Disconnect episodes flagged |
| 19 | `udp_gate_open` | 0/1 | 0 while waiting for first calm |
| 20 | `session_state` | 0..4 | 0=IDLE, 1=BASELINE, 2=BASELINE_DONE, 3=LIVE, 4=STOPPED |
| 21 | `elapsed_live_sec` | seconds | Time in LIVE |
| 22 | `unity_last_command_code` | 0..4 | 0=none, 1=increase, 2=decrease, 3=start, 4=stop |
| 23 | `unity_commands_sent` | count | Running total of UDP packets sent |
| 24 | `height_m` | metres | Legacy: balloon height from Unity (Acrophobia only), NaN otherwise |

Personal averages and thresholds (channels 9-13) are zero during
baseline and go live the instant calibration completes. QA counters
stay at zero on a clean session; anything nonzero points at a
hardware or contact issue.

### 6.4 Biofeedback_Telemetry LSL stream (1 string channel)

New stream added when the multi-scenario architecture landed. One
JSON string per sample, sent at pipeline rate:

```
{"scenario": "<name>", "data": {<field>: <value>, ...}}
```

Payload is empty string when the receiver has no fresh packet from
Unity (Unity not running yet, or telemetry gone stale). The
dashboard's scene panel subscribes to this stream; any external
tool can too.

### 6.5 UDP bridge to Unity

Plain-text commands to Unity's middleware on port 5005 by default
(`Config.UNITY_UDP_HOST` / `UNITY_UDP_PORT`). Four commands:

| Command | When sent | What Unity does |
|---|---|---|
| `start` | Once, on Start Live Session click | Flips VR scene into running state |
| `stop` | Once, on operator Stop or shutdown | Stops the scene |
| `increase` | While patient is `calm`, throttled | Escalates the scene (balloon up, new audience member, etc.) |
| `decrease` | While patient is `ultra_stressed`, throttled | Backs off (balloon down, person leaves, etc.) |

While the state is `stressed`, nothing is sent. Silence is the
command: Unity holds.

### 6.6 diagnostic.csv columns

Forensic per-tick trace, decimated to `Config.DIAGNOSTIC_CSV_RATE_HZ`
(default 10 Hz). Six columns:

| Column | Meaning |
|---|---|
| `tick_n` | Monotonic counter of rows actually written |
| `phase` | IDLE / BASELINE / LIVE / STOPPED |
| `status` | NEW_DATA / HOLD_LAST / NAN_REJ / OOR_REJ |
| `raw_eda` | Uncleaned EDA reading |
| `raw_hr` | Uncleaned HR reading |
| `raw_hrv` | Uncleaned HRV reading |

Status codes:

- `NEW_DATA`: fresh sample from the device, used.
- `HOLD_LAST`: no new sample this tick, previous value reused.
- `NAN_REJ`: incoming sample was NaN or infinite, rejected.
- `OOR_REJ`: sample was a real number but physiologically
  impossible, rejected.

### 6.7 unity_udp.csv columns

Audit log of every UDP packet sent to Unity:

```
timestamp,kind,command,state,s_t,gate_open
2026-05-31 12:00:00.000,lifecycle,start,,,0
2026-05-31 12:00:01.523,state,increase,calm,-2.4100,1
2026-05-31 12:02:15.117,state,decrease,ultra_stressed,38.7200,1
2026-05-31 12:05:00.000,lifecycle,stop,,,1
```

`kind=lifecycle` covers `start` and `stop` (state and s_t blank
because those are not state-driven). `kind=state` covers `increase`
and `decrease`. `gate_open` is 0 before the wait-for-calm gate
opens, 1 after. Interval between two `state` rows for the same
command should be at least `UNITY_COMMAND_INTERVAL_SEC`.

Console prints sometimes flush in bursts because of Windows stdout
buffering; the CSV is the ground truth.

### 6.8 metadata.json contents

Written once at session start (intake portion), amended once at
baseline lock (baseline + thresholds portion). Structure:

```json
{
  "patient": { "first_name": "...", "last_name": "...",
               "patient_id": "...", "gender": "F",
               "session_date": "2026-...", "session_number": 1 },
  "session_folder": "...",
  "session_timestamp": "...",
  "scenario": "acrophobia",
  "baseline": {
    "captured_at": "...",
    "baseline_duration_sec": 120,
    "sample_rate_pipeline_hz": 10,
    "personal_baselines": { "eda_uS": ..., "hr_bpm": ..., "hrv_ms": ... },
    "sigma_baseline": ...,
    "thresholds": { "mild": ..., "high": ... },
    "hrv_metrics": {...},
    "artifacts_removed": { "eda": ..., "hr": ..., "hrv": ... },
    "source": "...",
    "pipeline_constants": { ... }
  }
}
```

---

## 7. Per-participant tuning

Everything tunable lives in `src/config.py`. Edit, save, re-run.
Nothing else needs to change for any of the values listed here.

The values currently in the file are the ones validated against the
lab's reference implementation and the professor's recipe. Move them
deliberately, one at a time, and watch how the stress index responds.

### 7.1 What OpenSignals does NOT control

OpenSignals only exposes:

- Sampling rate (10 / 20 / 50 / 100 / 200 / ... 4000 Hz)
- Resolution (12 or 16 bit)
- Per-channel sensor type (EDA / ECG / EMG / ...)
- LSL Integration on / off
- Record start / stop

Sensor range is a hardware property of the biosignalsplux EDA
sensor (saturates at 25 µS) and cannot be moved. All
per-participant calibration happens in the Python config.

### 7.2 Fusion weights

```python
WEIGHT_EDA = 0.5
WEIGHT_HRV = 0.3
WEIGHT_HR  = 0.2
```

Per-signal contributions to the composite index. Rule: weights
must sum to 1.0.

| Situation | Suggested tweak |
|---|---|
| Very stable HR but expressive EDA | Bump EDA to 0.6, drop HR to 0.1 |
| Dry skin, weak EDA response | Drop EDA to 0.3, raise HRV to 0.4 and HR to 0.3 |
| On beta-blockers (HR cannot rise) | Drop HR to 0.0 (renormalise: 0.6 / 0.4 / 0.0) |
| Strict Moldoveanu replication | Keep 0.5 / 0.3 / 0.2 |

### 7.3 Threshold multipliers

```python
THRESH_MILD_K = 1.28      # z for 90th percentile
THRESH_HIGH_K = 2.33      # z for 99th percentile
```

Defaults flag the top 10% deviation as stressed and the top 1% as
ultra-stressed.

| Situation | Suggested tweak |
|---|---|
| Score reaches ULTRA too easily on calm tasks | Raise both: MILD=1.50, HIGH=2.58 |
| Score never crosses MILD even during clear stress | Lower MILD to 1.04 |
| Sensitive participant (anxious baseline) | Raise both — more headroom |
| Stoic participant | Lower both — expose subtler arousal |

### 7.4 Sigma floors

```python
HR_SIGMA_FLOOR_PCT     = 2.0
HRV_SIGMA_FLOOR_PCT    = 5.0
EDA_PHASIC_SIGMA_FLOOR = 0.02
SIGMA_FLOOR            = 1e-6
```

Prevents a flat baseline from making trivial live wobbles z-score
huge. Each sigma is clamped up to the floor if it falls below.

| Situation | Suggested tweak |
|---|---|
| Score false-alarms on tiny movements | Raise the relevant floor |
| Score never reacts to mild arousal | Lower the relevant floor |
| Athletes / HRV-rich participants | Raise HRV floor to 8-10% |
| Very nervous baseline | Raise EDA floor to 0.05 µS |

### 7.5 Baseline and window lengths

```python
BASELINE_SEC      = 120     # personal calibration window
HR_WINDOW_SEC     = 30      # trailing HR estimation
RMSSD_WINDOW_SEC  = 60      # trailing RMSSD estimation
HR_COMPUTE_INTERVAL_SEC = 0.5
S_T_SMOOTH_SEC    = 3.0     # rolling mean on the composite
```

Constraint: `RMSSD_WINDOW_SEC` ≥ 30 s is the practical minimum from
Shaffer & Ginsberg 2017 / Munoz 2015.

### 7.6 EDA phasic decomposition

```python
EDA_PHASIC_WINDOW_SEC          = 60
EDA_DECOMP_RATE_HZ             = 10
EDA_PHASIC_UPDATE_INTERVAL_SEC = 0.5
EDA_PHASIC_MAX_US              = 1.0
```

cvxEDA runs every 0.5 s on the last 60 s of raw EDA. Values above
`EDA_PHASIC_MAX_US` are filter-edge ringing and rejected.

### 7.7 RR-interval artifact gate (Malik 20%)

```python
RR_MIN_MS              = 300
RR_MAX_MS              = 1500
RR_MAX_RELATIVE_CHANGE = 0.20
```

Applied after Kubios correction inside the NeuroKit2 backend.
Rejects any RR interval whose change from the previous one exceeds
20%. biosignalsnotebooks does not use this gate; its own
`remove_ectopy` is the equivalent.

### 7.8 Backend selection

```python
HR_HRV_BACKEND  = 'neurokit'   # 'neurokit' | 'bsnb'
EDA_BACKEND     = 'neurokit'   # 'neurokit' | 'bsnb'
EDA_BSNB_METHOD = 'raw'        # 'raw' | 'lowpass_subtract'
```

Switching backends is the quickest A/B on the same participant.
Neither changes the fusion math; only HR / RMSSD / EDA extraction
differs.

### 7.9 Chart Y-axis ranges

```python
EDA_PLOT_DEFAULT_RANGE = (0.0, 25.0)      # full PLUX sensor span
HR_PLOT_DEFAULT_RANGE  = (40.0, 180.0)
HRV_PLOT_DEFAULT_RANGE = (0.0, 200.0)
```

EDA is pinned at the full sensor span so saturation is visible.
Stress-index chart Y-range is locked when thresholds are computed:

```
y_min = -3 × (HIGH - MILD)
y_max = HIGH + 3 × (HIGH - MILD)
```

### 7.10 Per-person calibration workflow

Suggested loop for each new participant:

1. Run with defaults. Observe first 30 s of LIVE.
2. If Score is pinned at 100 in the first minute → sigma floors
   too tight, raise them.
3. If Score is always 0 during obvious arousal → sigma floors too
   loose, or weights wrong for this person.
4. If thresholds look wrong (every breath crosses MILD) → raise
   `THRESH_MILD_K` to 1.50.
5. Save the tweaked config alongside the patient record.

### 7.11 What NOT to touch

- `PIPELINE_RATE` (10 Hz) — controls everything else by ratio
- `LSL_VALUES_PRECONVERTED` (True) — flipping it re-introduces the
  double-conversion bug
- `UDP_GATE_WARMUP_SEC` (1.5 s) — keeps Unity from receiving fake
  "calm" during the buffer warm-up
- `STREAM_TIMEOUT_SEC` (50 s) — Bluetooth dropout watchdog

---

## 8. Error handling and failure modes

Real recordings are messy: electrodes lose contact, Bluetooth drops,
people move, samples come through as garbage. The pipeline is built to
degrade gracefully — reject bad input rather than let it corrupt the
math, warn the operator when data quality drops, and if something
truly fatal happens, still save whatever was recorded before stopping.

### 8.1 Failure-mode matrix

| Failure mode | Handled | Where | What happens |
|---|---|---|---|
| LSL stream not present at startup | yes | `acquisition.py` `_connect_to_stream` | Raises an error; launcher aborts with a clear message |
| Stream goes silent mid-session | yes | `acquisition.py` `get_synchronized_sample` | After `STREAM_TIMEOUT_SEC` (50 s) of silence, raises a connection error; the session ends cleanly |
| Device streams faster than pipeline reads | yes | `acquisition.py` `get_synchronized_sample` | Drains the inlet each tick and uses the most recent sample |
| Dashboard timer cannot keep up with the stream | yes | `dashboard.py` `update_dashboard` | Drains to the latest sample and counts the chunk size; per-state counters stay accurate |
| Recording file has a truncated final row | yes | `data_sources.py` `MockDataSource` | Tolerant parser skips malformed lines |
| Mock file has a different ECG / EDA channel order | yes | `data_sources.py` `parse_opensignals_header` | Channel positions read from the file header, never hardcoded |
| Live PLUX stream has a different channel order | yes | `data_sources.py` `resolve_plux_channels` | Channel labels read from LSL stream metadata; Config indices are fallback |
| NaN or infinite value in a sample | yes | `acquisition.py` `_is_valid_number` | Sample rejected, previous value held, logged as NAN_REJ |
| Physiologically impossible value | yes | `acquisition.py` `_within` checks | Sample rejected, previous value held, logged as OOR_REJ |
| Electrode disconnect (signal pinned flat) | yes | `acquisition.py` `_check_disconnect` | Prints a one-time warning after 15 seconds of flat signal; session continues so partial data is saved |
| Baseline never finishes (signal cut in first 2 min) | yes | acquisition timeout + main loop | Connection error ends the session, CSV flushed |
| Main loop slower than nominal rate at baseline mark | yes | `processing.py` `finalize_baseline_now` | Wall-clock 120 s safety net forces the lock on whatever samples accumulated |
| Flat signal through whole baseline (sigma zero) | yes | `fusion.py` `set_thresholds` | Falls back to a safe default sigma with a loud warning |
| 3-sigma cleaning rejects every sample | yes | `processing.py` `_compute_personal_baselines` | Falls back to the raw buffer with a warning |
| Baseline average of zero | yes | `fusion.py` `compute_s_instant` | Guarded with a tiny floor value |
| R-peak detector finds no beats | yes | `data_sources.py` derive functions | Returns sensible defaults instead of empty or NaN |
| RMSSD outside 5-300 ms | yes | `_gated_rmssd_from_peaks` | Treated as detector failure; previous value held |
| Operator presses Ctrl+C | yes | `main.py` + `launcher.py` | Subprocesses terminated; session CSV already on disk |
| Operator closes the dashboard window during a session | yes | `dashboard.py` `closeEvent` + `main.py` `_handle_shutdown` | Prompts for save vs discard; sends SHUTDOWN_* command |
| Any other unexpected error | yes | `main.py` outer exception handler | Prints error, flushes partial CSV, re-raises |
| Dashboard cannot find the output stream | yes | `dashboard.py` | Catches; prints "ensure main.py is running" |
| Chart Y-axis collapsing on flat data | yes | `dashboard.py` `_create_signal_plot` | Y-range locked each tick |
| UDP send fails (Windows ICMP reset, no receiver) | yes | `unity_bridge.py` `send_raw` | Caught and logged; socket survives for subsequent sends |

### 8.2 What the operator sees

The acquisition layer keeps three running counters and broadcasts
them on the live stream. The dashboard shows them on the Data
Quality strip and turns them red if they go above zero:

- `invalid_sample_count`: NaN or infinite samples rejected
- `out_of_range_count`: samples outside physiological bounds
- `disconnect_warnings_issued`: electrode-disconnect episodes

The UDP bridge tracks its own:

- `commands_sent`: UDP packets actually delivered
- `commands_throttled`: state-driven packets suppressed by the throttle
- `commands_gated`: packets held back during the wait-for-calm gate

On a clean session all these stay at zero. Any nonzero value points
at a hardware or contact problem, not a software one.

---

## 9. File-by-file guide

What each Python file in `src/` and at the repo root actually does.
Useful when you come back to the code after a few weeks and can't
remember which module owns what.

### 9.1 Under src/

| File | Job |
|---|---|
| `__init__.py` | Package marker. Empty. |
| `acquisition.py` | Consumes the incoming LSL stream at pipeline rate. Validates every sample (range check, NaN check, disconnect detection), holds the last valid value when a sample gets rejected, tracks the three data-quality counters. |
| `bsnb_backend.py` | Alternative extraction backend using the biosignalsnotebooks (official PLUX) package. Pan-Tompkins R-peak detection, Lippman-Stein-Lerman ectopy removal, tutorial-style EDA phasic decomposition. Not the default; switched on via `Config.HR_HRV_BACKEND = 'bsnb'`. |
| `config.py` | Central configuration. Every tunable number lives here. Groups: data source, LSL settings, sampling rates, EDA phasic parameters, fusion weights, threshold multipliers, sigma floors, physiological bounds, dashboard visuals, UDP bridge, telemetry receiver. |
| `dashboard.py` | PyQt5 operator interface. Two-panel layout: baseline calibration on the left, live session on the right. Subscribes to Biofeedback_State and Biofeedback_Telemetry LSL streams. Renders all charts and buttons. |
| `data_sources.py` | Two data-source classes with the same interface: `MockDataSource` replays a recorded `.txt` file, `RealPLUXDataSource` reads live from PLUX via OpenSignals LSL. Both convert raw signals to physical units where needed and publish on the internal LSL stream. |
| `DataAnalyze.py` | Standalone reference script from the professor. Not imported anywhere in the pipeline; kept for historical comparison. |
| `fusion.py` | The stress-index engine. Computes per-signal deltas, z-scores them against the personal baseline, applies the weighted composite, smooths to S_t, classifies against thresholds. Also computes the personal baseline sigma at the 120 s lock. |
| `main.py` | The 10 Hz loop that ties everything together. Owns the SessionState state machine, drives acquisition → processing → fusion → output, sends UDP commands to Unity, writes to the session files. Runs as a subprocess. |
| `output.py` | LSL output bridges. Publishes the 25-channel Biofeedback_State stream and the 1-channel Biofeedback_Telemetry string stream. |
| `patient_intake.py` | Modal PyQt5 dialog that runs before the pipeline starts. Collects name, last name, patient ID, gender (dropdown), session date (fixed to today), session number. Validates inline. Returns a dict to the launcher. |
| `processing.py` | Middle-layer processing. Maintains the rolling raw-EDA window for cvxEDA phasic decomposition. Accumulates the baseline buffer during BASELINE state. Runs 3-sigma cleaning at the 120 s lock. |
| `session_control.py` | The SessionState enum, the Command enum, and the Biofeedback_Control LSL bus that carries button clicks from the dashboard back to main. |
| `session_manager.py` | Per-session bookkeeping and file writing. Owns metadata.json, samples.csv, diagnostic.csv. Writes CSV headers, handles the schema-adaptive dynamic columns, manages the LIVE_RESTART CSV rotation. |
| `session_review.py` | Offline replay of a saved session. Takes a session folder, produces a matplotlib figure showing the stress trajectory, physiology traces, and any telemetry field. Optional PDF/PNG export. |
| `streamer.py` | Tiny wrapper subprocess that instantiates a data source (mock or real PLUX per Config) and drives its `get_next_sample()` in a loop. The data source itself handles the LSL outlet and per-sample pacing. |
| `telemetry_receiver.py` | UDP receiver on port 5006. Parses per-scenario JSON packets from Unity, exposes the latest snapshot to main.py. Both scenario label and data payload go verbatim into samples.csv. |
| `unity_bridge.py` | UDP command bridge to Unity on port 5005. Sends `start`, `stop`, `increase`, `decrease` based on the current stress state. Applies the command throttle and the wait-for-calm gate. Writes per-packet audit to unity_udp.csv. |

### 9.2 At the repo root

| File | Job |
|---|---|
| `launcher.py` | Entry point. Shows the patient intake dialog, creates the session folder, writes the initial metadata.json, spawns the three subprocesses (streamer, main, dashboard), and monitors them. |
| `run.bat` / `run.ps1` | Windows launcher scripts. Activate the venv and run `python launcher.py`. |
| `compare_backends.py` / `compare_backends.ipynb` | Post-hoc analysis: runs both extraction backends (NeuroKit2 and biosignalsnotebooks) on the same recording and reports Pearson correlations for HR, RMSSD, and the composite stress index. Standalone; not imported by the live pipeline. |
| `datasource_2.py` | Alternative post-hoc analysis using the reference math from the professor's earlier script. Standalone. |
| `requirements.txt` | Python dependencies. |
| `.gitignore` | Excludes `env/`, `data/`, `references/*.pdf`. |

### 9.3 Under scripts/

| File | Job |
|---|---|
| `lsl_inspect.py` | Continuously prints what OpenSignals' LSL stream is actually publishing per channel. Used to diagnose dead channels or the pre-converted-vs-ADC question. |
| `plux_direct_inspect.py` | Bypasses OpenSignals entirely; talks directly to the PLUX hub over Bluetooth using the official PLUX Python API. Useful when OpenSignals itself is misbehaving. |
| `telemetry_simulator.py` | Pretends to be a Unity scene by sending fake JSON telemetry to UDP 5006 at 10 Hz. Lets the pipeline be tested end-to-end without an actual Unity build. Three built-in scenarios (`acrophobia`, `arachnophobia`, `public_speaking`) plus an ad-hoc mode. |

### 9.4 Under references/

Downloaded PDFs of the papers cited in `METHODOLOGY.md` §13. See
`references/DOWNLOAD_MANIFEST.md` for the current status of what has
been downloaded and what is still missing.

---

## 10. Where to find more

Documents you might want to open next, depending on what you are
doing:

- `METHODOLOGY.md` — the paper draft. Signal processing math,
  reference list, architecture diagrams. Written for lifting into
  LaTeX.

- `METHODOLOGY.docx` — Word version of the same content, for
  co-authors who do not use LaTeX. Regenerated from the markdown
  by the converter script.

- `README.md` — the GitHub landing page. Short overview plus
  links.

- `HOW_TO_RUN.md` — day-to-day operator notes for running a
  session.

- `LAB_CHECKLIST.md` — physical setup checklist for the lab.

- `FUTURE_CHANGES.md` — parking lot of decisions made and changes
  planned. GDPR to-do list. Supervisor question log.

- `SCENARIOS.md` — registry of the known VR scenes and their
  telemetry fields.

- `UNITY_TELEMETRY_CONTRACT.md` — the JSON contract the Unity
  teammate needs to satisfy.

- `references/DOWNLOAD_MANIFEST.md` — status of the reference
  PDFs (28 downloaded, 5 missing with fallback strategies).
