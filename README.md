# Biofeedback Acrophobia Therapy Pipeline

A closed-loop biofeedback system for VR height-exposure therapy. Three physiological signals from a PLUX device drive the height of a virtual balloon: when the patient is stressed the balloon descends to give relief, when they calm down it rises to expose them to more height. The patient can't move it directly — only their own autonomic state can. The point is to titrate exposure to how the person is actually doing, not to what they say they feel.

This repository is the Python side: signal acquisition, the stress-fusion math, the clinical dashboard, and session logging. The VR scene itself (Unity, Oculus Quest) is a separate piece that subscribes to the stream this code produces.

## Where to find things

This is the map. If someone asks you a question about the system, this table tells you which file answers it, so you don't have to dig through everything.

| If you want to know... | Read |
|---|---|
| What a term means (R-peak, RMSSD, EDA, EMA, baseline, sigma, S_t...) | `CONCEPTS.md` |
| How to actually run a session, day to day | `HOW_TO_RUN.md` |
| What the system produces and what every number/column/channel means | `OUTPUTS.md` |
| What the PLUX device gives us (the raw input side) | `SIGNALS_AND_DATA.md` |
| How a reading becomes a stress level and a balloon height, step by step | `DATA_FLOW.md` |
| How heart rate is calculated from the ECG | `DATA_FLOW.md` (Layer 2) and `CONCEPTS.md` (R-peak) |
| How the stress index is calculated | `DATA_FLOW.md` (Layers 6-7) |
| Why the thresholds are set where they are | `DATA_FLOW.md` (Layers 5, 8) and `CONCEPTS.md` (sigma_baseline) |
| How the balloon height moves and the three modes | `DATA_FLOW.md` (Layer 8) |
| How the code handles noise, dropouts, and bad input | `CODE_AUDIT.md` |
| How to set up the real device in the lab | `LAB_SETUP.md` |

A quick worked example of using the map: a professor asks "how did you compute heart rate variability?" You'd open `CONCEPTS.md` for the definition of RMSSD and `DATA_FLOW.md` Layer 2 for exactly how the code does it. Two files, no searching.

## Running it

```
python launcher.py
```

It asks for a patient name and ID, then starts everything. The first 120 seconds are a baseline measurement (the patient sits still); after that the live therapy phase runs, by default for 5 minutes or until you stop it with Ctrl+C. Full detail, including running the parts separately for debugging, is in `HOW_TO_RUN.md`.

## What you need installed

Python 3.10 or so, and the packages in `requirements.txt` (pylsl, numpy, scipy, PyQt5, pyqtgraph, matplotlib, pandas). The repo ships with an `env/` virtual environment, so usually:

```
env\Scripts\activate
pip install -r requirements.txt
```

## How the pieces fit together

Three programs pass data to each other over LSL (Lab Streaming Layer, a small protocol for streaming signal data). They start in order, because each subscribes to what the one before it publishes:

```
streamer.py    reads the device (or a recorded file) and publishes the raw signals
     |
main.py        smooths them, calibrates a 120-second baseline, computes the stress
     |          index and balloon height, and publishes the 18-channel output stream
     |
dashboard.py   draws the live charts for the operator
```

A fourth script, `session_review.py`, is separate and offline: it replays any saved session CSV so you can look at it afterward.

Switching between recorded data and the live device is one setting: `Config.DATA_SOURCE = 'mock'` replays a file, `'real_plux'` reads the PLUX hardware. Nothing else changes.

## Configuration

Almost everything tunable is in `src/config.py`: the data source, the session difficulty mode, the smoothing factors, the fusion weights, the stress thresholds, the balloon altitude ranges, the heart-rate detection settings, the physiological sanity bounds, and the dashboard chart ranges. The design intent is that you change behavior there, not by editing the pipeline code. `CODE_AUDIT.md` has a full list of what's config-only versus what needs a code change.

## The source files

| File | What it does |
|---|---|
| `launcher.py` | Starts the three subprocesses in order |
| `src/config.py` | Every tunable value |
| `src/data_sources.py` | Mock and real-device adapters, ADC conversion, R-peak detection |
| `src/acquisition.py` | Reads the stream at 50 Hz, validates samples, detects disconnects |
| `src/processing.py` | Smoothing, the baseline buffer, 3-sigma cleaning |
| `src/fusion.py` | Stress fusion, thresholds, the balloon kinematics |
| `src/output.py` | The 18-channel output stream |
| `src/session_manager.py` | The per-session CSV |
| `src/dashboard.py` | The operator dashboard |
| `src/main.py` | The 50 Hz loop that ties it together |
| `src/session_review.py` | Offline replay of a saved session |

## Status

The Python pipeline and the math are complete and verified against the spec documents (`shayans_biofeedback_math_pipeline` and `shayans_biofeedback_walkthrough`). What's left is integration: the live PLUX device path is built but needs a hands-on dry run, and the Unity scene is being built separately against the stream contract described in `OUTPUTS.md`.
