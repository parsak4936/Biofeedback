# Lab Session Checklist

Print this and bring it to the lab. Hand to the operator if someone else
is running the setup.

---

## Equipment required

- PLUX biosignalsplux hub (charged, Bluetooth paired with PC)
- EDA sensor cable (**red sleeve**) + 2 EDA finger pad stickers
- ECG sensor cable (**dark blue sleeve**) + 3 ECG chest stickers
- Lab PC with OpenSignals (r)evolution installed
- This repository cloned, `env\` virtualenv set up
- Participant (skin clean and dry where electrodes will be placed)

---

## Step 1 — Participant hookup

### EDA finger pads (red sleeve)

1. Non-dominant hand. Index finger + middle finger.
2. Volar (inside) surface, middle phalange.
3. Press each sticker firmly for 5 seconds.
4. **The two stickers MUST NOT touch each other** — no metal-to-metal
   contact between the snap buttons. Keep fingers spread.
5. Wait at least 30 seconds for skin contact to stabilise before
   starting baseline. Dry skin = slow EDA rise.

### ECG stickers (dark blue sleeve, 3 leads)

| Lead | Color | Placement |
|---|---|---|
| RA | Red    | Right collarbone, outer end |
| LA | Yellow | Left collarbone, outer end |
| REF| Black  | Right hip OR lower-right ribs (ground) |

1. Clean skin with alcohol wipe if available.
2. Press each sticker firmly for 5 seconds.
3. Snap cable clips onto stickers AFTER they are on the skin (less
   strain on the gel).

---

## Step 2 — Hub connection

1. Power on the PLUX hub. Wait for the LED to be solid (not blinking
   slowly — that means searching for Bluetooth).
2. **EDA red cable → CH1 port on the hub**.
3. **ECG blue cable → CH2 port on the hub**.
4. Cables click in firmly. Gentle tug to confirm seating.

If the hub LED never goes solid: pair via Windows Bluetooth settings
first, then retry.

---

## Step 3 — OpenSignals configuration (in this exact order)

1. Open OpenSignals (r)evolution.
2. **Devices panel**: the paired hub should appear. Click to select.
3. **Sensor configuration panel** — for each channel:
   - **CH1 → sensor type: EDA**
   - **CH2 → sensor type: ECG**

   If you do not see this panel:
   - Right-click on the CH1 box; look for "Properties" or "Sensor Type"
   - OR View menu → Show All Panels
   - OR Settings → Channels

   **Do not proceed until both channels show the correct sensor type.**

4. **Sampling rate: 200 Hz**. Resolution: 16-bit.
5. **Preferences → Integrations → Lab Streaming Layer → enabled**.
   Restart OpenSignals after toggling this setting.
6. Click the **RED RECORD BUTTON**.
   LSL only publishes while OpenSignals is actively recording.

---

## Step 4 — Visual verification (do NOT skip)

Inside OpenSignals's own waveform display:

1. **EDA channel**: have the participant squeeze the finger clips
   together (or just touch the pads firmly). The EDA trace should
   show a clear deflection.
2. **ECG channel**: should show a repeating waveform with visible
   R-peaks (sharp upward spikes at roughly 60–80 per minute at rest).

If EDA does not deflect → re-seat the finger pads, check the cable
is in CH1 (red cable into CH1 port). Wait another 30 seconds.

If ECG does not show heartbeats → check the three stickers are firmly
on bone landmarks, check the cable is in CH2.

---

## Step 5 — Verify LSL is actually publishing real data

In a CMD window:

```cmd
cd <path to project>
env\Scripts\activate.bat
python -c "from pylsl import resolve_streams, StreamInlet; import numpy as np; s=resolve_streams(wait_time=2.0)[0]; inl=StreamInlet(s); import time; time.sleep(2); d,_=inl.pull_chunk(timeout=2.0); a=np.array(d); print('shape:', a.shape); print('std per channel:', a.std(0))"
```

Expected output:

```
shape: (~400, 3)
std per channel: [<big>, <non-zero>, <non-zero>]
```

- Channel 0 = NSEQ counter: std around hundreds, that's the counter
  incrementing
- Channel 1 = EDA: std should be **> 1** (real signal)
- Channel 2 = ECG: std should be **> 100** (real signal, varies a lot)

**If std on channel 1 or 2 is 0 or near zero** → OpenSignals is publishing
a placeholder. Do NOT run the session. Go back to Step 3 and check the
sensor-type panel.

---

## Step 6 — Launch the pipeline

```cmd
run.bat
```

1. Fill in the patient intake form. **Gender must be selected** (the
   form will refuse to start otherwise).
2. Dashboard opens. **In the top "Live Signals" row** verify:
   - EDA: shows a non-zero value matching what OpenSignals shows
   - ECG: shows a varying voltage
3. Click **Start Baseline**. Sit still, breathe normally for 120
   seconds. Do not start VR yet.
4. After baseline completes, the **Personal Baseline cards** show
   captured EDA / HR / HRV values. Sanity check:
   - EDA: typically 1–20 µS at rest
   - HR: typically 60–90 BPM at rest
   - HRV (RMSSD): typically 20–80 ms at rest
5. Click **Start Live Session** to begin VR exposure.

---

## Step 7 — During the session

- **Watch the Stress Index chart** — should show CALM band most of the
  time at start; should rise when balloon altitude increases.
- **Watch the Balloon Height chart** — should match what's happening
  in Unity.
- Note any **disconnect warnings** in the console — re-seat the
  affected electrode.

---

## Step 8 — End of session

1. Click **Stop** in the Live Session panel.
2. Close the dashboard window. Save when prompted.
3. Session data is in `data/<participant>_Session<n>_<date>_<gender>/`.
4. Stop recording in OpenSignals (red button again).

---

## Quick troubleshooting reference

| Symptom | First thing to check |
|---|---|
| Dashboard hangs on "Scanning for stream" | OpenSignals not recording, or LSL not enabled |
| EDA card reads 0 or near zero | Re-seat finger pads, check CH1 sensor type = EDA |
| ECG chart frozen at -1.473 mV | LSL bridge publishing dead channel; OpenSignals config issue (Step 3) |
| HR shows `--` after 10 seconds | ECG sensor not detecting beats; check chest stickers |
| HRV stays `--` | Normal for first 60 seconds — wait |
| Bluetooth dropout warning | Move hub closer to PC; check battery |
| Module error / Python crash | Run `pip install -r requirements.txt` again |

---

## If OpenSignals refuses to publish real EDA after all checks

Plan B exists: a direct PLUX Python API path that bypasses OpenSignals
entirely (talks to the hub over Bluetooth directly). Contact the
software side to deploy it. Until then, **abort the session** — do not
record garbage data.

---

## Sampling rate / channel reference card

| Setting | Value |
|---|---|
| Sampling rate | **200 Hz** |
| Resolution | 16-bit |
| CH1 (red sleeve) | **EDA** sensor |
| CH2 (dark blue sleeve) | **ECG** sensor |
| Pipeline processing rate | 10 Hz |
| Unity VR command rate | 1 Hz (max) |
| Baseline duration | 120 seconds |
