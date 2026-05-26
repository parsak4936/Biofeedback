# How to run

These are the notes I keep open when I'm running sessions, so I don't have to re-figure out the order every time.

## One-time setup

From the project folder:

```
python -m venv env
env\Scripts\activate
pip install -r requirements.txt
```

The repo already ships with an `env\` folder so you can usually skip the first line. The dependencies are pylsl, numpy, scipy, PyQt5, pyqtgraph, matplotlib, pandas. Nothing exotic.

## The normal way to run

```
python launcher.py
```

That's it for normal use. The launcher prompts for a patient name and ID, then starts the three subprocesses in the right order and stays alive until you close the dashboard window or hit Ctrl+C in the launcher's terminal.

## What the launcher is actually doing under the hood

The system is three processes that pass data to each other over a small networking protocol called LSL (Lab Streaming Layer). They have to start in a specific order because each one subscribes to whatever the previous one publishes:

1. `streamer.py` produces the input signal stream. In mock mode it reads the OpenSignals file in `data/` and pretends to be a device. In real-PLUX mode the OpenSignals software itself does the publishing, so the streamer process is essentially a no-op.
2. `main.py` subscribes to that input stream, runs everything in the math pipeline (EMA smoothing, 120 s baseline, fusion into S_t, state classification, balloon kinematics), and publishes a 18-channel output stream called `Biofeedback_State`.
3. `dashboard.py` subscribes to `Biofeedback_State` and draws the charts.

If they start in the wrong order, the later ones can't find their input and fail with "stream not found". The launcher sleeps 2 seconds between starting each one to avoid the race.

## Running the parts individually (when you're debugging)

Sometimes it's easier to run one piece at a time, with its own terminal so you can see its logs. They each work standalone, you just have to launch them in order and put a couple of seconds between them.

```
python src/streamer.py
```

```
python src/main.py
```

```
python src/dashboard.py
```

If you go this route, you'll need to set `PATIENT_NAME` and `PATIENT_ID` environment variables before starting `main.py`, otherwise it defaults to `PATIENT / 000`. On Windows PowerShell:

```
$env:PATIENT_NAME = "Alice"; $env:PATIENT_ID = "001"
```

## What runs per patient

For a single patient session:

1. Decide which difficulty mode you're running (`easy`, `moderate`, or `intense`). Open `src/config.py` and set `SESSION_MODE` accordingly. This is the only thing you change between sessions.
2. Run `python launcher.py`.
3. Type the patient's name and ID at the prompt.
4. Wait for the 120-second baseline. The dashboard's phase indicator counts down. During this time the participant is just sitting; no stress visualization yet, that's by design from the spec.
5. The live phase runs for 5 minutes by default (configurable as `LIVE_PHASE_MAX_SEC`). You can stop early with Ctrl+C at any point if the clinician decides to end the session.
6. The session CSV is written to `data/session_<timestamp>_<patient>.csv`.

## The three-mode protocol

If you're running all three modes for the same patient:

- First session: set `SESSION_MODE = 'easy'`, run `launcher.py`, save the CSV.
- Optional rest.
- Second session: change to `'moderate'`, run, save.
- Optional rest.
- Third session: change to `'intense'`, run, save.

You end up with three CSVs and each one has `session_mode` recorded in every row, so they're trivially distinguishable later. Don't forget to give the patient a short break between sessions to settle.

## Reviewing past sessions

This is the one piece of the system that doesn't need anything else running. It just reads saved CSVs.

```
python src/session_review.py
```

It lists every session in `data/` newest-first, asks you to pick one by number, then opens a matplotlib window with the stress curve, all three physiological signals, and a summary panel (mode, duration, time-in-state, artifact counts).

If you want to skip the picker and go straight to a specific file:

```
python src/session_review.py data/session_20260525_143022_Alice_001.csv
```

There's also a `--no-window` flag that just prints a one-line summary to the terminal, useful when scripting.

## Process dependencies at a glance

If you're trying to remember what depends on what:

```
streamer.py     no dependencies (or OpenSignals if real-PLUX)
main.py         needs streamer running
dashboard.py    needs main running
session_review  completely independent, runs offline on saved CSVs
```

So the dependency chain is linear: streamer to main to dashboard, in that order. The review tool sits off to the side and works on already-saved data.

## When something fails

The terminal output is your first stop. Every warning and error is tagged with the module name in brackets like `[ACQUISITION]`, `[FUSION]`, `[PROCESSOR]`. That tells you exactly which file to look at.

A few common ones I've hit:

- "Could not find stream 'OpenSignals'" — the streamer (or OpenSignals software in real-PLUX mode) isn't running yet. Wait a second and retry, or check the streamer's terminal for an error.
- "Stream Lost: No new data received for 5 seconds" — the source stopped publishing. In mock mode that means the streamer process died; in real mode it usually means Bluetooth dropped.
- Dashboard shows but charts are blank — main.py probably hasn't finished baseline yet, or it crashed silently. Check its terminal.
- HR or HRV stuck at constants like 75 or 40 — the ECG signal is flat (electrode disconnected) so R-peak detection found no beats and fell back to defaults.

For real-hardware-specific issues there's a separate `LAB_SETUP.md` with the troubleshooting table.

## Files you'll touch most often

- `src/config.py` — every tunable parameter lives here. Data source, session mode, math constants, sample-rate thresholds, etc. If you're changing the system's behavior, this is usually the file.
- `data/` — all session outputs land here. Three files per session (the audit CSV, an acquisition log, a processing log). Only the `session_*.csv` is the one you keep long-term; the others are diagnostic.

## What I do not run separately

`processing.py`, `fusion.py`, `acquisition.py`, and `output.py` are library modules used by the others. You don't run them directly during a session. They each have a `__main__` block at the bottom that runs a small self-test, useful for sanity-checking after a code change but never during a real run.
