# What the system produces, and what every number means

A reference for anyone who needs to read the data without having written the code. It covers the files written to disk, the live network streams that go to the dashboard and Unity, and how to interpret the important numbers.

One thing to remember: the device gives voltage; everything else is computed by software from that voltage.

## The files written per session

Every session creates ONE folder under `data/`. All outputs for that launch live inside it — `metadata.json`, `samples.csv`, `diagnostic.csv`, `unity_udp.csv`. Four files per session, not seven.

The folder name is patient-first so sorting by name groups all sessions for one patient together:

    data/<first>_<last>_Session<n>_<YYYY-MM-DD>_<gender>/

e.g. `data/Alice_Rossi_Session1_2026-06-02_F/`. If the same patient + same session number + same day collides (rare), an `_HHMMSS` suffix is appended so nothing is clobbered.

| File | What it is | When written |
|---|---|---|
| `metadata.json` | Intake form + frozen baseline merged into one JSON. Contains patient demographics, session info, personal averages, sigma_baseline, locked thresholds, bonus HRV metrics, per-signal artifact counts, source label, and the pipeline constants in force. The canonical record of "what calibrated this session." | At session start (intake portion); the baseline portion is appended at the 120 s lock. |
| `samples.csv` | The clinical record. ~1 row per wall second (1 Hz, configurable via `Config.SAMPLES_CSV_RATE_HZ`), 21 columns including patient demographics, phase, signals, percentage deltas, the stress index, the state, the dashboard score, and the baseline artifact counts. First column is `sample_n`, a 1-indexed row counter. | Per pipeline tick during BASELINE and LIVE only, decimated to the configured rate. Skipped entirely in IDLE / BASELINE_DONE / STOPPED so the file ends cleanly. Truncated and restarted on Start Live after a Stop. |
| `diagnostic.csv` | Forensic raw-signal trace, one row per write (1 Hz default). Columns: `tick_n, phase, status, raw_eda, raw_hr, raw_hrv`. The `status` column records the acquisition layer's per-tick verdict (`NEW_DATA`, `HOLD_LAST`, `NAN_REJ`, `OOR_REJ`). Safe to delete after confirming a session went fine. | Per pipeline tick in every phase (decimated). Counter `tick_n` is monotonic across the whole launch. |
| `unity_udp.csv` | Audit log of every UDP packet sent to Unity. Columns: `timestamp, kind` (`lifecycle` for start/stop, `state` for increase/decrease), `command, state, s_t, gate_open`. Use it to verify the throttle is working and to correlate the balloon's behavior with the stress trace. | Per UDP send. |

`metadata.json` and `samples.csv` are the long-term archive. `diagnostic.csv` and `unity_udp.csv` are operational scratch you can clean out later.

## The session CSV, column by column

21 columns. Unit and meaning per column:

| Column | Unit | Meaning |
|---|---|---|
| `sample_n` | integer | 1-indexed row counter. Resets to 1 on Stop-then-Start-Live restart. Combined with the write rate (`Config.SAMPLES_CSV_RATE_HZ`, default 1 Hz) it tells you elapsed seconds. |
| `phase` | text | `BASELINE` for the 120 s baseline, `LIVE` during the live session. Rows for IDLE / BASELINE_DONE / STOPPED are not written. |
| `patient_first_name` | text | From the intake form |
| `patient_last_name` | text | From the intake form |
| `patient_id` | text | From the intake form |
| `gender` | text | F or M, from the intake dropdown |
| `session_date` | YYYY-MM-DD | Set at intake (always today) |
| `session_number` | integer | Set at intake (1 to `Config.MAX_SESSION_NUMBER`) |
| `eda` | microsiemens | Raw skin conductance (no smoothing — see PDF §7). |
| `hr` | BPM | Heart rate from `nk.ecg_rate` over the trailing `HR_WINDOW_SEC` (30 s) of ECG, ZOH-held between recomputes (every 0.5 s). NaN during the first ~3 s warm-up. |
| `hrv` | milliseconds | RMSSD via `nk.hrv_time` over the trailing `RMSSD_WINDOW_SEC` (60 s) of ECG, with the Malik 20% RR-change gate applied. NaN during the 60 s warm-up. |
| `delta_eda` | µS | **Phasic EDA** in microsiemens (channel 6 carries phasic, not a percent — PDF §7 Cause 1). |
| `delta_hr` | percent | HR's deviation from baseline. NaN during HR warm-up. |
| `delta_hrv` | percent | HRV's deviation from baseline (inverted so positive = more stressed). NaN during HRV warm-up. |
| `s_instant` | unitless (z-weighted) | Raw per-tick stress, computed as `0.5·z(phasic_EDA) + 0.3·z(HRV%) + 0.2·z(HR%)` with HR-omit-and-renormalise during HR warm-up. Zero during BASELINE. |
| `s_t` | unitless (z-weighted) | 1-second rolling mean of `s_instant`. Zero during BASELINE. |
| `state` | text | `baseline` during BASELINE; one of `calm` / `stressed` / `ultra_stressed` during LIVE. |
| `dashboard_score` | 0-100 | Operator-friendly remap of `s_t`. Zero during baseline. |
| `artifacts_eda` | count | EDA samples thrown out by the 3-sigma cleaning during baseline |
| `artifacts_hr` | count | Same for HR |
| `artifacts_hrv` | count | Same for HRV |

During the 120 s baseline, the stress-related columns are deliberately zero; the fusion math doesn't run yet, by design. They come alive at the baseline-to-live transition. The artifact counts stay zero until the baseline finishes (that is when the cleaning happens), then hold their final value for the rest of the file.

## The UDP bridge to Unity

Unity gets a deliberately simple feed: plain text commands over UDP to its `BioFeedbackMiddleware`, listening on port 5005 by default (`Config.UNITY_UDP_HOST` / `UNITY_UDP_PORT`). Four commands:

| Command | When sent | What Unity does |
|---|---|---|
| `start` | Once, the moment Start Live Session is clicked (and again on every subsequent Start Live within the same launch) | Flips the VR scene into running state |
| `stop` | Once, when the operator clicks Stop on the live panel (and on shutdown) | Stops the scene |
| `increase` | While the patient is `calm`, at most once per `UNITY_COMMAND_INTERVAL_SEC` (default 1.0 s) | Bumps the balloon target altitude up by one step |
| `decrease` | While the patient is `ultra_stressed`, same throttle | Bumps the balloon target altitude down by one step |

When the state is `stressed` (the therapeutic target zone), nothing is sent. Silence is the command: Unity holds altitude. The goal of the stressed band is to keep the patient there, not to move the balloon.

### The wait-for-calm gate

When the live phase begins, the bridge does not start emitting `increase` / `decrease` immediately. Two reasons: the patient may still be adjusting (putting on the headset, settling into the chair), so the first stress reading is often unreliable; and the fusion engine reports a synthetic `calm` during its first ~1 second of buffer warmup that should not count as a real reading.

The bridge waits through a brief warmup window (`Config.UDP_GATE_WARMUP_SEC`, default 1.5 s) and then watches for the first genuine `calm` state. Until that happens, every `increase` / `decrease` is held back and counted in `commands_gated`. Stressed states during the wait remain silent as usual. The moment the patient first hits calm, the gate opens permanently and the throttled command stream takes over.

The dashboard surfaces the gate state on the live panel: orange "UDP gate: WAITING for first calm reading" while waiting, gray "UDP gate: ACTIVE" once open. The same state is on LSL channel 19 (`udp_gate_open`, 0 or 1) for any other consumer that needs it.

The throttle prevents flooding. Unity's middleware applies `stepAmount` per packet, so without throttling 50 packets/second from the 50 Hz pipeline would jerk the balloon by 50 m/second. At one per second with the default 1 m step, sustained calm gives a 1 m/s ascent, gentle enough for therapeutic pacing, and the Unity-side lerp (`altitudeSmoothing`) smooths it into continuous motion.

The UDP bridge runs in parallel with the LSL stream. They do not share state. LSL is for the dashboard and audit logs (which need every channel and every tick); UDP is for Unity (which only needs the decision).

### Verifying with the UDP audit log

`unity_udp_log_<timestamp>_<patient>.csv` records every packet actually sent. Sample rows:

```
timestamp,kind,command,state,s_t,gate_open
2026-05-31 12:00:00.000,lifecycle,start,,,0
2026-05-31 12:00:01.523,state,increase,calm,-2.4100,1
2026-05-31 12:00:02.534,state,increase,calm,-2.3800,1
2026-05-31 12:02:15.117,state,decrease,ultra_stressed,38.7200,1
2026-05-31 12:05:00.000,lifecycle,stop,,,1
```

- `kind=lifecycle` covers `start` and `stop` (the state and s_t columns are blank because those are not state-driven).
- `kind=state` covers `increase` and `decrease` (state and s_t are populated).
- `gate_open` is 0 before the wait-for-calm gate opens, 1 after.
- The interval between two `state` rows for the same command should be at least `UNITY_COMMAND_INTERVAL_SEC`.

If the file shows two `increase` rows with timestamps less than 1 second apart, that is a real throttle issue. Console prints sometimes flush in bursts because of Windows stdout buffering, so the terminal can give a misleading impression; the audit CSV is the ground truth.

## The live LSL stream

While a session runs, `main.py` broadcasts a stream called `Biofeedback_State` at 50 Hz. The dashboard subscribes for visualization and audit. 24 channels, fixed order. The schema lives in `output.py` as `UnityBridge.CHANNELS` so any consumer can rely on the indices.

Unity does not need this stream; it gets its commands over the UDP bridge. The LSL stream is for the dashboard, the audit pipeline, and any future analysis tools.

| # | Channel | Unit | What it is |
|---|---|---|---|
| 0 | `s_t` | percent-ish | Smoothed stress index, centered on zero |
| 1 | `state_enum` | 0/1/2 | 0 = calm, 1 = stressed, 2 = ultra_stressed |
| 2 | `dashboard_score` | 0-100 | Operator display value derived from `s_t` |
| 3 | `eda` | microsiemens | Smoothed skin conductance |
| 4 | `hr` | BPM | Smoothed heart rate |
| 5 | `hrv` | milliseconds | Smoothed RMSSD |
| 6 | `delta_eda` | percent | EDA deviation from baseline |
| 7 | `delta_hr` | percent | HR deviation from baseline |
| 8 | `delta_hrv` | percent | HRV deviation from baseline (inverted) |
| 9 | `avg_eda` | microsiemens | Personal EDA baseline (zero until baseline locks) |
| 10 | `avg_hr` | BPM | Personal HR baseline |
| 11 | `avg_hrv` | milliseconds | Personal HRV baseline |
| 12 | `thresh_mild` | same as s_t | Locked calm/stressed boundary |
| 13 | `thresh_high` | same as s_t | Locked stressed/ultra boundary |
| 14 | `baseline_status` | 0/1 | 0 during baseline, 1 once calibration is done |
| 15 | `elapsed_baseline_sec` | seconds | Time spent in BASELINE state, frozen on lock |
| 16 | `qa_invalid_count` | count | Running total of NaN/infinite samples rejected |
| 17 | `qa_out_of_range_count` | count | Running total of samples outside physiological bounds |
| 18 | `qa_disconnect_warnings` | count | Running total of electrode-disconnect episodes flagged |
| 19 | `udp_gate_open` | 0/1 | 0 while waiting for first calm, 1 once the gate has opened |
| 20 | `session_state` | 0..4 | 0=IDLE, 1=BASELINE, 2=BASELINE_DONE, 3=LIVE, 4=STOPPED. Drives the dashboard's button enable/disable logic. |
| 21 | `elapsed_live_sec` | seconds | Time spent in LIVE state, frozen on Stop |
| 22 | `unity_last_command_code` | 0..4 | Numeric encoding of the most recent UDP packet sent. 0=none, 1=increase, 2=decrease, 3=start, 4=stop |
| 23 | `unity_commands_sent` | count | Running total of UDP packets actually sent (lifecycle + state) |

The personal averages and thresholds (channels 9-13) are zero during the baseline and become live the instant calibration completes. The QA counters stay at zero on a clean session; any nonzero value points at a hardware or contact issue and turns red on the dashboard.

## The status codes in the acquisition log

The `status` column in `acquisition_log_*.csv` tells what happened to each incoming tick:

- `NEW_DATA`: a fresh sample arrived from the device and was used. This should be the common case.
- `HOLD_LAST`: no new sample this tick, the previous value was reused. A few of these are normal. A long run means the stream stalled.
- `NAN_REJ`: the incoming sample contained a NaN or infinite value and was rejected; the previous value was held.
- `OOR_REJ`: the sample was a real number but physiologically impossible (heart rate of 400 BPM, negative EDA), so it was rejected as an artifact.

Lots of `NAN_REJ` or `OOR_REJ` indicates an electrode problem, not a software problem.

## Reading the important numbers

**`s_t`, the stress index.** A weighted blend of how far the three signals have moved from the patient's own resting baseline, on a roughly percentage scale, smoothed over the last second. Zero means at baseline. Positive means more aroused than baseline; the bigger the number, the more aroused. Small negative values are possible (slightly more relaxed than baseline) and harmless. The absolute scale depends on the person, which is why it is compared against thresholds derived from that person's own baseline rather than a fixed cutoff.

**`state`.** `s_t` bucketed by the two thresholds. At or below `thresh_mild` it is `calm`. Between the thresholds it is `stressed`, and that is the therapeutic target zone, not a problem. Above `thresh_high` it is `ultra_stressed`, the "back off" signal. The state is what gets sent to Unity; Unity decides what to do with it in the scene.

**`dashboard_score`, the 0-100 number.** A cosmetic remapping of `s_t` for quick reading. 0 at baseline, around 50 when the patient just crossed into the stressed zone, 100 at ultra. Display-only; it does not drive the VR scene.

**The deltas.** The three percentage-deviation values, one per signal. Useful for understanding why the stress index moved the way it did: was it mostly EDA, mostly HR, mostly HRV? The dashboard shows them as `dEDA / dHR / dHRV` in the state panel.

**The thresholds.** `thresh_mild` and `thresh_high` are computed once at the end of the 120-second baseline from how much that specific patient's stress index naturally wobbles at rest. After that they are frozen for the whole session. Intentional: if they adapted during the session, a big stress response would raise the bar and hide itself.

## What the dashboard shows

The dashboard is split into two side-by-side panels, one per session phase.

**Top bar** spans both panels: patient name and ID, the current phase (`IDLE`, `BASELINE`, `BASELINE DONE`, `LIVE`, `STOPPED`), and a status banner that mirrors the console state messages.

**Left panel: Baseline Calibration.** Two buttons at the top: Start Baseline, Stop. Below them, the baseline-duration counter (`MM:SS / 02:00`) and a captured-values readout that fills in once calibration finishes (personal averages for EDA, HR, HRV, the noise-floor sigma, the locked thresholds, and the per-signal artifact counts). The bottom of the panel is four stacked live charts: EDA, HR, HRV, and ECG. These charts are alive from the moment the dashboard opens, even during IDLE, so the operator can verify electrode contact before starting baseline.

**Right panel: Live Session.** Two buttons at the top: Start Live Session, Stop. Both are disabled until a baseline has been captured. Below the buttons, the live-duration counter and a numeric strip with: a big colored state chip (CALM green / STRESSED yellow / ULTRA STRESSED red), S_t value, dashboard score, the three deltas, the locked thresholds, time-in-each-state, the UDP gate indicator (orange "WAITING for first calm" / gray "ACTIVE"), and the Unity last-command tracker (color-coded by command: green for INCREASE, red for DECREASE, blue for START, gray for STOP, plus a total-sent counter). The bottom of the panel is the stress-index chart with the mild and high threshold lines drawn across it, and the component-deltas chart below that.

**Bottom strip** spans both panels: data-quality counters (samples processed, invalid samples rejected, out-of-range samples rejected, electrode-disconnect warnings). The counters turn red if they go above zero, so a bad electrode is obvious at a glance.

The buttons enable and disable based on the session state coming in on channel 20. The operator cannot reach an invalid state from clicking; the dashboard only offers the transitions that make sense from where the pipeline currently is.

## The short version

One clinical CSV per session, a JSON capture of the baseline, the patient intake form, a human-readable live transcript, a UDP audit log, and two diagnostic logs. Live, a 24-channel LSL stream goes to the dashboard, plus a UDP feed of plain text commands to Unity. The single most important number is `s_t`, the stress index; `state` is that number bucketed into calm / stressed / ultra; the deltas tell which signal moved how much. Everything is measured relative to the patient's own resting baseline, captured in the first two minutes of the session.
