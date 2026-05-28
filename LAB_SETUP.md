# Lab setup guide for the real PLUX device

Print this before you go to the lab. It takes you from opening the case to seeing a live session on the dashboard, without needing to know what the Python code is doing under the hood.

## What to bring

A laptop with this repo on it and `python launcher.py` already tested in mock mode. The PLUX biosignalsplux hub. ECG electrodes (3-lead set) and cable. EDA electrodes (the Ag/AgCl finger pads) and cable. Electrode gel and skin-prep wipes. The laptop charger. And a USB Bluetooth adapter if your laptop's built-in Bluetooth is flaky.

## 1. Hardware setup (about 10 minutes)

Charge the PLUX hub the night before; its side LED should be solid, not blinking. Power it on and it goes into Bluetooth advertising mode.

Pair it from the laptop: Settings, then Bluetooth, then Add device, and pick the hub by its MAC address (printed on the back, something like 00:07:80:0F:31:9C). If it doesn't appear, hold the power button for five seconds to force it to re-advertise.

Connect the electrodes:

ECG is a standard 3-lead setup. Red lead goes near the right collarbone, yellow near the left collarbone, and black (the ground) on the lower right ribcage or either ankle. Clean the skin with an alcohol wipe first so the electrodes stick well. Hairy skin needs a quick shave. Poor contact is the single most common cause of failed recordings, so this step is worth doing carefully.

EDA goes on the fingertips: two pads on the index and middle fingers of the participant's nondominant hand. This placement is from the math-pipeline document, Step 0 (Boucsein et al. 2012). Check that the contact gel hasn't dried out.

Snap the cables onto the electrodes and into the hub. Note which physical port holds ECG and which holds EDA, because you'll need that in step 3.

## 2. OpenSignals software (about 5 minutes)

Download OpenSignals (r)evolution from plux.info/15-opensignals and launch it. Your device should show up in the Discover panel.

Configure the recording: set the sampling rate to 1000 Hz (200 Hz also works, but 1000 Hz gives cleaner HRV). Confirm which sensor is on which channel and remember that mapping for step 3.

Turn on the network stream: in Preferences, under Integration, enable Lab Streaming Layer. The default stream name is "OpenSignals" — leave it as is.

Quick check that the stream is alive, from this repo's PowerShell prompt:

```
env\Scripts\python.exe -c "from pylsl import resolve_stream; print(resolve_stream('name','OpenSignals'))"
```

If a StreamInfo object prints within a couple of seconds, the stream is reachable. If it hangs, make sure OpenSignals is actually recording, not just connected — LSL only broadcasts during an active recording.

## 3. Code config changes (about 2 minutes)

Open `src/config.py` and change three lines:

```python
DATA_SOURCE = 'real_plux'        # was 'mock'
REAL_PLUX_ECG_CHANNEL = 0        # 0-based, match what you noted in step 1
REAL_PLUX_EDA_CHANNEL = 1        # 0-based, match what you noted in step 1
```

Nothing else in the file needs to change. Everything tunable lives in this one file.

If you want a particular session mode or session length for this visit:

```python
SESSION_MODE = 'easy'            # or 'moderate' or 'intense'
LIVE_PHASE_MAX_SEC = 300         # 5 minutes; set to None for no cap
```

## 4. Pre-session checks (5 minutes, don't skip)

These three checks catch most "why isn't anything happening" problems before a participant is in the chair.

First, confirm the stream resolves and has the channels you expect:

```
env\Scripts\python.exe -c "from pylsl import resolve_stream; s = resolve_stream('name','OpenSignals'); print(s[0].channel_count(), 'channels at', s[0].nominal_srate(), 'Hz')"
```

You want something like "2 channels at 1000.0 Hz". If it says 0 channels, the device isn't recording.

Second, confirm a real sample arrives and the values move:

```
env\Scripts\python.exe -c "from pylsl import resolve_stream, StreamInlet; s = resolve_stream('name','OpenSignals'); i = StreamInlet(s[0]); print('first sample:', i.pull_sample(timeout=5.0))"
```

You should get a tuple of integers like ([33060, 15198], 12345.6), and the numbers should change if you re-run it. All zeros means an electrode isn't connected.

Third, run the whole thing for 30 seconds:

```
env\Scripts\python.exe launcher.py
```

Enter a test patient name like "TEST" so you can delete it afterward. Within a second the terminal should print tick lines showing EDA, HR, and HRV. Watch for 30 seconds; if the values change and there are no warning messages, you're ready to record real participants. Ctrl+C to stop, and delete the TEST session CSV if you want a clean folder.

## 5. Recording a real session

Get the participant comfortable, sitting upright with the EDA hand resting palm-up on a table. Explain that the first two minutes are a baseline measurement where they should sit still and breathe normally, and that nothing happens visually during that time.

Launch with `env\Scripts\python.exe launcher.py`, type the real patient ID, and three windows open: the terminal log, the main pipeline, and the dashboard.

During the session, here's what each part of the dashboard is telling you:

The top header shows the patient and the session mode, so you can confirm the recording is labelled correctly. The phase indicator counts down the baseline; for the first 120 seconds the system is only collecting data, not computing stress. The big color chip shows the current state once the baseline is done (green calm, yellow stressed, red ultra-stressed). The right-hand charts show the live signals; if any goes flat for more than 15 seconds you'll see a disconnect warning. The personal-baseline panel fills in at the 120-second mark with the participant's resting values. The threshold numbers (mild and high) are shown in the stress-state box and printed on the chart's threshold lines. And the data-quality counters in the statistics panel should stay at zero on a clean session — if any turns red, check the electrode contact.

To end the session, either let it stop automatically at the live-phase cap (5 minutes by default) or press Ctrl+C in any window. It prints the saved filename. Three CSVs land in `data/`: the `session_*.csv` is the one to keep (the full per-participant record), and the `acquisition_log_*` and `processing_log_*` files are diagnostics you can usually discard.

To look at what just happened, run `env\Scripts\python.exe src\session_review.py`. It lists past sessions; pick one and it opens a plot of the stress curve, the three signals, and a summary. Good for discussing with the participant or filing with their record.

## 6. Running all three modes for one participant

For the standard three-session protocol: set `SESSION_MODE` to `easy`, run a session, save. Give the participant a short break. Change to `moderate`, run, save. Break again. Change to `intense`, run, save. Each CSV records its mode in the `session_mode` column, so the three are easy to tell apart later, and you can compare them with the review tool.

## 7. Common problems and what to do

"Could not find stream 'OpenSignals'" usually means OpenSignals isn't recording or the LSL output isn't enabled. Re-check step 2 and make sure you've hit record.

"Config asks for channel 1 but the stream only has 1" means the channel indices in config don't match the device. Fix `REAL_PLUX_ECG_CHANNEL` and `REAL_PLUX_EDA_CHANNEL`.

A disconnect warning for EDA usually means the finger pads dried out or came loose. Re-gel and reattach; the warning clears on its own once the signal returns.

HR reading zero or wildly wrong means the R-peak detector isn't finding heartbeats, almost always due to motion or poor ECG contact. Re-prep the skin and check that the ECG trace actually shows clean spikes.

A "sigma_baseline is degenerate" warning means a sensor was flat for the whole baseline (an electrode never made contact). The fallback keeps the system running, but you should restore contact and redo the baseline.

"Stream Lost, no new data for 5 seconds" means Bluetooth dropped mid-session. Reconnect and restart; the partial recording is already saved.

If the dashboard window doesn't open, the main pipeline probably wasn't running first. The launcher starts things in the right order, so use the launcher rather than starting windows by hand.

A high out-of-range counter means bad samples are being rejected, which is the system protecting the math from noise. It's informational; improve the electrode contact and it'll settle.

## 8. After the visit

Keep the `session_*.csv` files; they're your audit trail. You can delete the acquisition and processing logs if you're short on disk. It's worth copying the session CSVs somewhere organized by participant ID for clinical archiving, and running the review tool to generate a summary plot for each one's file.

## 9. If you change the recording rate later

You don't need to touch the code. The pipeline reads the sample rate from the stream and adapts the heart-rate math automatically. 1000 Hz gives slightly cleaner HRV than 200 Hz, but both work.

## 10. Where to look when something breaks

The terminal output is always the first place to check, because every warning and error is tagged with the module name in brackets like [FUSION], [PROCESSOR], or [ACQUISITION], which tells you which file to open.

| What it does | File |
|---|---|
| Starts the three subprocesses | `launcher.py` |
| Every tunable value (start here) | `src/config.py` |
| Mock and real device adapters, ADC conversion, R-peak detection | `src/data_sources.py` |
| Reads the stream at 50 Hz, validates samples, detects disconnects | `src/acquisition.py` |
| Smoothing, the 120-second baseline, 3-sigma cleaning | `src/processing.py` |
| Stress fusion, thresholds, balloon kinematics | `src/fusion.py` |
| The 18-channel output stream | `src/output.py` |
| The operator dashboard | `src/dashboard.py` |
| The per-session CSV | `src/session_manager.py` |
| Offline replay of a saved session | `src/session_review.py` |

The pipeline math has been verified against the spec documents, so anything that goes wrong in the lab is almost certainly at the hardware or contact layer. That's exactly what the data-quality counters and the live signal charts are there to surface.
