# Lab Setup Guide — Real PLUX Hardware

**Print this before you go to the lab.** It walks you from "open the case" to "patient sees the dashboard turn green" without needing to know what the Python code is doing.

---

## 0. What you should bring

- Laptop with this repo cloned and `python launcher.py` already verified in mock mode (you already did this)
- PLUX BiosignalsPLUX device (the hub box)
- ECG electrodes (3-lead set) + ECG cable
- EDA electrodes (Ag/AgCl finger pads) + EDA cable
- Electrode gel / prep wipes
- Laptop charger
- USB Bluetooth adapter (if your laptop's BT is unreliable)

---

## 1. Hardware setup (10 min)

### 1a. Power the device
- Charge the PLUX hub the night before. The LED on the side should be solid (not blinking).
- Power it on; the LED should change colour to indicate Bluetooth advertising mode.

### 1b. Pair Bluetooth
- On the laptop: Settings → Bluetooth → Add device → pick `00:07:80:0F:31:9C` (or whatever MAC is printed on the back of your hub).
- If it doesn't show up, hold the PLUX power button for 5 s to force re-advertise.

### 1c. Connect electrodes
- **ECG (3-lead):**
  - **Red** lead → right collarbone area
  - **Yellow** lead → left collarbone area
  - **Black** (ground) → lower right ribcage *or* either ankle
  - Clean the skin first with an alcohol wipe so adhesion is good. Hairy skin needs a quick shave; the signal noise from poor contact is the #1 cause of failed recordings.
- **EDA:** two electrode pads on the **distal phalanges (fingertips) of the index and middle fingers of the participant's nondominant hand** (per math-pipeline Step 0, citation [3] Boucsein et al. 2012). Make sure the contact gel hasn't dried out.

### 1d. Snap cables to electrodes
- ECG cable goes into the BlueSensor-equivalent ECG sensor port on the PLUX hub.
- EDA cable goes into the EDA sensor port.
- **Note which physical port (channel 1 vs channel 2) holds which sensor** — you'll need this in step 3.

---

## 2. OpenSignals software (5 min)

### 2a. Install + open
- Download OpenSignals (r)evolution from <https://plux.info/15-opensignals>.
- Launch it; the device should appear in the "Discover" panel.

### 2b. Configure recording
- Click the device → **Configure** → set **Sampling rate = 1000 Hz** (or 200 Hz; either works, 1000 Hz gives cleaner HRV).
- Confirm the channel order. The mock you've been running has **CH1=ECG, CH2=EDA**. Set the same in OpenSignals: drag the ECG sensor to "CH1" and EDA sensor to "CH2".
- Save the configuration.

### 2c. Enable LSL output
- **Preferences → Integration → Lab Streaming Layer**: enable it.
- The default stream name is `OpenSignals`. Leave it.

### 2d. Verify LSL is live (quick sanity test)
From this repo's PowerShell prompt:

```powershell
env\Scripts\python.exe -c "from pylsl import resolve_stream; print(resolve_stream('name','OpenSignals'))"
```

If you see a `<StreamInfo>` printed within a couple of seconds → ✅ the LSL stream is reachable. If it hangs → check that OpenSignals is **recording** (not just connected); LSL only broadcasts during active recording.

---

## 3. Code config changes (2 min)

Open `src/config.py` and change three lines:

```python
DATA_SOURCE = 'real_plux'                    # was 'mock'
REAL_PLUX_ECG_CHANNEL = 0                    # 0-based — match step 1d
REAL_PLUX_EDA_CHANNEL = 1                    # 0-based — match step 1d
```

The rest of the file (math constants, modes, etc.) does not change. **Everything tuneable lives in this one file.**

If you also want a different session mode for this lab visit:

```python
SESSION_MODE = 'easy'      # or 'moderate' or 'intense'
LIVE_PHASE_MAX_SEC = 300   # 5 minutes — set to None for no cap
```

---

## 4. Pre-session sanity tests (5 min — don't skip)

These three tests catch ~95% of "why isn't anything happening" problems before you have a real participant in the chair.

### 4a. Stream resolves
```powershell
env\Scripts\python.exe -c "from pylsl import resolve_stream; s = resolve_stream('name','OpenSignals'); print(s[0].channel_count(), 'channels at', s[0].nominal_srate(), 'Hz')"
```
Expected: prints something like `2 channels at 1000.0 Hz`. If channel count is 0, the device isn't actually recording — go back to step 2.

### 4b. Sample arrives + ADC values look right
```powershell
env\Scripts\python.exe -c "from pylsl import resolve_stream, StreamInlet; s = resolve_stream('name','OpenSignals'); i = StreamInlet(s[0]); print('first sample:', i.pull_sample(timeout=5.0))"
```
Expected: a tuple of small integers like `([33060, 15198], 12345.6)`. Numbers should change each time you re-run. If they're all zero, an electrode isn't connected.

### 4c. Run the full pipeline for 30 seconds
```powershell
env\Scripts\python.exe launcher.py
```
- Enter a test patient name (use "TEST" so you can identify and delete it later)
- Watch the terminal: you should see `[TICK 00050] NEW_DATA | EDA: 5.x μS | HR: 7x BPM | HRV: x.x ms` within ~1 second
- Wait 30 seconds. If no `[WARN]` messages and the values change → ✅ ready to record
- Press Ctrl+C to stop. Delete `data/session_*_TEST_*.csv` if you want a clean folder

---

## 5. Recording a real session (the main event)

### 5a. Get the participant comfortable
- Sitting upright, hands relaxed (the EDA hand resting palm-up on a table is best)
- Explain that the first 2 minutes are a baseline measurement — they should sit still and breathe normally. Nothing happens visually during baseline.

### 5b. Launch
```powershell
env\Scripts\python.exe launcher.py
```
- Type the patient's real ID/name
- Three windows open: terminal (acquisition log), main pipeline, dashboard

### 5c. What to watch during the session

| Dashboard field | What it tells you |
|---|---|
| **Patient: name (id) + Session: EASY/MODERATE/INTENSE** | Top header — confirms this recording is labelled correctly |
| **Phase: BASELINE (XXs remaining)** | Countdown — for the first 120 s the math is *only* collecting data, not computing stress |
| **Duration: MM:SS** | Total elapsed time since launch |
| **Big colored chip ● CALM / ⚠ STRESSED / 🔴 ULTRA STRESSED** | Current state classification (only meaningful after T=120 s) |
| **EDA / HR / HRV / ECG charts (right column)** | Live signal traces — if any is flat-lined for >15 s you'll see a `[WARN] Possible electrode disconnect` |
| **Personal Baseline (bottom-left)** | Populates at T=120 s with the participant's resting averages |
| **Thresholds: MILD = x.xx, HIGH = y.yy** (in Current Stress State box) | The numeric stress boundaries — locked at T=120 s, displayed throughout LIVE |
| **Session Statistics → Data Quality counters** | `Invalid samples` / `Out-of-range` / `Disconnects`. **Should stay at 0 on a clean session. Any nonzero value turns red — investigate the electrode contact.** |

### 5d. End of session
- Either let it auto-end at `LIVE_PHASE_MAX_SEC` (default 5 min after baseline), or press **Ctrl+C** in any of the windows
- The pipeline prints `[SESSION] Output saved to: session_YYYYMMDD_HHMMSS_<patient>.csv`
- Three CSVs are now in `data/`:
  - `session_*.csv` — the canonical per-participant audit record (this is the one to keep)
  - `acquisition_log_*.csv` — raw 50 Hz samples + ZOH state per tick (diagnostic only)
  - `processing_log_*.csv` — smoothed signals per tick (diagnostic only)

### 5e. Review what just happened (optional but recommended)
```powershell
env\Scripts\python.exe src\session_review.py
```
Picker lists every past session newest-first; pick one to open the matplotlib view (S_t curve with state bands, all three signals, summary panel). Gives you a one-page clinical view to discuss with the participant or save with their file.

---

## 6. Multi-session protocol (easy → moderate → intense)

If you're running the standard three-session protocol for one participant:

1. **Session 1 — easy.** Set `Config.SESSION_MODE = 'easy'`, run. Save the CSV.
2. **Optional break** for the participant.
3. **Session 2 — moderate.** Change to `'moderate'`. Run. Save.
4. **Optional break.**
5. **Session 3 — intense.** Change to `'intense'`. Run. Save.

Each session gets its own CSV labelled with the mode in the `session_mode` column. You can compare them later with `session_review.py`.

---

## 7. Common errors and fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `[ERROR] Could not find stream 'OpenSignals'` | OpenSignals not recording, or LSL output not enabled | Step 2c (enable LSL) + click ▶ Record in OpenSignals |
| `RuntimeError: Config asks for channel 1 but the stream only has 1` | Channel index mismatch | Step 3 — set `REAL_PLUX_ECG_CHANNEL` / `REAL_PLUX_EDA_CHANNEL` to match the physical ports |
| `[WARN] Possible electrode disconnect: EDA` | EDA finger pads dried out or detached | Re-gel and reattach; the warning clears automatically once signal returns |
| HR chart shows 0 BPM or wildly wrong values | ECG bandpass found no R-peaks (probably motion artifact or bad contact) | Re-prep the skin; check the ECG plot for actual QRS complexes |
| `[FUSION] WARN: σ_baseline=0.0 is degenerate` | Sensor was flat for the entire baseline (electrode never made contact) | The fallback σ=1.5 keeps the system usable, but redo the baseline once contact is restored |
| `ConnectionError: Stream Lost — no new data received for 5 seconds` | Bluetooth dropped mid-session | Reconnect device; restart the run. The partial CSV is already saved. |
| Dashboard window doesn't open / crashes immediately | LSL handshake timed out | Make sure `main.py` is actually running before the dashboard — `launcher.py` handles this in the right order |
| Data Quality counters showing high `Out-of-range` count | Possibly bad electrode → noise → values outside physiological bounds → being rejected. This is the system protecting the math. | Improve electrode contact. Counter is informational only. |

---

## 8. After the lab visit

- **Keep:** the `session_*.csv` files in `data/`. They're your audit trail.
- **Optional:** delete `acquisition_log_*` and `processing_log_*` files if disk space matters; they're per-tick diagnostics you usually don't need long-term.
- **Optional:** copy the CSVs to a shared drive named by participant ID for clinical archival.
- **Run `session_review.py`** to generate a one-page visual summary for each participant's file.

---

## 9. If you change recording rate later

The pipeline is rate-agnostic. **No code edit needed** when switching OpenSignals from 1000 Hz to 200 Hz (or vice versa). The new sample rate is read from the LSL stream's `nominal_srate()` and all the R-peak math adapts. You will get cleaner HRV at 1000 Hz; both are valid.

---

## 10. Where to look if something goes wrong

| Layer | File | What it does |
|---|---|---|
| Entry point | [launcher.py](launcher.py) | Spawns the 3 subprocesses |
| Config | [src/config.py](src/config.py) | **All tunable values — start here** |
| Hardware abstraction | [src/data_sources.py](src/data_sources.py) | Mock + Real PLUX adapters (R-peak detection, ADC conversion) |
| LSL consumer | [src/acquisition.py](src/acquisition.py) | Drains stream at 50 Hz, sample validation, disconnect detection |
| Smoothing + baseline | [src/processing.py](src/processing.py) | EMA, 120 s buffer, 3σ cleaning |
| Stress math | [src/fusion.py](src/fusion.py) | Steps 5-10 of the spec, mode kinematics |
| LSL output | [src/output.py](src/output.py) | 18-channel `Biofeedback_State` stream |
| Operator GUI | [src/dashboard.py](src/dashboard.py) | PyQt5 dashboard |
| Audit log | [src/session_manager.py](src/session_manager.py) | Per-session CSV writer |
| Reviewer | [src/session_review.py](src/session_review.py) | Offline replay of any past CSV |

If you find a bug in the lab, the first thing to check is the terminal output: every WARN/ERROR is tagged with the module name in brackets (`[FUSION]`, `[PROCESSOR]`, `[ACQUISITION]`) so you know exactly which file to investigate.

---

**Good luck with the visit. The pipeline math has been audited and verified — anything that goes wrong now is almost certainly at the hardware/contact layer, which is exactly what the Data Quality counters and ECG chart are there to surface.**
