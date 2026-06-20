# How to run

These are the day-to-day notes for running sessions, in the order you need them.

## One-time setup

From the project folder:

```
python -m venv env
env\Scripts\activate
pip install -r requirements.txt
```

The repo already ships with an `env\` folder populated, so the venv-creation step is usually skippable. Dependencies are pylsl, numpy, scipy, PyQt5, pyqtgraph, matplotlib, pandas, neurokit2, python-docx.

## Picking a data source

In `src/config.py`, one line decides where the signals come from:

```python
DATA_SOURCE = 'real_plux'   # live PLUX device via OpenSignals LSL (default)
DATA_SOURCE = 'mock'        # replay MOCK_DATA_FILE for offline testing
```

`real_plux` reads the live LSL stream named `OpenSignals` that PLUX's
desktop software publishes when "Lab Streaming Layer" is enabled in
its preferences. The data source auto-detects which channel index
carries ECG and which carries EDA from the stream metadata. If labels
are absent, `Config.REAL_PLUX_ECG_CHANNEL` and `REAL_PLUX_EDA_CHANNEL`
are the fallback.

`mock` replays the file at `Config.MOCK_DATA_FILE`. Useful for
development, demos, and pipeline smoke-tests without the device
attached. Full details in [MOCK_MODE.md](MOCK_MODE.md).

## Picking a backend (NeuroKit2 vs biosignalsnotebooks)

Two lines in `src/config.py`:

```python
HR_HRV_BACKEND = 'neurokit'   # 'neurokit' | 'bsnb'
EDA_BACKEND    = 'neurokit'   # 'neurokit' | 'bsnb'
```

Both backends share identical CSV format, dashboard, and fusion math.
Only HR / RMSSD / EDA extraction differs. Side-by-side comparison and
exact formulas in [METHODS.md](METHODS.md).

## The normal way to run

```
python launcher.py
```

That is the whole entry point. The launcher walks through three things in sequence:

1. **Patient intake.** A PyQt5 dialog pops up with fields for first name, last name, patient ID, gender (M or F dropdown, no free text), and session number (1 to `Config.MAX_SESSION_NUMBER`, default 3). The session date is fixed to today and is not editable. Every field validates inline; the Start button stays disabled until everything is valid. Cancel exits without starting anything.

2. **Subprocess startup.** Three subprocesses spawn in order: streamer, main, dashboard. The launcher coordinates the order so the LSL streams come up correctly. Launcher messages appear in the terminal and three new windows / consoles appear.

3. **Operator control.** Once the dashboard window is open, the rest is button-driven. No automatic transitions.

## The session, step by step

The dashboard splits into two panels side by side. Each panel has two buttons.

**Left panel: Baseline Calibration.** Start Baseline and Stop. Below them, the personal-baseline readout (filled in once calibration finishes) and four live charts (EDA, HR, HRV, ECG). The charts are alive from the moment the dashboard opens, so electrode contact can be verified before doing anything else.

**Right panel: Live Session.** Start Live Session and Stop. Both buttons stay disabled until a baseline has been captured. Once Start Live is pressed, Start disables and Stop enables.

**Top bar.** Patient name + ID, current phase, and a status banner that mirrors the console state messages (for example "Baseline started: sit still, capture in progress", or "Baseline complete. You can start the Live Session.").

Workflow:

1. Wait for the dashboard to load. Banner reads "Ready. Click Start Baseline to begin."
2. Confirm the four signal charts on the left are alive. EDA, HR, HRV, and ECG should all be moving. If any is flat for more than 15 seconds a disconnect warning appears, which means an electrode needs attention.
3. Click **Start Baseline** on the left. Phase switches to BASELINE and the 120-second baseline-duration counter starts at 00:00. The patient sits still; no stress visualization runs yet, by design. Signal capture for the personal averages only begins at this click; anything before is ignored.
4. Wait for baseline to finish. At 02:00 the captured personal averages, the noise-floor sigma, the locked thresholds, and the artifact counts appear in the readout. The banner switches to "Baseline complete. You can start the Live Session." The signal charts auto-rescale to the patient's range around their baseline.
5. Put the VR headset on the patient. Get them comfortable, brief them. The pipeline is in BASELINE_DONE state and sends nothing to Unity yet.
6. Click **Start Live Session** on the right. Unity gets a `start` command, the wait-for-calm gate is active. Once the patient hits a calm reading (after the 1.5 s warmup window), the gate opens and `increase` / `decrease` commands start flowing to Unity based on state. The stress chart, deltas chart, and numeric strip on the right are all live.
7. Run the session as long as needed. The operator decides when it ends. There is no automatic time cap.
8. Click **Stop** on the right to end. Unity gets a `stop` command. A clinical-style summary (time in CALM / STRESSED / ULTRA, mean and max S_t, score, files written) prints to the terminal. The session CSV, baseline JSON, live transcript, and UDP audit log are on disk.
9. To run another live session on the same patient against the same baseline, click Start Live Session again. The previous session's CSV stays on disk; a new one is created with a fresh timestamp.
10. To start over from scratch with a new baseline, click Start Baseline. Any previously captured baseline for this launch is cleared.

## What happens when you close the dashboard window

Closing the dashboard while in IDLE just exits. During BASELINE / BASELINE_DONE / LIVE / STOPPED, a prompt appears asking what to do with the data:

- **BASELINE** in progress: "Discard and close" or "Cancel". Nothing is saved either way (baseline only locks at 02:00).
- **BASELINE_DONE** with no live session yet: "Save baseline" (keeps `baseline_*.json` and `patient_*.json`), "Discard everything", or "Cancel".
- **LIVE** or **STOPPED**: "Save both", "Keep baseline only (discard live)", "Discard both", or "Cancel".

The chosen action is sent to the main pipeline, which deletes the requested files (closing any open handles first), then exits. The launcher polls the dashboard process and shuts down the streamer once the dashboard is gone.

**Note:** Ctrl+C in the launcher terminal does an immediate kill of all subprocesses with no cleanup. Use the dashboard's close button (the X) if the save/discard prompt matters.

## Running the parts individually for debugging

Each component can run standalone. They must be started in the right order because they wait on each other.

```
python src/streamer.py
python src/main.py
python src/dashboard.py
```

For full demographic fields (gender, session number, etc.) the launcher is required, since those come from the intake dialog. To run main.py and dashboard.py without the launcher, set `PATIENT_NAME` and `PATIENT_ID` environment variables first; they default to `PATIENT / 000` otherwise. On Windows PowerShell:

```
$env:PATIENT_NAME = "Alice"; $env:PATIENT_ID = "001"
```

## Multiple sessions for one patient

Run the launcher each time; the intake dialog appears each time. Bump the session number field to 2, 3, and so on for follow-up visits. Each session gets its own CSV, baseline JSON, and live log, all tagged with that number, so they are easy to tell apart later. Use `session_review.py` to compare them.

Within a single launch, clicking Start Live Session a second time after a Stop produces a new session CSV (and a new live log) rotated to a fresh timestamp. The baseline JSON from the same launch is preserved, so the new live run uses the same calibration. To re-baseline mid-launch (for example after a long break or electrode adjustment), click Start Baseline again.

## Reviewing past sessions

This is independent of everything else. It runs offline on saved CSVs and does not need any of the live processes.

### Interactive picker

```
python src/session_review.py
```

Lists every saved session newest-first. Pick one by number and a matplotlib window opens with:

- S_t over time, with shaded bands for baseline / calm / stressed / ultra
- Raw EDA, HR, HRV traces below (no smoothing per PDF)
- A summary box on the right: patient demographics, session number and date, source label, durations, time-in-state, mean and max S_t, mean dashboard score, personal baseline values, locked thresholds and sigma, artifact counts

The same summary is also printed to the terminal in a paste-friendly multi-line block.

### Open a specific session

```
python src/session_review.py data/Alice_Rossi_Session1_2026-06-01_F
```

Pass either the **session folder** (recommended) or the path to its `samples.csv`. The matching baseline is read from `metadata.json` inside the folder. The picker also still understands the legacy flat `session_*.csv + baseline_*.json` layout for old recordings.

### Print to the patient file (PDF / PNG)

```
python src/session_review.py data/Alice_Rossi_Session1_2026-06-01_F --save review_alice_s1.pdf
```

`--save` writes the review figure to disk at 150 dpi with tight margins, suitable for printing on A4 / Letter and attaching to the patient record. The file extension chooses the format (`.pdf`, `.png`, `.svg`, etc.). The window does not open in this mode.

### Console summary only

```
python src/session_review.py path/to/session.csv --no-window
```

Useful for piping into clinical notes, or for quickly inspecting many files in a row.

## Process dependencies at a glance

```
streamer.py     no dependencies (or OpenSignals software if real-PLUX)
main.py         needs streamer running; needs dashboard's control stream to come up;
                emits two outputs:
                  - LSL stream "Biofeedback_State" (for the dashboard)
                  - UDP commands to Unity on port 5005 (start/stop/increase/decrease)
dashboard.py    needs main running; publishes "Biofeedback_Control" (button clicks
                go back to main as command codes)
Unity scene     optional; if not running, the UDP packets are silently dropped
                and nothing else cares
session_review  completely independent, runs offline on saved CSVs
```

There is a circular dependency between main and dashboard (main consumes Biofeedback_Control which dashboard produces; dashboard consumes Biofeedback_State which main produces). The launcher handles this by spawning streamer first, then main (which blocks waiting for the control stream), then dashboard (which subscribes to Biofeedback_State, then publishes its control stream, which unblocks main).

## When something fails

Terminal output is the first place to check. Every warning and error is tagged with the module name in brackets:

- `[ACQUISITION]`: sample read, validation, disconnects
- `[PROCESSOR]`: smoothing, baseline buffer, 3-sigma cleaning
- `[FUSION]`: stress fusion, thresholds
- `[OUTPUT]`: LSL outlet for the dashboard
- `[UNITY]`: UDP bridge to Unity
- `[CONTROL]`: operator command bus
- `[STATE]`: state machine transitions
- `[SESSION]`: session manager and CSV writes
- `[DATA SOURCE]`: mock or real-PLUX adapter

The most useful for diagnosing live-session issues:

- `Could not find stream 'OpenSignals'`: streamer is not running yet, or OpenSignals software in real-PLUX mode is not recording.
- `Stream Lost: No new data received for 5 seconds`: the source stopped publishing mid-session; usually a Bluetooth drop with real PLUX.
- `[FUSION] WARN: sigma_baseline=0 is degenerate`: a sensor was flat for the whole baseline (electrode never made contact). Restart and redo.
- `[UNITY] gate opened: first calm reading`: confirms the wait-for-calm gate just opened and commands are now flowing to Unity.
- `[PROCESSOR] Wall-clock 120 s reached with N/6000 samples`: the safety net fired (loop ran slower than 50 Hz, but baseline still locks correctly at 02:00).

For lab-specific hardware issues, `LAB_SETUP.md` has a more detailed troubleshooting table.

## Files you will touch most often

`src/config.py`. Every tunable parameter, including the data source, mock file path, smoothing factors, fusion weights, threshold multipliers, R-peak detection method, physiological bounds, UDP target, gate warmup, and the DataSource2 windowing parameters. Changing system behavior should almost always be a Config edit.

`data/`. All session outputs. Per patient session: the intake JSON, the baseline JSON, the session CSV (canonical record), the live transcript text, the UDP audit CSV, plus the two diagnostic logs. The session CSV and baseline JSON are the ones to keep long-term.

## What you do not run separately

`processing.py`, `fusion.py`, `acquisition.py`, `output.py`, `unity_bridge.py`, `session_control.py`, `session_manager.py`, `patient_intake.py` are library modules used by the others. They are not launched directly during a session. Each has a `__main__` block with a small self-test, useful for sanity-checking after a code change but never during a real run.
