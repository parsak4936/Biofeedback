# How a sample becomes a stress state

This walks one sample from the device all the way to the balloon height, so the whole pipeline is traceable. It matches the architecture diagram (`biofeedback_acrophobia_framework_valid.drawio.png`) and the math-pipeline and walkthrough documents. If you want the input side in isolation, read `SIGNALS_AND_DATA.md`; if you want the outputs in isolation, read `OUTPUTS.md`. This is the end-to-end version.

## Layer 0 — the device

A PLUX biosignalsplux hub sits on the patient with two sensors: ECG (heart electrical activity) and EDA (skin conductance). OpenSignals, the desktop software, talks to it over Bluetooth and publishes the readings two ways — a `.txt` file on disk and a live LSL network stream. Either way, what comes out is raw analog-to-digital counts (16-bit integers, 0 to 65535), not physical units. The device measures voltage; nothing physiological has been computed yet.

The sampling rate and which sensor is on which channel vary between recordings. We've seen 200 Hz and 1000 Hz, and the ECG/EDA channel order flipped between files. So we never hardcode those — we read them from the OpenSignals header. `parse_opensignals_header()` in `src/data_sources.py` pulls the sampling rate and the channel-to-sensor mapping out of the JSON header line, and everything downstream adapts.

## Layer 1 — raw counts to physical units

The ADC integers get converted with PLUX's published transfer constants:

```
EDA (microsiemens) = (ADC / 65536) * 3.0 / 0.132
ECG (millivolts)   = ((ADC / 65536) - 0.5) * 3.0 / 1100 * 1000
```

The 3.0 is the reference voltage, 0.132 is the EDA sensor constant, 1100 is the ECG amplifier gain, and the `- 0.5` recenters ECG because it swings both ways around zero. After this step we have EDA in microsiemens and ECG in millivolts.

## Layer 2 — ECG becomes HR and HRV

The device doesn't give us heart rate. We derive it from the ECG voltage by finding the R-peaks (the tall spikes, one per heartbeat) and measuring the time between them.

The detector is adaptive, which matters because real ECG is noisy and amplitudes differ between people and recordings. A fixed threshold breaks the moment a recording is noisier than expected. Instead, `_detect_r_peaks()` in `src/data_sources.py`:

1. Bandpasses the ECG to 5–15 Hz, the frequency band where QRS energy lives. This kills baseline wander and high-frequency noise.
2. Detects on peak *prominence* (how far a peak rises above its local surroundings) rather than raw height, so a drifting baseline doesn't fool it.
3. Runs two passes: a loose first pass to gather candidate peaks, then it measures the typical R-peak prominence in *this* signal and keeps peaks at half that size. This self-calibration is why the same code handles a clean 42-second clip and a noisy 14-minute recording without changes.
4. Enforces a 300 ms refractory gap so the T-wave that follows each beat isn't miscounted as a second beat.

From the peaks:

```
HR (BPM)    = 60000 / RR_interval_in_ms
RMSSD (ms)  = sqrt( mean( (RR_n+1 - RR_n)^2 ) )   over a rolling 10-second window
```

RR intervals that are wildly off the running median (a missed or doubled beat) get replaced with the median before these calculations, so one bad beat doesn't spike HR or HRV. For the offline mock file this runs once over the whole recording; for the live device it runs incrementally on a rolling 5-second ECG buffer. Same logic both ways.

EDA needs none of this — the converted microsiemens value is used directly. It's a slow signal, so the high sample rate is just oversampling.

## Layer 3 — onto the network at 50 Hz

In mock mode, `MockDataSource` reads the file, does the conversions and HR/HRV derivation above, and publishes three channels — EDA, HR, HRV — on the LSL stream named in `Config.STREAM_NAME` ("OpenSignals"), paced at the file's native rate. In real-device mode the OpenSignals software is the publisher and `RealPLUXDataSource` does the conversion/derivation as samples arrive.

`BiofeedbackAcquisition` (inside `main.py`) consumes that stream at the pipeline rate of 50 Hz. It drains the inlet to the most recent sample each tick rather than reading one-at-a-time, which keeps it from falling behind when the device streams faster than 50 Hz. Every incoming sample is validated here: NaN or infinite values are rejected, physiologically impossible values are rejected, and if a signal goes flat for more than 15 seconds it flags a probable electrode disconnect. If the stream goes silent for 5 seconds it raises a connection error and the session ends cleanly with whatever was recorded saved.

## Layer 4 — smoothing and the baseline

`SignalProcessor` applies a one-pole exponential moving average to each signal:

```
y_t = alpha * x_t + (1 - alpha) * y_(t-1)
```

with alpha 0.05 for EDA, 0.10 for HR, 0.05 for HRV. Lower alpha means heavier smoothing.

For the first 120 seconds (6000 ticks at 50 Hz) the smoothed samples just accumulate in buffers — this is the baseline phase. The patient sits still; no stress math runs yet. At the 120-second mark, `_compute_personal_baselines()` runs once:

1. For each signal, drop any sample more than 3 standard deviations from the mean (motion spikes, glitches). If a signal is perfectly flat the filter is skipped with a warning rather than rejecting everything.
2. Average what's left. Those three averages are the patient's personal resting baseline.
3. Keep the cleaned arrays around so the next layer can compute the noise floor.

The baseline is per-person and computed fresh every session from that session's first two minutes. Two different people produce two different baselines, which is the whole point — everything afterward is measured relative to this patient at rest, not against population norms.

## Layer 5 — the noise floor (sigma)

To know what counts as a real stress response, we need to know how much the stress index naturally jitters when the patient is just sitting there. `calculate_baseline_sigma()` in `src/fusion.py` does this by running the cleaned baseline samples through the same stress math the live phase will use (layers 6 and 7 below), then taking the standard deviation of the result. That number is sigma_baseline.

The two thresholds come from it:

```
thresh_mild = 1.33 * sigma_baseline   (calm / stressed boundary)
thresh_high = 2.28 * sigma_baseline   (stressed / ultra boundary)
```

Both are frozen for the rest of the session. If they adapted live, a big stress response would inflate sigma, raise the bar, and mask itself. There's also a guard: if the baseline was degenerate (flat signal, sigma near zero) it falls back to a safe default and logs a warning rather than setting the thresholds to zero, which would label everything ultra-stressed.

## Layer 6 — fusing three signals into one stress number

Each live sample is turned into a percentage deviation from the personal baseline. HRV is inverted because lower HRV means more stress:

```
delta_EDA = (EDA - avg_EDA) / avg_EDA * 100
delta_HR  = (HR  - avg_HR ) / avg_HR  * 100
delta_HRV = (avg_HRV - HRV) / avg_HRV * 100      (inverted)
```

Then they're combined with weights that reflect how specifically each signal indicates sympathetic arousal:

```
S_instant = 0.5 * delta_EDA + 0.3 * delta_HRV + 0.2 * delta_HR
```

EDA gets the most weight because it's purely sympathetic; HR gets the least because it's influenced by lots of non-stress things. All the weights and the threshold multipliers live in `config.py` if you want to retune them.

## Layer 7 — smoothing the stress index

`S_instant` per tick is noisy, so it's averaged over the last 50 samples (one second at 50 Hz) to produce `S_t`, the canonical stress index. From here on, `S_t` is *the* stress number that drives both the dashboard and the balloon.

## Layer 8 — state and balloon height

`evaluate_state()` does two things from `S_t`.

First it classifies the state: at or below `thresh_mild` it's calm, between the thresholds it's stressed (the therapeutic target zone), above `thresh_high` it's ultra_stressed.

Then it moves the balloon. The altitude `y_t` updates by a small amount each tick depending on state: it rises when the patient is calm (more exposure), holds when they're in the target zone, and falls when they're ultra-stressed (relief). The rates scale with the session's altitude range so the feel is consistent across modes:

```
easy:     20-30 m, midpoint 25 m
moderate: 30-45 m, midpoint 37.5 m
intense:  45-65 m, midpoint 55 m

descent rate = 0.010 * range per second
ascent rate  = 0.005 * range per second
```

Descent is twice as fast as ascent on purpose — relief from being overwhelmed should be quick, re-exposure should be gentle. The net effect is that a sustained-calm patient takes about 200 seconds to float from floor to ceiling, and a sustained-ultra patient takes about 100 seconds to drop from ceiling to floor, in every mode. `y_t` is clamped to the mode's range.

The mode is read from `Config.SESSION_MODE` for now. When the Unity client exists it will send the mode at session start and `main.py` will call `fusion.set_mode()` once — the hook is already there, nothing else changes.

## Layer 9 — the 0-100 score

A cosmetic remap of `S_t` to a 0-100 number for the operator: 0 at baseline, ~50 at the stressed boundary, 100 at ultra. It's display-only and does not drive the balloon.

## Layer 10 — output

Every 50 Hz tick, `UnityBridge.broadcast_state()` pushes an 18-channel vector on the LSL stream `Biofeedback_State`. The full channel list and meanings are in `OUTPUTS.md`. In short it carries the stress index, the state, the balloon height, the three smoothed signals, the personal baselines, the thresholds, the mode, baseline status and elapsed time, and three data-quality counters.

In parallel, `SessionManager` writes the per-tick row to the session CSV, and the acquisition and processing layers each write their own diagnostic log.

## Session lifecycle

```
T=0    Launch, stream resolves, baseline buffering starts
T=120  Personal baselines computed, sigma and thresholds locked, phase flips to LIVE
        (the balloon and stress visualization come alive here)
...    Live therapy: S_t every tick, balloon moves, dashboard updates
T=120+LIVE_PHASE_MAX_SEC   Auto-end (default 5 minutes of live phase)
*or*   Operator presses Ctrl+C at any point
```

Both endings flush the session CSV and print the filename. The session-end cap is one global value (`LIVE_PHASE_MAX_SEC`, default 300 s) that applies to every mode the same way.

## Where each piece lives

- `src/config.py` — every tunable number
- `src/data_sources.py` — file/device adapters, ADC conversion, R-peak detection
- `src/acquisition.py` — 50 Hz consumer, sample validation, disconnect detection
- `src/processing.py` — EMA smoothing, baseline buffering, 3-sigma cleaning
- `src/fusion.py` — sigma, thresholds, stress fusion, state, balloon kinematics
- `src/output.py` — the 18-channel LSL output
- `src/session_manager.py` — the per-session CSV
- `src/dashboard.py` — the operator GUI
- `src/main.py` — the 50 Hz loop tying it together
- `src/session_review.py` — offline replay of a saved session
