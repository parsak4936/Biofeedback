# Lab setup guide for the real PLUX device

A print-and-take-to-the-lab guide. It covers opening the case to seeing a live session on the dashboard, with no assumptions about the Python code underneath.

## What to bring

A laptop with this repo on it and `python launcher.py` already tested in mock mode. The PLUX biosignalsplux hub. ECG electrodes (3-lead set) and cable. EDA electrodes (the Ag/AgCl finger pads) and cable. Electrode gel and skin-prep wipes. The laptop charger. And a USB Bluetooth adapter if the laptop's built-in Bluetooth is flaky.

## 1. Hardware setup (about 10 minutes)

Charge the PLUX hub the night before; the side LED should be solid, not blinking. Power it on and it goes into Bluetooth advertising mode.

Pair it from the laptop: Settings, then Bluetooth, then Add device, and pick the hub by its MAC address (printed on the back, something like 00:07:80:0F:31:9C). If it does not appear, hold the power button for five seconds to force it to re-advertise.

Connect the electrodes:

ECG is a standard 3-lead setup. Red lead goes near the right collarbone, yellow near the left collarbone, and black (the ground) on the lower right ribcage or either ankle. Clean the skin with an alcohol wipe first so the electrodes stick well. Hairy skin needs a quick shave. Poor contact is the single most common cause of failed recordings, so this step is worth doing carefully.

EDA goes on the fingertips: two pads on the index and middle fingers of the participant's nondominant hand. This placement is from the math-pipeline document, Step 0 (Boucsein et al. 2012). Check that the contact gel has not dried out.

Snap the cables onto the electrodes and into the hub. Note which physical port holds ECG and which holds EDA. With newer OpenSignals builds the channel labels are auto-detected from the LSL stream; with older builds or unrecognised labels the fallback uses `Config.REAL_PLUX_ECG_CHANNEL` and `REAL_PLUX_EDA_CHANNEL`.

## 2. OpenSignals software (about 5 minutes)

Download OpenSignals (r)evolution from plux.info/15-opensignals and launch it. The device should show up in the Discover panel.

Configure the recording: set the sampling rate to 1000 Hz (200 Hz also works, but 1000 Hz gives cleaner HRV). Confirm which sensor is on which channel.

Turn on the network stream: in Preferences, under Integration, enable Lab Streaming Layer. The default stream name is `OpenSignals`. Leave it as is.

Quick check that the stream is alive, from this repo's PowerShell prompt:

```
env\Scripts\python.exe -c "from pylsl import resolve_stream; print(resolve_stream('name','OpenSignals'))"
```

If a StreamInfo object prints within a couple of seconds, the stream is reachable. If it hangs, make sure OpenSignals is actually recording (the red record button is pressed), not just connected. LSL only broadcasts during an active recording.

## 3. Code config changes (about 1 minute)

Open `src/config.py` and change the data source to one of the live options:

```python
DATA_SOURCE = 'real_plux'    # in-house adaptive R-peak detector
# or
DATA_SOURCE = 'real_plux2'   # NeuroKit2 sliding-window chain (matches vret_server_v2)
```

Channel indices are auto-detected from the LSL stream labels at startup, so `REAL_PLUX_ECG_CHANNEL` and `REAL_PLUX_EDA_CHANNEL` only need editing if labels are missing on the OpenSignals build being used. The auto-detection result is printed at startup, like:

```
[DATA SOURCE] channels resolved by label -> EDA=ch1, ECG=ch2 (labels: ['SEQ', 'EDA', 'ECG']).
```

Nothing else needs to change. Everything tunable lives in `src/config.py`. There is no longer a difficulty mode or a session-length cap to set in Python: Unity handles altitude range, and the session runs until the operator stops it.

## 4. Pre-session checks (5 minutes, do not skip)

These three checks catch most "why is nothing happening" problems before a participant is in the chair.

First, confirm the stream resolves and has the channels expected:

```
env\Scripts\python.exe -c "from pylsl import resolve_stream; s = resolve_stream('name','OpenSignals'); print(s[0].channel_count(), 'channels at', s[0].nominal_srate(), 'Hz')"
```

Something like `2 channels at 1000.0 Hz` (or `3 channels at 1000.0 Hz` for setups with a digital-input channel). If it says 0 channels, the device is not recording.

Second, confirm a real sample arrives and the values move:

```
env\Scripts\python.exe -c "from pylsl import resolve_stream, StreamInlet; s = resolve_stream('name','OpenSignals'); i = StreamInlet(s[0]); print('first sample:', i.pull_sample(timeout=5.0))"
```

A tuple of integers like `([33060, 15198], 12345.6)` should print, and the numbers should change if the command is re-run. All zeros means an electrode is not connected.

Third, run the whole thing for 30 seconds:

```
env\Scripts\python.exe launcher.py
```

Enter a test patient name like `TEST` so the files can be deleted afterward. Within a second the terminal should print tick lines showing EDA, HR, and HRV. Watch for 30 seconds; if the values change and there are no warning messages, the setup is ready for real participants. Close the dashboard window and pick "Discard" in the prompt to clean up the TEST files automatically.

## 5. Recording a real session

Get the participant comfortable, sitting upright with the EDA hand resting palm-up on a table. Explain that the first two minutes will be a baseline measurement where they sit still and breathe normally; nothing happens visually during that time.

Launch with `env\Scripts\python.exe launcher.py`. A patient intake dialog appears first: fill in first name, last name, ID, gender, and session number. The session date is today and is not editable. Hit Start. The launcher then opens the dashboard window plus a terminal showing the streamer / main pipeline logs.

The dashboard splits into two panels. Left is **Baseline Calibration** with two buttons (Start Baseline, Stop), a 02:00 baseline-duration counter, a captured-values readout, and four live signal charts below (EDA, HR, HRV, ECG). Right is **Live Session** with two buttons (Start Live Session, Stop), a live-duration counter, a numeric strip for stress info, and the stress + deltas charts below. The Live Session Start button is disabled until a baseline has been captured.

What to do, in order:

1. Confirm the left-side charts are alive. EDA, HR, HRV, and ECG should all show varying traces. If any is flat for more than 15 seconds a disconnect warning appears: fix the electrode before continuing.
2. Click **Start Baseline** on the left. The phase indicator at the top changes to BASELINE and the baseline-duration counter starts at 00:00. Signal capture for the baseline only begins at this click.
3. Wait the full 2 minutes. When the counter hits 02:00 the left panel populates with the captured personal averages, sigma_baseline, threshold values, and per-signal artifact counts. The status banner changes to "Baseline complete. You can start the Live Session." The Start Live Session button becomes clickable.
4. This is the unhurried window for putting the VR headset on the patient, getting them oriented, and briefing them. The pipeline is in BASELINE_DONE state. No data is being sent to Unity yet, no matter how long this takes.
5. When the patient is ready, click **Start Live Session** on the right. The phase changes to LIVE. The UDP gate indicator shows "WAITING for first calm reading." Once the system observes a real calm state (after the 1.5 s warmup), the gate opens and `increase` / `decrease` commands start flowing to Unity based on the patient's state. The Unity last-command tracker at the bottom of the right panel shows what was most recently sent and the cumulative total.
6. Watch the dashboard during the session. The state chip shows current calm / stressed / ultra. The deltas chart shows which signal is driving the stress changes. The time-in-state counters give a quick read on how the session is going. The Data Quality counters at the bottom stay at zero on a clean session; any nonzero value (especially "Out-of-range") points to a contact issue.
7. To end the session, click **Stop** on the right. Unity gets a stop command. A session summary prints to the terminal. The session CSV, baseline JSON, live transcript, and UDP audit log are on disk.
8. To run another live session on the same patient against the same baseline, click Start Live Session again. A fresh session CSV is created. To re-baseline (e.g. after a long break), click Start Baseline again on the left.
9. To finish for this patient, close the dashboard window. The prompt asks what to do with the data: "Save both", "Keep baseline only (discard live)", "Discard both", or "Cancel". The pipeline shuts down cleanly afterwards.

After the session, run `env\Scripts\python.exe src\session_review.py` to look at what just happened. The interactive picker lists past sessions; pick one to open the review window. Add `--save data\review_<patient>_s<n>.pdf` to dump a printable PDF for the patient file.

## 6. Running multiple sessions for one participant

The Python side does not have a difficulty mode (Unity owns that now). For repeated sessions on the same patient, just run the launcher again and bump the session number in the intake form to 2, 3, and so on (up to `Config.MAX_SESSION_NUMBER`, default 3). Each CSV is tagged with that number, so they are easy to tell apart later and can be compared with the review tool. Give the participant a short break between sessions to let their physiology settle before re-baselining.

## 7. Common problems and what to do

`Could not find stream 'OpenSignals'` usually means OpenSignals is not recording or the LSL output is not enabled. Re-check step 2 and make sure the record button is pressed.

`channel labels missing or partial; using fixed-index fallback EDA=chN, ECG=chM` means the auto-detection could not read labels off the LSL stream. The indices it used are listed; if they are wrong, set `REAL_PLUX_ECG_CHANNEL` and `REAL_PLUX_EDA_CHANNEL` in `config.py` and restart.

A disconnect warning for EDA usually means the finger pads dried out or came loose. Re-gel and reattach; the warning clears on its own once the signal returns.

HR reading zero or wildly wrong means the R-peak detector is not finding heartbeats, almost always due to motion or poor ECG contact. Re-prep the skin and check that the ECG trace actually shows clean spikes. If `real_plux2` reports `Baseline RMSSD outside physiological range`, the ECG quality is too low for NeuroKit2's strict gates; switching to `real_plux` or improving ECG contact is the fix.

A `sigma_baseline is degenerate` warning means a sensor was flat for the whole baseline (an electrode never made contact). The fallback keeps the system running, but restoring contact and redoing the baseline is the right move.

`Stream Lost, no new data for 5 seconds` means Bluetooth dropped mid-session. Reconnect and restart; the partial recording is already saved.

If the dashboard window does not open, the main pipeline probably was not running first. The launcher starts things in the right order, so use the launcher rather than starting windows by hand.

A high out-of-range counter means bad samples are being rejected, which is the system protecting the math from noise. It is informational; improve the electrode contact and it will settle.

## 8. After the visit

Keep the `session_*.csv`, `baseline_*.json`, `live_log_*.txt`, and `unity_udp_log_*.csv` files: they are the per-patient audit trail. The acquisition and processing logs can be deleted to save disk. Worth copying the session CSVs somewhere organised by patient ID for clinical archiving, and running `session_review.py --save` to generate a printable PDF for each one's file.

## 9. If the recording rate changes later

No code changes needed. The pipeline reads the sample rate from the stream and adapts the heart-rate math automatically. 1000 Hz gives slightly cleaner HRV than 200 Hz, but both work.

## 10. Where to look when something breaks

The terminal output is always the first place to check, because every warning and error is tagged with the module name in brackets like `[FUSION]`, `[PROCESSOR]`, `[ACQUISITION]`, `[UNITY]`, `[DATA SOURCE]`, `[STATE]`, `[CONTROL]`, `[SESSION]`. That tag points to the file responsible.

| What it does | File |
|---|---|
| Starts the three subprocesses | `launcher.py` |
| Every tunable value (start here) | `src/config.py` |
| Mock and real device adapters, ADC conversion, R-peak detection, channel auto-detection | `src/data_sources.py` |
| Reads the stream at 50 Hz, validates samples, detects disconnects | `src/acquisition.py` |
| Smoothing, the 120-second baseline, 3-sigma cleaning | `src/processing.py` |
| Stress fusion, thresholds, state classification | `src/fusion.py` |
| The 24-channel output stream | `src/output.py` |
| UDP commands to Unity with throttle, gate, and audit log | `src/unity_bridge.py` |
| The operator dashboard | `src/dashboard.py` |
| Per-session CSV, baseline JSON capture, live transcript log | `src/session_manager.py` |
| Offline replay of a saved session, optional PDF/PNG export | `src/session_review.py` |

The pipeline math has been verified against the spec documents, so anything that goes wrong in the lab is almost certainly at the hardware or contact layer. That is exactly what the data-quality counters, the live signal charts, and the disconnect warnings are there to surface.
