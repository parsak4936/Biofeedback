# Biofeedback VR Exposure Framework

Real-time biofeedback engine for VR exposure therapy across multiple
phobia scenarios. Reads ECG and EDA from a PLUX biosignalsplux hub,
computes a personalised stress index, and drives whichever Unity VR
scene is connected — acrophobia (heights), arachnophobia (spiders),
fear of public speaking, or a future scene the same infrastructure
supports without changes.

This repository is the Python server side: signal acquisition, the
stress-fusion math, the operator dashboard, session logging, the UDP
bridge to Unity, and the JSON telemetry receiver that lets any Unity
scene report scene-specific data back into the recording. Each phobia
scene is a separate Unity project maintained elsewhere.

---

## Documentation

| If you want to... | Read |
|---|---|
| Understand how everything works, section by section | [DOCUMENTATION.md](DOCUMENTATION.md) |
| Read the paper-draft methodology | [METHODOLOGY.md](METHODOLOGY.md) (or [METHODOLOGY.docx](METHODOLOGY.docx) if you don't use LaTeX) |
| Set up the lab and attach electrodes | [LAB_CHECKLIST.md](LAB_CHECKLIST.md) |
| Run a session day-to-day | [HOW_TO_RUN.md](HOW_TO_RUN.md) |
| Send telemetry from a Unity scene | [UNITY_TELEMETRY_CONTRACT.md](UNITY_TELEMETRY_CONTRACT.md) |
| Know which VR scenes and fields are registered | [SCENARIOS.md](SCENARIOS.md) |
| See what's on the to-do list | [FUTURE_CHANGES.md](FUTURE_CHANGES.md) |
| Grab the reference PDFs cited in the paper | [references/DOWNLOAD_MANIFEST.md](references/DOWNLOAD_MANIFEST.md) |

Start with `DOCUMENTATION.md` — it has the full concepts glossary,
data flow, output-file schemas, per-participant tuning guide, error
handling reference, and a file-by-file guide to the codebase.

---

## Quick start

On the lab workstation with the PLUX hub already paired and
OpenSignals running:

```
run.bat
```

The launcher shows the patient intake dialog. Fill it in, click Start.
Three subprocesses spawn (streamer, main pipeline, dashboard) and the
operator dashboard opens.

If `run.bat` errors out, see [HOW_TO_RUN.md](HOW_TO_RUN.md) for the
manual `python launcher.py` path and dependency troubleshooting.

---

## What runs where

Four programs cooperate on the workstation over LSL and UDP:

```
OpenSignals (vendor)  --LSL-->  streamer.py  --LSL-->  main.py
                                                          |
                                              +-----------+-----------+
                                              |                       |
                                              v                       v
                                         dashboard.py           Unity VR scene
                                              ^                       |
                                              |                       |
                                              +---- JSON on UDP <-----+
```

`streamer.py` reads the raw PLUX stream via OpenSignals. `main.py`
runs the 10 Hz pipeline that turns raw signals into the stress index
and sends `start` / `stop` / `increase` / `decrease` commands to Unity.
`dashboard.py` is the operator's live view. Unity ships back JSON
telemetry per scene so the recording captures whatever fields that
scene reports (balloon height, spider count, audience members
looking, etc).

A fifth script, `session_review.py`, is offline. It replays any saved
session and produces a review figure for the patient file.

---

## Session output

Each session creates one folder under `data/`:

```
data/<session folder>/
    metadata.json    intake, frozen baseline, thresholds, scenario
    samples.csv      per-second clinical record (dynamic columns per scenario)
    diagnostic.csv   forensic per-tick raw signal trace
    unity_udp.csv    audit log of every UDP packet sent to Unity
```

Nothing in `data/` is committed to git. Each machine keeps its own
recordings. Full schema in `DOCUMENTATION.md` §6.

---

## Configuration highlights

Everything tunable lives in `src/config.py`. The flags you touch
most often:

```python
DATA_SOURCE             = 'real_plux'     # 'mock' | 'real_plux'
HR_HRV_BACKEND          = 'neurokit'      # 'neurokit' | 'bsnb'
EDA_BACKEND             = 'neurokit'      # 'neurokit' | 'bsnb'

PIPELINE_RATE           = 10.0            # Hz, internal tick rate
BASELINE_SEC            = 120             # baseline window length

WEIGHT_EDA              = 0.5             # Moldoveanu 2023 weights
WEIGHT_HRV              = 0.3
WEIGHT_HR               = 0.2
THRESH_MILD_K           = 1.28            # 90th-percentile z
THRESH_HIGH_K           = 2.33            # 99th-percentile z
S_T_SMOOTH_SEC          = 3.0             # rolling mean on stress index
```

Per-participant tuning guide and every other knob is in
`DOCUMENTATION.md` §7.

---

## Installation

Python 3.10 or newer, plus the packages listed in `requirements.txt`
(pylsl, numpy, scipy, PyQt5, pyqtgraph, matplotlib, pandas, neurokit2,
biosignalsnotebooks, python-docx, openpyxl, pillow).

```
python -m venv env
env\Scripts\activate
pip install -r requirements.txt
```

If `biosignalsnotebooks` fails to install its transitive dependencies,
install without them and add `bokeh` and `h5py` manually:

```
pip install --no-deps biosignalsnotebooks bokeh h5py
```

---

## Status

The pipeline runs end-to-end against live PLUX hardware. Both
extraction backends work and can be swapped with one config flag for
A/B validation. The multi-scenario telemetry contract is in place,
tested with the Python `scripts/telemetry_simulator.py` harness.
Real participant recordings are landing correctly to disk. Backend
agreement on the clean archive session sits at r ≈ 0.90 for the
composite stress index; details in `METHODOLOGY.md` §10.

Known open items are tracked in `FUTURE_CHANGES.md`. The main ones
today: GDPR name/lastname removal from the recording, a few small
dashboard visual cleanups, and a stream-loss exit handler.
