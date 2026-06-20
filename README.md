# Biofeedback Acrophobia Therapy Pipeline

Real-time biofeedback system for VR height-exposure therapy.

Three physiological signals from a PLUX biosignalsplux hub —
electrodermal activity (EDA), heart rate (HR), and heart-rate
variability (RMSSD) — are fused into a single stress index. The Unity
VR scene reads that index and adjusts the balloon altitude in real
time: calm → exposure advances, stressed → hold, ultra-stressed →
back off. The participant cannot move the balloon directly. Only
their own autonomic state can.

This repository is the Python side: signal acquisition, the stress-
fusion math, the operator dashboard, session logging, and the UDP
bridge to Unity. The VR scene itself (Unity, Oculus Quest) is a
separate project.

---

## Documentation

| If you want to know... | Read |
|---|---|
| Exact formulas, both backends, comparison tables | [METHODS.md](METHODS.md) |
| Day-to-day operating procedure | [HOW_TO_RUN.md](HOW_TO_RUN.md) |
| Lab hookup checklist (printable) | [LAB_CHECKLIST.md](LAB_CHECKLIST.md) |
| Glossary of terms (R-peak, RMSSD, EDA, S_t, ...) | [CONCEPTS.md](CONCEPTS.md) |
| End-to-end data flow, layer by layer | [DATA_FLOW.md](DATA_FLOW.md) |
| Output files and their columns | [OUTPUTS.md](OUTPUTS.md) |
| Failure modes and how the code handles them | [CODE_AUDIT.md](CODE_AUDIT.md) |
| Mock-mode replay of saved OpenSignals files | [MOCK_MODE.md](MOCK_MODE.md) |

---

## Quick start

```cmd
run.bat
```

That launches the patient intake dialog. Fill in name, ID, gender, session
number. The launcher then spawns three subprocesses (streamer, main,
dashboard) and the dashboard opens.

If `run.bat` errors out, see [HOW_TO_RUN.md](HOW_TO_RUN.md) for the
manual `python launcher.py` path and dependency setup.

---

## Configuration in one screen

Everything tunable lives in `src/config.py`. The most important flags:

```python
DATA_SOURCE             = 'real_plux'      # 'mock' | 'real_plux'
HR_HRV_BACKEND          = 'neurokit'       # 'neurokit' | 'bsnb'
EDA_BACKEND             = 'neurokit'       # 'neurokit' | 'bsnb'
LSL_VALUES_PRECONVERTED = True             # OpenSignals LSL sends µS / mV directly

PIPELINE_RATE           = 10.0             # Hz, internal tick rate
BASELINE_SEC            = 120              # baseline window length

WEIGHT_EDA              = 0.5              # Moldoveanu 2023 weights
WEIGHT_HRV              = 0.3
WEIGHT_HR               = 0.2
THRESH_MILD_K           = 1.28             # 90th percentile z-score
THRESH_HIGH_K           = 2.33             # 99th percentile z-score
```

Every formula behind those flags is in [METHODS.md](METHODS.md).

---

## How the pieces fit together

Four programs talk over LSL and UDP:

```
streamer.py    publishes raw signals on LSL stream "OpenSignals"
     |
     v
main.py        subscribes; runs the math; publishes "Biofeedback_State"
     |          for the dashboard; sends start/stop/increase/decrease
     |          to Unity over UDP on port 5005
     |
     v
dashboard.py   draws live charts; publishes "Biofeedback_Control"
               with operator button clicks
```

A fifth script, `session_review.py`, is offline. It replays any saved
session for post-hoc analysis and can dump a PDF/PNG for the patient
file.

---

## Output

Each session creates one folder under `data/`:

```
data/<first>_<last>_Session<n>_<YYYY-MM-DD>_<gender>/
    ├── metadata.json     intake + frozen baseline + thresholds
    ├── samples.csv       per-second clinical record
    ├── diagnostic.csv    forensic raw + acquisition status per tick
    └── unity_udp.csv     one row per UDP packet sent to Unity
```

`Config.SAMPLES_CSV_RATE_HZ` controls disk-write density.

---

## Installation

Python 3.10–3.13, plus the packages in `requirements.txt`
(pylsl, numpy, scipy, PyQt5, pyqtgraph, matplotlib, pandas, neurokit2,
python-docx, biosignalsnotebooks, openpyxl, pillow).

```cmd
python -m venv env
env\Scripts\activate
pip install -r requirements.txt
```

If `biosignalsnotebooks` fails to install transitive deps, install with
`--no-deps` and add `bokeh h5py` manually:

```cmd
pip install --no-deps biosignalsnotebooks bokeh h5py
```

---

## Status

The math pipeline runs end-to-end against live PLUX hardware with both
backends switchable. Sessions are recording correctly to disk, the
operator dashboard shows real signals, the Unity balloon responds to
state changes, and the post-hoc analysis tools verify the math on saved
recordings. See [METHODS.md](METHODS.md) §7 for the verified
agreement metrics between the two backends.
