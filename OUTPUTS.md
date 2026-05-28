# What the system produces, and what every number means

This is the reference I'd hand to anyone who needs to read our data without having written the code. It covers the three files we write to disk, the live network stream we send to Unity and the dashboard, and how to actually interpret the important numbers.

If you only remember one thing: the device gives us voltage, and everything else in here is computed by our software from that voltage.

## The three files we write per session

Every session drops three CSV files into the `data/` folder. They share a timestamp so you can tell which three belong together.

The one that matters for keeping is `session_<timestamp>_<patient>.csv`. That's the clinical record — one row per processing tick (50 per second) with the full picture: the signals, the stress index, the state, the balloon height inputs, everything. This is what you archive per patient and what `session_review.py` reads.

The other two are diagnostic logs you can usually delete after you've confirmed a session went fine:

`acquisition_log_<timestamp>.csv` records what arrived from the device before any processing — the raw signal values and whether each tick got fresh data or had to reuse the last value. Columns: `timestamp, status, raw_eda, raw_hr, raw_hrv`. The `status` column is the interesting one; see the status codes further down.

`processing_log_<timestamp>.csv` records the signals after smoothing. Columns: `timestamp, phase, smooth_eda, smooth_hr, smooth_hrv`. Useful if you want to see how much the EMA filter changed the raw input.

## The session CSV, column by column

Fifteen columns. Here's what each one is, its unit, and when it actually has a meaningful value.

| Column | Unit | Meaning |
|---|---|---|
| `timestamp` | wall clock | Time the row was written, millisecond precision |
| `phase` | text | `BASELINE` for the first 120 s, `LIVE` after |
| `session_mode` | text | `easy`, `moderate`, or `intense` — the difficulty range for this session |
| `patient_name` | text | Entered at launch |
| `patient_id` | text | Entered at launch |
| `eda` | microsiemens | Smoothed skin conductance |
| `hr` | BPM | Smoothed heart rate |
| `hrv` | milliseconds | Smoothed RMSSD (heart-rate variability) |
| `s_instant` | percent-ish | The raw per-tick stress value before smoothing. Zero during baseline. |
| `s_t` | percent-ish | The smoothed stress index — this is the canonical stress number. Zero during baseline. |
| `state` | text | `calm`, `stressed`, or `ultra_stressed`. Always `calm` during baseline. |
| `dashboard_score` | 0–100 | The operator-friendly version of `s_t`. Zero during baseline. |
| `artifacts_eda` | count | How many EDA samples the 3-sigma baseline cleaning threw out |
| `artifacts_hr` | count | Same for HR |
| `artifacts_hrv` | count | Same for HRV |

A couple of things worth knowing. During the 120-second baseline, the stress columns (`s_instant`, `s_t`, `state`, `dashboard_score`) are deliberately zero/calm — the math that produces them isn't running yet, by design. They come alive at the baseline-to-live transition. The artifact counts stay zero until the baseline finishes (that's when the cleaning happens), then hold their final value for the rest of the file.

## The live LSL stream (to Unity and the dashboard)

While a session runs, `main.py` broadcasts a stream called `Biofeedback_State` at 50 Hz. It has 18 channels. Both the dashboard and (soon) the Unity scene subscribe to it. The order is fixed and defined in `src/output.py` as `UnityBridge.CHANNELS`, so anyone coding against it can rely on the index.

| # | Channel | Unit | What it is |
|---|---|---|---|
| 0 | `s_t` | percent-ish | Smoothed stress index. Centered on zero, positive means more stressed than baseline. |
| 1 | `state_enum` | 0/1/2 | 0 = calm, 1 = stressed, 2 = ultra_stressed |
| 2 | `dashboard_score` | 0–100 | Operator display value derived from `s_t` |
| 3 | `y_t` | meters | The balloon's target altitude. This is the main thing Unity acts on. |
| 4 | `eda` | microsiemens | Smoothed skin conductance |
| 5 | `hr` | BPM | Smoothed heart rate |
| 6 | `hrv` | milliseconds | Smoothed RMSSD |
| 7 | `avg_eda` | microsiemens | The patient's personal EDA baseline (0 until baseline locks) |
| 8 | `avg_hr` | BPM | Personal HR baseline |
| 9 | `avg_hrv` | milliseconds | Personal HRV baseline |
| 10 | `thresh_mild` | same as s_t | The calm/stressed boundary, locked after baseline |
| 11 | `thresh_high` | same as s_t | The stressed/ultra boundary, locked after baseline |
| 12 | `baseline_status` | 0/1 | 0 during baseline, 1 once calibration is done |
| 13 | `elapsed_baseline_sec` | seconds | Time since the session started |
| 14 | `mode_enum` | 0/1/2 | 0 = easy, 1 = moderate, 2 = intense |
| 15 | `qa_invalid_count` | count | Running total of NaN/infinite samples rejected |
| 16 | `qa_out_of_range_count` | count | Running total of samples outside physiological bounds |
| 17 | `qa_disconnect_warnings` | count | Running total of electrode-disconnect episodes flagged |

Channels 0 through 6 are the live readings. Channels 7 through 14 are mostly fixed once the baseline locks (the personal averages, the thresholds, the mode) — they're there so a consumer that joins mid-session has everything it needs without missing the baseline. Channels 15 through 17 are the data-quality counters; on a clean session they stay at zero.

For Unity specifically, the channels that matter most are `y_t` (channel 3, the balloon height), `state_enum` (channel 1), and `mode_enum` (channel 14, so it knows which altitude range to render).

## The status codes in the acquisition log

The `status` column in `acquisition_log_*.csv` tells you what happened to each incoming tick:

- `NEW_DATA` — a fresh sample arrived from the device and was used. This is what you want to see most of the time.
- `HOLD_LAST` — no new sample this tick, so we reused the previous value. A few of these is normal (the device and our loop aren't perfectly in lockstep). A long run of them means the stream stalled.
- `NAN_REJ` — the incoming sample contained a NaN or infinite value and was rejected; the previous value was held instead.
- `OOR_REJ` — the sample was a real number but physiologically impossible (e.g. a heart rate of 400 BPM), so it was rejected as an artifact.

If you see a lot of `NAN_REJ` or `OOR_REJ`, that points at an electrode or connection problem, not a software problem.

## Reading the important numbers

A few of these come up constantly, so here's how to actually interpret them.

**`s_t`, the stress index.** It's a weighted blend of how far the three signals have moved from the patient's own resting baseline, expressed as a roughly percentage-scale number and smoothed over the last second. Zero means "at baseline". Positive means more aroused than baseline; the bigger the number, the more aroused. Small negative values are possible (slightly more relaxed than baseline) and are nothing to worry about. The absolute scale depends on the person, which is why we compare it against thresholds derived from their own baseline rather than a fixed cutoff.

**`state`.** This is just `s_t` bucketed by the two thresholds. At or below `thresh_mild` it's `calm`. Between the two thresholds it's `stressed` — and that's actually the therapeutic target zone, not a problem. Above `thresh_high` it's `ultra_stressed`, which is the "back off" signal.

**`dashboard_score`, the 0–100 number.** This is a cosmetic remapping of `s_t` for quick reading. 0 is baseline, around 50 means the patient just crossed into the stressed zone, and 100 means they've hit ultra. It does not drive the balloon — it's purely for the operator's situational awareness. The balloon is driven by `state` and `y_t`.

**`y_t`, the balloon height.** Starts at the middle of the session's altitude range. When the patient is calm it drifts up (more exposure to height); when they go ultra it drops (relief); when they're in the stressed target zone it holds. The rates are tuned so a sustained-calm patient takes about 200 seconds to rise the full range and a sustained-ultra patient takes about 100 seconds to fall it — same timing in every mode, just different absolute heights.

**The thresholds.** `thresh_mild` and `thresh_high` are computed once, at the end of the 120-second baseline, from how much that specific patient's stress index naturally wobbles at rest. After that they're frozen for the whole session. That's intentional — if they adapted during the session, a big stress response would raise the bar and hide itself.

## What the dashboard shows

The operator dashboard is a live view of the same stream. Top bar: patient, session mode, phase with a baseline countdown, elapsed time, and a big color-coded state chip (green calm, yellow stressed, red ultra). Left: the stress index chart with the two threshold lines drawn across it and their numeric values labeled on the lines. Right: three stacked charts for EDA, HR, and HRV. Bottom: the patient's personal baseline values, the current state and threshold numbers, and a session-statistics panel that includes time-in-each-state and the three data-quality counters. Those quality counters turn red if they go above zero, so a bad electrode is obvious at a glance.

## The short version for someone who just wants the gist

We record one clinical CSV per session with everything in it, plus two throwaway diagnostic logs. Live, we broadcast an 18-channel stream that the dashboard draws and Unity uses to move the balloon. The single most important number is `s_t`, the stress index; `state` is that number bucketed into calm / stressed / ultra; and `y_t` is the balloon height the stress state drives. Everything is measured relative to the patient's own resting baseline, captured in the first two minutes of every session.
