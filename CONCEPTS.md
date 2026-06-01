# Concepts and terms, explained

A plain-language glossary for the project.

The terms are grouped roughly in the order they appear as a signal travels through the system: the body and the sensors first, then the numbers computed from them, then the plumbing.

## The physiology and the sensors

**EDA (electrodermal activity).** Also called skin conductance. Measures how easily a small electrical current passes across the skin, which changes with sweating. Sweat glands are controlled only by the sympathetic ("fight or flight") branch of the nervous system, so EDA is a clean, direct readout of arousal. Measured in microsiemens. When someone gets stressed, EDA goes up. Read from two electrodes on the fingertips.

**ECG (electrocardiogram).** The electrical activity of the heart, measured in millivolts from chest / collarbone electrodes. Looks like a repeating wave with a sharp tall spike on each heartbeat. The ECG voltage is not used directly for stress; it is used to find the heartbeats, which give heart rate and heart-rate variability.

**Sympathetic and parasympathetic nervous system.** Two opposing branches of the autonomic nervous system. Sympathetic is the accelerator (stress, arousal, fight-or-flight); parasympathetic is the brake (rest, recovery). Stress is sympathetic going up and parasympathetic going down. EDA tracks the accelerator; HRV tracks the brake. Using both gives a fuller picture than either alone.

## From ECG to heart numbers

**R-peak.** The tall sharp spike in the ECG that happens once per heartbeat. In medical terms it is the R wave of the QRS complex. "Finding the R-peaks" means locating each heartbeat in the voltage trace. Everything heart-related is built on detecting these reliably.

**RR interval.** The time between two consecutive R-peaks, i.e. the time between two heartbeats, measured in milliseconds. An 800 ms interval means the heart beats once every 800 ms.

**HR (heart rate).** Beats per minute, computed from the RR interval as 60000 divided by the RR interval in milliseconds. An 800 ms interval is 75 BPM. Because it is computed per beat, HR is naturally a "stair-step" signal: it gets a new value each heartbeat (roughly once a second) and holds steady in between. This is normal and expected, not a glitch.

**HRV (heart-rate variability), measured as RMSSD.** Heartbeats are not perfectly evenly spaced; the tiny variations in the gaps carry information. RMSSD (root mean square of successive differences) is one standard way to quantify that. Take the differences between consecutive RR intervals, square them, average, and square-root. A regular, metronome-like heart gives a low RMSSD; a healthy relaxed heart that varies beat-to-beat gives a higher RMSSD. Stress lowers HRV. Computed over a rolling `Config.RMSSD_WINDOW_SEC` window of recent beats, in milliseconds. Typical resting adults sit somewhere in the 20-80 ms range.

The window length is a clinical trade-off. The Task Force 1996 / European Society of Cardiology standard is 5 minutes (300 s) for "short-term" HRV, which is the most stable estimate but too laggy for real-time biofeedback. The ultra-short-term literature (Shaffer & Ginsberg 2017, Munoz et al. 2015) treats 60 s as the practical minimum. The pipeline's default is 60 s, which gives a per-tick RMSSD with roughly 12 ms standard deviation, against the ~56 ms standard deviation that comes out of a 10 s window. Both knobs (`RMSSD_WINDOW_SEC` for the adaptive path and `DS2_HRV_WINDOW_SEC` for the NeuroKit path) live in `src/config.py`.

Why RMSSD and not the more famous LF/HF ratio? Because RMSSD reflects the parasympathetic brake specifically, it stabilises within about 10 seconds (the LF/HF ratio needs 20-30 seconds and assumes the signal is stationary, which is false during VR exposure), and the modern literature considers LF/HF an unreliable stress measure. The math-pipeline document cites the sources.

## Signal cleaning

**ADC (analog-to-digital converter).** The chip in the PLUX hub that turns a sensor's voltage into a number. PLUX uses a 16-bit ADC, so each reading is an integer from 0 to 65535. Those integers are "raw ADC counts" and mean nothing physical until converted with the sensor's known formula.

**Bandpass filter.** A filter that keeps only a chosen band of frequencies and removes the rest. For R-peak detection the ECG is bandpassed to 5-15 Hz, which is where the sharp QRS energy lives. This throws away slow baseline drift (below 5 Hz) and high-frequency noise (above 15 Hz), leaving the heartbeats easy to find.

**EMA (exponential moving average).** A lightweight smoothing filter. Each new smoothed value is a blend of the new raw reading and the previous smoothed value: `new = alpha * raw + (1 - alpha) * previous`. A small alpha leans heavily on the past (smooths hard, reacts slowly); a larger alpha reacts faster but smooths less. The pipeline uses 0.05 for EDA and HRV (slow signals, smooth hard) and 0.10 for HR. It turns the stair-stepped raw signals into continuous-looking traces.

**Zero-order hold.** A fancy name for a simple idea: when no new sample has arrived this instant, reuse the last one. Because HR updates only once per heartbeat but the loop runs 50 times a second, most of the time there is no new heart value, so the previous one is held until the next beat. The acquisition log marks these ticks as `HOLD_LAST`.

**Peak prominence.** How much a peak stands out from the dips on either side of it, as opposed to its absolute height. Prominence is robust to a drifting baseline: a peak that rises 0.3 mV above its surroundings has a prominence of 0.3 mV whether the baseline is at zero or has wandered up. The fallback R-peak detector uses prominence rather than raw height for exactly this reason.

**Refractory period.** A minimum enforced gap between detected peaks. After a real heartbeat there is a smaller bump (the T-wave) a few hundred milliseconds later; without a refractory window the detector might count it as a second beat. The pipeline requires at least 300 ms between peaks, which also caps the maximum detectable rate at 200 BPM.

**3-sigma cleaning (outlier removal).** Sigma is the standard deviation, a measure of spread. In a normal distribution, 99.7% of values fall within three standard deviations of the mean. During the baseline, any sample more than three sigma from the mean is thrown out and treated as an artifact (a motion spike or electrode glitch) rather than real physiology. This happens once, at the end of the baseline.

## The baseline

**Baseline (personal baseline).** The first 120 seconds of every session, during which the participant sits still and data is just collected. From it the pipeline computes that specific person's resting average for EDA, HR, and HRV. Everything afterward is measured relative to these personal numbers, not to population averages, because "high stress" for a calm person and an anxious person look completely different in absolute terms. The baseline is recomputed fresh every session.

**sigma_baseline (the noise floor).** Even at rest, the stress index does not sit perfectly still; it jitters a little. sigma_baseline is how much it jitters at rest, computed by running the resting data through the full stress calculation and taking the standard deviation of the result. It is the reference for deciding what counts as a real stress response versus normal noise. Frozen at the end of the baseline and never changes during the session: if it adapted, a big stress response would raise the bar and hide itself.

## The stress numbers

**Percentage deviation.** For each signal, how far the current reading is from the personal baseline, as a percent. HR and EDA deviations are positive when above baseline. HRV is inverted (baseline minus current) so that "more stressed" is positive for all three, since stress lowers HRV.

**delta_EDA.** The percentage deviation for EDA, computed as `(EDA_now - avg_EDA) / avg_EDA * 100`. Positive when the patient is sweating more than at rest. This is one of the three inputs to S_t (with the heaviest weight, 0.5). Shown live on the deltas chart in green, recorded in the session CSV as `delta_eda`, and on the LSL stream as channel 6.

**delta_HR.** The percentage deviation for heart rate, `(HR_now - avg_HR) / avg_HR * 100`. Positive when the heart is beating faster than at rest. The smallest contributor to S_t (weight 0.2) because heart rate moves for lots of reasons besides stress. Shown live on the deltas chart in orange, in the CSV as `delta_hr`, on LSL as channel 7.

**delta_HRV.** The percentage deviation for HRV, inverted: `(avg_HRV - HRV_now) / avg_HRV * 100`. Positive when HRV has dropped below baseline (which means more stress, because lower HRV signals parasympathetic withdrawal). The middle contributor to S_t (weight 0.3). Shown live on the deltas chart in blue, in the CSV as `delta_hrv`, on LSL as channel 8.

These three deltas are the components of S_t: the stress index is just their weighted sum. The deltas chart on the dashboard makes that decomposition visible: when S_t jumps, the operator can immediately see whether the jump came mostly from EDA spiking, HR rising, HRV dropping, or some combination. That is diagnostic information not available from S_t alone.

**S_instant (instantaneous stress).** The three percentage deviations combined into one number with weights: half the weight on EDA, a third on HRV, a fifth on HR. EDA gets the most because it is the most specifically tied to stress arousal; HR gets the least because it is influenced by lots of unrelated things like posture and breathing. Computed every tick and is noisy.

**S_t (the stress index).** S_instant smoothed over the last second (50 samples). This is the canonical stress number that everything downstream uses. Zero means at baseline; positive means more aroused than baseline; the bigger, the more aroused. Small negatives (slightly calmer than baseline) are possible and harmless.

**Thresholds (mild and high).** Two cutoffs derived from sigma_baseline: mild is 1.33 times it, high is 2.28 times it. Those multipliers correspond to roughly the 90th and 99th percentiles of a normal distribution. They split S_t into the three states and are frozen for the whole session.

**State (calm, stressed, ultra_stressed).** S_t bucketed by the thresholds. At or below mild is calm. Between mild and high is stressed, which is actually the therapeutic target zone, not a problem. Above high is ultra_stressed, the signal to back off. The state is what gets sent to Unity; Unity decides what to do with it (in the acrophobia scene, it moves the balloon).

**Dashboard score (0-100).** A cosmetic remapping of S_t for quick reading by the operator: 0 at baseline, about 50 at the entry to the stressed zone, 100 at ultra. Display-only; it does not drive the VR scene.

## The therapy output

The Python side does not compute altitude or any other VR-specific control value. It publishes the stress state on LSL (for the dashboard and audit log) and sends `increase` / `decrease` / nothing commands to Unity over UDP. Unity owns the altitude math, the altitude range, and the difficulty scaling. The reason for the split: the same Python pipeline can drive any other exposure paradigm (spider phobia, social anxiety) without changes, because the VR side owns the imagery and the kinematics.

## The plumbing

**LSL (Lab Streaming Layer).** A small open-source networking protocol for streaming time-stamped signal data between programs on a network. The PLUX software publishes the raw signals over LSL; the pipeline publishes its results over LSL for the dashboard and audit log. It handles timestamps and ordering, so the consumer just reads values.

**UDP bridge.** A second output channel, separate from LSL, dedicated to Unity. A plain UDP socket sending one of four short text strings: `start`, `stop`, `increase`, `decrease`. Unity's `BioFeedbackMiddleware` listens on the configured port (5005 by default) and uses the commands to move the balloon. Why a second channel? LSL is for high-rate, multi-channel signal data; UDP is for low-rate, fire-and-forget decisions. Unity needs the latter, and the existing Unity script was already written for UDP, so this matches the contract without any Unity-side change.

**Command throttle.** The minimum gap between two consecutive `increase` or `decrease` packets sent to Unity. Without it, the 50 Hz pipeline would send 50 commands per second and Unity would step the balloon by 50 m per second: physically impossible and visually jarring. With a one-second throttle (the default), sustained calm produces a steady 1 m/s rise, which Unity's per-frame lerp smooths into continuous motion. `start` and `stop` bypass the throttle.

**UDP audit log.** Every packet actually sent to Unity is also written to `data/unity_udp_log_<timestamp>_<patient>.csv` with timestamp, kind (lifecycle vs state), command, current state, current S_t, and the gate-open flag. This is the ground truth for verifying that the throttle is working and for correlating Unity's behaviour with the stress trace offline.

**SessionState.** The five possible states the pipeline can be in: `IDLE` (waiting for the operator to start baseline), `BASELINE` (the 120-second calibration buffer is filling), `BASELINE_DONE` (calibration finished, thresholds locked, waiting for the operator to start the live session), `LIVE` (full pipeline active, commands flowing to Unity), `STOPPED` (live session ended, waiting for restart, another live run, or close). The state is not decided by the pipeline; the operator drives transitions via dashboard buttons. The current state is exposed on LSL channel 20 so the dashboard knows which buttons to enable.

**Command.** The operator commands the dashboard can send back to the pipeline: `BASELINE_START`, `BASELINE_STOP`, `BASELINE_RESET`, `LIVE_START`, `LIVE_STOP`, `LIVE_RESTART`, plus `SHUTDOWN_SAVE_BOTH` / `SHUTDOWN_DISCARD_LIVE` / `SHUTDOWN_DISCARD_BOTH` (sent by the close-window prompt), plus an internal `NONE`. They travel on a dedicated LSL stream named `Biofeedback_Control`, which the dashboard publishes and the main pipeline consumes. Each button click sends one command.

**Control bus.** The `Biofeedback_Control` LSL stream. One channel, integer values from the Command enum. Carries operator intent from the dashboard process to the main pipeline process. The other direction (pipeline -> dashboard) is the `Biofeedback_State` stream with 24 channels. Together those two streams plus the UDP feed to Unity are the full IPC surface of the system.

**Patient intake form.** A modal PyQt5 dialog that runs before the dashboard. Collects first name, last name, patient ID, gender (M / F dropdown, no free text), and session number (1 to `Config.MAX_SESSION_NUMBER`). The session date is fixed to today and not editable. Validates each field inline and refuses to submit until everything is correct. The result is written to `data/patient_<id>_<date>_s<n>.json` and passed to the subprocesses via an environment variable.

**Baseline capture file.** `data/baseline_<timestamp>_<patient_id>.json`, written once per session at the moment the 120-second baseline finishes. Contains the patient demographics, the personal averages, sigma_baseline, the locked thresholds, the bonus HRV metrics (RMSSD, SDNN, pNN50, etc. from NeuroKit2), the per-signal artifact counts, the source recording, and the pipeline constants in force. The canonical record of "what calibrated this session" and can be cited in post-hoc analysis.

**Live transcript.** `data/live_log_<timestamp>_<patient>.txt`, written one line per second during the LIVE state. Each line carries a wall-clock timestamp and a paste-friendly summary of the smoothed signals, percentage deltas, S_t, and state. Convenient for terminal review during a session and for pasting into clinical notes afterwards.

**Wait-for-calm gate.** After the live phase starts, the UDP bridge holds every `increase` and `decrease` command until the system observes a genuine `calm` state. Two reasons: the patient may still be mid-adjustment when the session begins (putting on the headset, settling in), so the first stress readings are often noisy; and the fusion engine returns a synthetic "calm" during its 1-second buffer-warmup period that should not count as a real reading. The bridge ignores everything for the first `UDP_GATE_WARMUP_SEC` (default 1.5 s), then watches for the first real calm. Once that is seen, the gate opens for the rest of the session and the normal throttled command flow takes over. The dashboard shows the gate state in the live panel (orange while waiting, gray once active), and the same state is on LSL channel 19 as `udp_gate_open`.

**Stream (inlet and outlet).** An outlet is a program publishing data on LSL; an inlet is a program subscribing to it. The system has three named streams: `OpenSignals` (the raw signals coming in), `OpenSignals_ECG` (a side stream carrying just the raw ECG voltage for the dashboard's waveform chart), and `Biofeedback_State` (the 24-channel results going out). Plus `Biofeedback_Control` for operator commands.

**Pipeline rate (50 Hz).** The core loop runs 50 times per second, once every 20 ms. The device may sample faster (200 or 1000 Hz), but the pipeline downsamples to 50 Hz, which is plenty for physiological signals that change over seconds.

## If you only need the one-liners

EDA is skin sweat (stress up). ECG is the heart's electrical trace, used to find heartbeats. An R-peak is one heartbeat. HR is beats per minute; HRV (RMSSD) is the beat-to-beat variation, which drops under stress. EMA smooths the signals. The baseline is the person's first two resting minutes. S_t is the single stress number built from how far the signals have moved from that baseline. The thresholds turn S_t into calm/stressed/ultra. That state goes to Unity, which decides how to move the balloon. LSL is how the programs talk to each other; UDP is how commands go to Unity.
