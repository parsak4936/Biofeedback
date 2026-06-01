# Biofeedback Acrophobia Therapy Pipeline

A closed-loop biofeedback system for VR height-exposure therapy. Three physiological signals from a PLUX device are turned into a single stress state (calm / stressed / ultra) on the Python side. The VR scene reads that state and decides how to move the balloon: ultra-stressed sends it down for relief, calm sends it up for more exposure. The patient cannot move it directly; only their own autonomic state can. The point is to titrate exposure to how the person is actually doing, not what they say they feel.

This repository is the Python side: patient intake, signal acquisition, the stress-fusion math, the operator dashboard with explicit Start / Stop controls, session logging, and a UDP bridge to Unity. The VR scene itself (Unity, Oculus Quest) is a separate project. The same Python pipeline can drive any other exposure paradigm because it only outputs a stress state. The VR side owns the imagery and the kinematics.

## Where to find things

If a question comes up about the system, this table points to the file that answers it.

| If you want to know... | Read |
|---|---|
| What a term means (R-peak, RMSSD, EDA, EMA, baseline, sigma, S_t, SessionState...) | `CONCEPTS.md` |
| How to run a session, day to day | `HOW_TO_RUN.md` |
| How to review a saved session and print it for the patient file | `HOW_TO_RUN.md` (Reviewing past sessions) |
| What the system produces and what every number / column / channel means | `OUTPUTS.md` |
| What the PLUX device provides (the raw input side) | `SIGNALS_AND_DATA.md` |
| How a reading becomes a stress state and a Unity command, step by step | `DATA_FLOW.md` |
| How heart rate is calculated from the ECG | `DATA_FLOW.md` (Layer 2) and `CONCEPTS.md` (R-peak) |
| How the stress index is calculated | `DATA_FLOW.md` (Layers 6-7) |
| Why the thresholds are set where they are | `DATA_FLOW.md` (Layer 5) and `CONCEPTS.md` (sigma_baseline) |
| How the operator's buttons drive the pipeline | `DATA_FLOW.md` (Layer 11) and `CONCEPTS.md` (SessionState) |
| What gets sent to Unity, and when | `OUTPUTS.md` (UDP bridge section) |
| How the code handles noise, dropouts, and bad input | `CODE_AUDIT.md` |
| How to set up the real device in the lab | `LAB_SETUP.md` |

## Running it

```
python launcher.py
```

That spawns a patient intake dialog. Fill in name, last name, ID, gender, and session number (1 to `Config.MAX_SESSION_NUMBER`, default 3). The session date is fixed to today and not editable. The system then starts three subprocesses (streamer, main, dashboard) and the dashboard window opens. From there everything is button-driven on the operator side.

Full operational detail is in `HOW_TO_RUN.md`.

## What needs to be installed

Python 3.10 or so, plus the packages in `requirements.txt`: pylsl, numpy, scipy, PyQt5, pyqtgraph, matplotlib, pandas, neurokit2, python-docx. The repo ships with an `env/` virtualenv already populated, so usually:

```
env\Scripts\activate
pip install -r requirements.txt
```

## How the pieces fit together

Four programs talk over LSL (Lab Streaming Layer, a small protocol for streaming signal data) and UDP:

```
streamer.py    publishes the raw signals on LSL stream "OpenSignals"
     |
     v
main.py        subscribes to OpenSignals; runs the math pipeline state machine;
     |          publishes a 24-channel "Biofeedback_State" LSL stream for the
     |          dashboard; sends compact text commands (start/stop/increase/
     |          decrease) to Unity over UDP on port 5005
     |
     v
dashboard.py   subscribes to "Biofeedback_State" and draws the live charts;
               publishes "Biofeedback_Control" with operator button clicks
               (BASELINE_START, BASELINE_STOP, LIVE_START, LIVE_STOP,
                LIVE_RESTART, and SHUTDOWN_* on window close)
```

The session is operator-driven via dashboard buttons. There is no automatic baseline-to-live transition. The operator clicks Start Baseline when the patient is ready, waits for the 120-second calibration to finish, then clicks Start Live Session when the patient has the VR headset on and is settled.

A fifth script, `session_review.py`, is offline and independent. It replays any saved session CSV for post-hoc analysis and can dump a printable PDF/PNG of the review for attaching to the patient file.

## Switching between mock recordings and live PLUX

A one-line change in `src/config.py`:

```python
DATA_SOURCE = 'mock'        # replay MOCK_DATA_FILE, NeuroKit2 chain (per-beat output)
DATA_SOURCE = 'mock2'       # replay MOCK_DATA_FILE, NeuroKit2 chain (30 s / 60 s sliding-mean output)
DATA_SOURCE = 'real_plux'   # live PLUX hardware via OpenSignals LSL, NeuroKit2 chain (per-beat)
DATA_SOURCE = 'real_plux2'  # live PLUX hardware via OpenSignals LSL, NeuroKit2 chain (sliding-mean)
```

All four paths run the same canonical NeuroKit2 chain (`nk.ecg_clean` → `nk.ecg_peaks(correct_artifacts=True)` → `nk.ecg_rate` / `nk.hrv_time`), following the design principle of the VRET Biofeedback Pipeline technical report Section 1: every number is computed by a validated library call. The `mock` / `mock2` paths replay the recorded file at `Config.MOCK_DATA_FILE`; `real_plux` / `real_plux2` read live samples from the OpenSignals LSL stream. The numbered variants differ in their windowing strategy on top of the same chain: per-beat updates (`mock`, `real_plux`) versus 30 s / 60 s sliding-mean updates every 0.5 s (`mock2`, `real_plux2`). A prominence-based detector is kept as a fallback only for the case where NeuroKit2 is unavailable or raises on the input.

The live paths auto-detect which LSL channel carries ECG and which carries EDA from the stream metadata; if labels are missing, `Config.REAL_PLUX_ECG_CHANNEL` and `REAL_PLUX_EDA_CHANNEL` are the fallback.

## Configuration

Everything tunable lives in `src/config.py`: data source, EMA smoothing factors, fusion weights, stress thresholds, heart-rate detection method (NeuroKit2 is primary), physiological sanity bounds, disconnect-detection thresholds, dashboard chart ranges, UDP target and throttle, wait-for-calm warmup, and the DataSource2 windowing parameters. The design intent is that behavior changes happen there, not by editing pipeline code. `CODE_AUDIT.md` has a full list of what is config-only versus what needs a code change.

## Output

Each session writes per patient:

- `data/patient_<id>_<date>_s<n>.json`: the intake form's record
- `data/baseline_<timestamp>_<patient_id>.json`: captured at the end of baseline (personal averages, sigma, locked thresholds, NeuroKit2 HRV summary, pipeline constants in force)
- `data/session_<timestamp>_<patient_id>.csv`: clinical record, one row per 50 Hz tick, 21 columns covering patient demographics, signals, deltas, stress index, state, score, and artifact counts
- `data/live_log_<timestamp>_<patient_id>.txt`: human-readable 1 Hz transcript of the live session
- `data/unity_udp_log_<timestamp>_<patient_id>.csv`: audit log of every UDP packet sent to Unity (timestamp, command, state, S_t, gate)
- `data/acquisition_log_*.csv` and `data/processing_log_*.csv`: diagnostic logs (raw and smoothed signal traces)

Live, the system broadcasts a 24-channel LSL stream the dashboard draws from, and sends `increase` / `decrease` / `start` / `stop` text commands to Unity over UDP. Every output and what it means is documented in `OUTPUTS.md`.

## The source files

| File | What it does |
|---|---|
| `launcher.py` | Patient intake dialog, then spawns the three subprocesses |
| `src/config.py` | Every tunable value |
| `src/patient_intake.py` | PyQt5 modal form with validated demographic fields |
| `src/data_sources.py` | Mock and real-PLUX adapters (two derivation methods each), ADC conversion, NeuroKit2 R-peak detection, live-stream channel auto-detection |
| `src/streamer.py` | Wrapper that drives the data source's outlet at native rate |
| `src/acquisition.py` | Reads OpenSignals stream at 50 Hz, validates samples, detects disconnects |
| `src/processing.py` | EMA smoothing, baseline buffer, 3-sigma cleaning |
| `src/fusion.py` | Stress fusion, thresholds, state classification |
| `src/session_control.py` | LSL command bus (dashboard -> main) and the SessionState machine |
| `src/session_manager.py` | Per-session CSV, baseline JSON capture, live transcript log |
| `src/output.py` | The 24-channel LSL output stream (Biofeedback_State) |
| `src/unity_bridge.py` | UDP bridge sending balloon commands to Unity (with wait-for-calm gate and per-packet audit log) |
| `src/dashboard.py` | The two-panel operator dashboard |
| `src/main.py` | The 50 Hz loop tying it all together, state-machine driven |
| `src/session_review.py` | Offline replay of any saved session CSV; optional PDF/PNG export |

## Status

Math pipeline (Steps 0-10 from the spec) is implemented and verified against the math-pipeline and walkthrough documents. The operator workflow is button-driven via the two-panel dashboard: Start + Stop on the baseline panel, Start + Stop on the live panel. Unity integration is one UDP socket on port 5005 and four text commands, with an audit log per session. What is left is field validation: real PLUX hardware dry-run, real participant sessions, and tuning the constants in `config.py` against pilot data.
