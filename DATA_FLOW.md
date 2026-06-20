# How a sample becomes a stress state

A walk through of one sample from the device all the way to the stress state that goes out to Unity, so the whole pipeline is traceable. It matches the architecture diagram (`biofeedback_acrophobia_framework_valid.drawio.png`) and the math-pipeline and walkthrough documents. For the input side in isolation, read `SIGNALS_AND_DATA.md`; for the outputs in isolation, read `OUTPUTS.md`. This is the end-to-end version.

A note on the boundary: Python does the signal processing and the stress classification. Unity owns altitude, scene logic, and any phobia-specific imagery. The Python side only sends "calm / stressed / ultra" as state, and `increase` / `decrease` / `start` / `stop` over UDP. Unity decides how to move the balloon. That split means the same Python pipeline can drive any exposure scene.

## Layer 0: the device

A PLUX biosignalsplux hub sits on the patient with two sensors: ECG (heart electrical activity) and EDA (skin conductance). OpenSignals, the desktop software, talks to it over Bluetooth and publishes the readings two ways: a `.txt` file on disk and a live LSL network stream. Either way, what comes out is raw analog-to-digital counts (16-bit integers, 0 to 65535), not physical units. The device measures voltage; nothing physiological has been computed yet.

The sampling rate and which sensor is on which channel vary between recordings. Both 200 Hz and 1000 Hz are common, and the ECG / EDA channel order can flip between files. So those are not hardcoded: `parse_opensignals_header()` in `src/data_sources.py` pulls the sampling rate and the channel-to-sensor mapping from the JSON header line for mock files, and `resolve_plux_channels()` reads the LSL stream's channel-label metadata for live PLUX. Everything downstream adapts.

## Layer 1: raw counts to physical units

The ADC integers get converted with PLUX's published transfer constants:

```
EDA (microsiemens) = (ADC / 65536) * 3.0 / 0.132
ECG (millivolts)   = ((ADC / 65536) - 0.5) * 3.0 / 1100 * 1000
```

The 3.0 is the reference voltage, 0.132 is the EDA sensor constant, 1100 is the ECG amplifier gain, and the `- 0.5` recenters ECG because it swings both ways around zero. After this step the values are EDA in microsiemens and ECG in millivolts.

## Layer 2: ECG becomes HR and HRV

The device does not give heart rate. The pipeline derives it from the ECG voltage by finding the R-peaks (the tall spikes, one per heartbeat) and measuring the time between them.

Both derivation paths run on the same canonical NeuroKit2 chain, following the VRET technical-report design principle that every number is computed by a validated library call. The chain:

```
nk.ecg_clean
  -> nk.ecg_peaks(correct_artifacts=True)   # signal_fixpeaks internally
  -> nk.ecg_rate(peaks, ...)                # per-sample HR
  -> nk.hrv_time(peaks, ...)["HRV_RMSSD"]   # RMSSD
```

`correct_artifacts=True` is the technical-report Bug 3 fix: it routes detected peaks through `nk.signal_fixpeaks` to correct missed or doubled beats, replacing the older hand-rolled "drop RR intervals > 50% off the median" approach.

HR is the mean of `nk.ecg_rate(peaks, sampling_rate)` over a trailing
`HR_WINDOW_SEC` of ECG (default 30 s). RMSSD is computed from
`_gated_rmssd_from_peaks` (Kubios + Malik 20 % gate) over a trailing
`RMSSD_WINDOW_SEC` (default 60 s). Both are recomputed every 0.5 s
and held with zero-order hold between updates. RMSSD values outside
5-300 ms are rejected as detector failures and the previous valid
value is held.

For mock mode the detection runs once over the whole recording at
load time. For the live PLUX path the same chain runs incrementally
on a rolling ECG buffer (65 seconds, covering the RMSSD window plus a
margin).

EDA needs none of this: the converted microsiemens value is used directly. It is a slow signal, so the high sample rate is just oversampling.

## Layer 3: onto the network at 50 Hz

In mock mode, `MockDataSource` (or `MockDataSource2`) reads the file, does the conversions and HR/HRV derivation above, and publishes three channels (EDA, HR, HRV) on the LSL stream named in `Config.STREAM_NAME` (`OpenSignals`), paced at the file's native rate. A side stream `OpenSignals_ECG` carries the raw ECG voltage for the dashboard's waveform chart. In real-device mode the OpenSignals software is the publisher and `RealPLUXDataSource` (or `RealPLUXDataSource2`) does the conversion / derivation as samples arrive.

`BiofeedbackAcquisition` (inside `main.py`) consumes that stream at the pipeline rate of 50 Hz. It drains the inlet to the most recent sample each tick rather than reading one-at-a-time, which keeps it from falling behind when the device streams faster than 50 Hz. Every incoming sample is validated here: NaN or infinite values are rejected, physiologically impossible values are rejected, and if a signal goes flat for more than 15 seconds it flags a probable electrode disconnect. If the stream goes silent for 5 seconds it raises a connection error and the session ends cleanly with whatever was recorded saved.

## Layer 4: phasic-EDA decomposition + baseline buffering

`SignalProcessor` does **no smoothing** anymore — per the PDF, raw values are passed through unchanged. What this layer still does:

1. **Phasic EDA decomposition.** Every tick, raw EDA is appended to a rolling 60-second window. Every `EDA_PHASIC_UPDATE_INTERVAL_SEC` (default 0.5 s), `nk.eda_phasic` runs on the window to extract the current phasic component (tonic drift removed). The value is held between recomputes. A plausibility ceiling (`EDA_PHASIC_MAX_US` = 1 µS) rejects filter-ringing artifacts from the resampler edge.
2. **Baseline buffering.** For the 120 seconds inside the BASELINE state, raw EDA / HR / HRV values accumulate in per-signal buffers. Phasic EDA values are also captured per-tick into a separate baseline buffer. Accumulation is gated by `accumulate_baseline` (True only while state == BASELINE), so samples from IDLE never leak in.

At the 120-second mark, `_compute_personal_baselines()` runs once:

1. For each signal, drop any sample more than 3 standard deviations from the mean (motion spikes, glitches). HR/HRV NaN warm-up samples are also dropped here so personal averages reflect only real measurements. If a signal is perfectly flat the filter is skipped with a warning rather than rejecting everything.
2. Average what is left. Those three averages are the patient's personal resting baseline.
3. Keep the cleaned arrays around so the next layer can compute the noise floor and per-signal z-score stats.

A wall-clock safety net runs alongside: if 120 seconds of BASELINE has elapsed but the main loop ran slower than nominal 50 Hz and the buffer hasn't quite filled to 6000 samples, `finalize_baseline_now()` forces the computation on whatever is there. The lock therefore always fires at 02:00 on the dashboard counter, even on slower machines.

The baseline is per-person and computed fresh every session. Two different people produce two different baselines, which is the whole point: everything afterward is measured relative to this patient at rest, not against population norms.

## Layer 5: the noise floor (sigma) and per-signal z-score stats

To know what counts as a real stress response, the pipeline needs to know how much the stress index naturally jitters when the patient is at rest. `calculate_baseline_sigma()` in `src/fusion.py` is a two-pass computation:

**Pass 1** — for each baseline sample, build raw per-signal deltas (phasic EDA in µS, HR percent, HRV percent inverted). Compute each signal's own mean + sigma. Apply per-signal sigma floors (HR ≥ 2%, HRV ≥ 5%, EDA phasic ≥ 0.02 µS) so a flukey baseline can't make tiny live wobbles z-score huge.

**Pass 2** — z-score each delta against its own baseline, weight (0.5 / 0.3 / 0.2), accumulate the raw `S_inst` series, smooth to `S_t` with the 1-second rolling mean used live. **Only fully-valid windows** (both HR and HRV non-NaN, i.e. past the 60 s warm-up) contribute.

Then:
- `mean_baseline = mean(s_t_series)` — the centring point for the bands
- `sigma_baseline = std(s_instant_series)` — the spread of the RAW series, not the smoothed one (PDF Bug 7: smoothing shrinks variance ~√N, would collapse the bands)

The two thresholds come from these:

```
thresh_mild = mean_baseline + 1.28 * sigma_baseline   (true 90th-pct z)
thresh_high = mean_baseline + 2.33 * sigma_baseline   (true 99th-pct z)
```

Centring on `mean_baseline` (PDF Bug 6) rather than zero matters: resting S_t is generally not zero, so measuring from zero flags calm participants whose resting drift is slightly positive.

Both are frozen for the rest of the session. If they adapted live, a big stress response would inflate sigma, raise the bar, and mask itself. There is also a guard: if the baseline was degenerate (flat signal, sigma near zero) it falls back to a safe default and logs a warning rather than setting the thresholds to zero, which would label everything ultra-stressed.

## Layer 6: fusing three signals into one stress number

Each live sample is turned into a percentage deviation from the personal baseline. HRV is inverted because lower HRV means more stress:

```
delta_EDA = (EDA - avg_EDA) / avg_EDA * 100
delta_HR  = (HR  - avg_HR ) / avg_HR  * 100
delta_HRV = (avg_HRV - HRV) / avg_HRV * 100      (inverted)
```

Then they are combined with weights that reflect how specifically each signal indicates sympathetic arousal:

```
S_instant = 0.5 * delta_EDA + 0.3 * delta_HRV + 0.2 * delta_HR
```

EDA gets the most weight because it is purely sympathetic; HR gets the least because it is influenced by lots of non-stress things. All the weights and the threshold multipliers live in `config.py` for retuning.

## Layer 7: smoothing the stress index

`S_instant` per tick is noisy, so it is averaged over the last 50 samples (one second at 50 Hz) to produce `S_t`, the canonical stress index. From here on, `S_t` is the single number that drives the dashboard and the state classification.

## Layer 8: state classification

`evaluate_state()` classifies `S_t` into one of three states: at or below `thresh_mild` it is calm, between the thresholds it is stressed (the therapeutic target zone), above `thresh_high` it is ultra_stressed. That is it on the Python side. There is no altitude calculation here. The state is what gets published to Unity, and Unity decides how to translate it into balloon movement (or whatever the active VR scene happens to be).

The Python pipeline used to compute a balloon altitude itself, with rate constants scaled to one of three difficulty modes. All of that is now gone. The reason: the same stress signal should be able to drive an acrophobia balloon, a spider-phobia scene, or a social-anxiety crowd render, and the VR scene knows much better than Python does how to map "the patient just hit ultra" to visual change. So the Python side hands off the state and lets Unity handle the rest. The `BioFeedbackMiddleware.cs` script on the Unity side does the lerp and any per-scene scaling.

## Layer 9: the 0-100 score

A cosmetic remap of `S_t` to a 0-100 number for the operator's dashboard: 0 at baseline, around 50 at the stressed boundary, 100 at ultra. Display-only.

## Layer 10: LSL output

Every 50 Hz tick, `UnityBridge.broadcast_state()` pushes a 24-channel vector on the LSL stream `Biofeedback_State`. The full channel list and meanings are in `OUTPUTS.md`. In short it carries the stress index, the state, the dashboard score, the three smoothed signals, the three percentage deltas from baseline, the personal averages, the locked thresholds, baseline status and per-state elapsed time, three data-quality counters, the UDP gate state, the current session-machine state, and the Unity last-command code plus total-sent counter.

In parallel, `SessionManager` writes the per-tick row to the session CSV (with the full patient demographics from the intake form), writes a 1 Hz human-readable line to the live transcript log, and the acquisition and processing layers each write their own diagnostic log.

## Layer 11: UDP bridge to Unity

A separate, deliberately simple feed runs alongside the LSL output: plain text commands over UDP to Unity's `BioFeedbackMiddleware` on port 5005. The pipeline picks one of four strings (`start`, `stop`, `increase`, `decrease`) based on the current stress state.

The mapping:

- calm -> `increase` (balloon rises, more exposure)
- stressed -> no command sent (balloon holds: silence is the command for "hold")
- ultra_stressed -> `decrease` (balloon descends, relief)
- entering the LIVE state -> `start` (first time and on every subsequent Start Live)
- leaving LIVE -> `stop`

Two safety mechanisms wrap this:

A **command throttle** (default 1 second between consecutive `increase` / `decrease`) prevents flooding. Unity steps the balloon by `stepAmount` (default 1 m) per packet, so without throttling the 50 Hz pipeline would jerk the balloon by 50 m every second. At one command per second, sustained-calm gives a 1 m/s ascent, which Unity's per-frame lerp smooths visually.

A **wait-for-calm gate** holds all state-driven commands when the LIVE phase first starts, until the patient hits a genuine calm reading. A 1.5 second warmup ignores the synthetic "calm" the fusion engine returns while its rolling buffer fills. The reason for the gate: when the LIVE phase begins, the patient may still be adjusting (putting on the VR headset, settling in), so the first stress readings are often noise. Waiting prevents that noise from instantly driving the balloon.

Every packet that actually goes out is also recorded to `data/unity_udp_log_<timestamp>_<patient>.csv` with timestamp, kind (`lifecycle` for start/stop, `state` for increase/decrease), command, current state, current S_t, and the gate-open flag. The dashboard's UDP gate indicator shows when the gate is open versus waiting; the gate state is also on LSL channel 19, and the most recent command + cumulative sent count are on channels 22 and 23.

## Layer 12: operator control via the state machine

The pipeline does not auto-transition from baseline into live. The operator drives transitions via Start and Stop buttons on the dashboard. The states are:

```
IDLE          - waiting for the operator to click Start Baseline
BASELINE      - 120-second baseline buffer is filling
BASELINE_DONE - baseline captured, thresholds locked, JSON written;
                waiting for Start Live
LIVE          - full pipeline: fusion runs, UDP commands to Unity
STOPPED       - live session ended; ready to Start Live again or close
```

When the operator clicks a button, the dashboard publishes a command code (`BASELINE_START`, `BASELINE_STOP`, `LIVE_START`, `LIVE_STOP`, or `LIVE_RESTART`) on the LSL stream `Biofeedback_Control`. Closing the dashboard window prompts for save vs discard, then sends one of `SHUTDOWN_SAVE_BOTH` / `SHUTDOWN_DISCARD_LIVE` / `SHUTDOWN_DISCARD_BOTH`. The main pipeline reads commands every tick and applies them via a small lookup table of valid transitions (`_TRANSITIONS` in `src/session_control.py`). Invalid transitions are no-ops: the dashboard's button enable / disable logic already prevents the operator from clicking anything that does not make sense from the current state, and the main loop is defensive on top of that.

State transitions trigger side effects:

- Entering IDLE from a non-initial state: `SignalProcessor.reset()`, `FusionEngine.reset()`, `UnityUDPBridge.reset()` so the next attempt starts from a clean slate. Diagnostic logs from a Stop-during-baseline are discarded.
- Entering BASELINE from BASELINE_DONE or STOPPED: previously captured baseline is cleared so the new run starts fresh.
- Entering LIVE: `unity.send_raw("start")` fires (whether from BASELINE_DONE on the first live run or from STOPPED on a subsequent one). When coming from STOPPED, the session CSV is rotated to a fresh file and the fusion buffer is reset.
- Entering STOPPED: `unity.send_raw("stop")` fires, and `print_session_summary()` writes a clinical-style block to the console.
- SHUTDOWN commands: per the operator's choice, the relevant files are deleted before main exits cleanly.

## Session lifecycle

```
T=0    Launcher shows the patient intake form; operator fills it in
       Subprocesses start; the dashboard window opens in IDLE state
        - signal charts (EDA, HR, HRV, ECG) are alive immediately
        - all buttons except Start Baseline are disabled

T=t0   Operator clicks Start Baseline.
       IDLE -> BASELINE. Baseline-duration counter starts at 00:00.
        - the live signal charts on the left keep running
        - the stress-related charts on the right stay empty
        - no UDP traffic to Unity

T=t0+120
       BASELINE -> BASELINE_DONE.
        - personal averages, sigma, and thresholds computed and locked
        - baseline_<timestamp>_<patient_id>.json written
        - dashboard shows the captured values, threshold lines drawn
        - banner reads "Baseline complete. You can start the Live Session."
        - Start Live Session button becomes clickable
       Operator puts on the VR headset and gets the patient ready.
       No UDP traffic to Unity yet: the bridge waits in BASELINE_DONE.

T=t1   Operator clicks Start Live Session.
       BASELINE_DONE -> LIVE. unity.send_raw("start") fires.
        - UDP wait-for-calm gate is closed
        - fusion runs, deltas compute, dashboard updates
        - once the patient hits a calm reading (after 1.5 s warmup),
          the gate opens and increase/decrease commands flow to Unity
        - live transcript log accumulates one line per second

...    Live therapy. Session runs until the operator stops it.

End    Operator clicks Stop or closes the dashboard window.
       LIVE -> STOPPED. unity.send_raw("stop") fires.
       Session summary prints to the console. CSV, baseline JSON,
       live transcript, and UDP audit are all on disk.
       Click Start Live again for a second run against the same baseline
       (a fresh session CSV is rotated in).
```

There is no automatic time cap. The session runs as long as the clinical situation calls for; the operator decides when to stop.

## Where each piece lives

- `src/config.py`: every tunable number
- `src/patient_intake.py`: modal demographic dialog before the dashboard
- `src/data_sources.py`: file / device adapters (two derivation methods each), ADC conversion, NeuroKit2 R-peak detection, LSL channel auto-detection for live PLUX
- `src/acquisition.py`: 50 Hz consumer, sample validation, disconnect detection
- `src/processing.py`: phasic EDA decomposition (rolling `nk.eda_phasic`), baseline buffering, 3-sigma cleaning, wall-clock-safety finalization. No EMA smoothing (removed per PDF — raw values flow straight through).
- `src/fusion.py`: sigma, thresholds, stress fusion, state classification
- `src/session_control.py`: Command enum, SessionState enum, control LSL bus
- `src/output.py`: the 24-channel LSL output
- `src/unity_bridge.py`: UDP commands to Unity with throttle, wait-for-calm gate, and per-packet audit log
- `src/session_manager.py`: per-session CSV, baseline JSON capture, live transcript log
- `src/dashboard.py`: two-panel operator GUI with close-window save/discard prompt
- `src/main.py`: the 50 Hz loop with the state machine
- `src/session_review.py`: offline replay of a saved session, with optional PDF/PNG export for the patient file
