# Concepts and terms, explained

This is the plain-language glossary for the project. 

The terms are grouped roughly in the order they appear as a signal travels through the system: the body and the sensors first, then the numbers we compute, then the plumbing.

## The physiology and the sensors

**EDA (electrodermal activity).** Also called skin conductance. It measures how easily a small electrical current passes across the skin, which changes with sweating. Sweat glands are controlled only by the sympathetic ("fight or flight") branch of the nervous system, so EDA is a clean, direct readout of arousal. It's measured in microsiemens. When someone gets stressed, EDA goes up. We read it from two electrodes on the fingertips.

**ECG (electrocardiogram).** The electrical activity of the heart, measured in millivolts from chest/collarbone electrodes. It looks like a repeating wave with a sharp tall spike on each heartbeat. We don't use the ECG voltage directly for stress; we use it to find the heartbeats, which give us heart rate and heart-rate variability.

**Sympathetic and parasympathetic nervous system.** Two opposing branches of the autonomic nervous system. Sympathetic is the accelerator (stress, arousal, fight-or-flight); parasympathetic is the brake (rest, recovery). Stress is sympathetic going up and parasympathetic going down. EDA tracks the accelerator; HRV tracks the brake. Using both gives a fuller picture than either alone.

## From ECG to heart numbers

**R-peak.** The tall sharp spike in the ECG that happens once per heartbeat. In medical terms it's the R wave of the QRS complex. "Finding the R-peaks" means locating each heartbeat in the voltage trace. Everything heart-related is built on detecting these reliably.

**RR interval.** The time between two consecutive R-peaks, i.e. the time between two heartbeats, measured in milliseconds. If the heart beats once every 800 ms, the RR interval is 800 ms.

**HR (heart rate).** Beats per minute, computed from the RR interval as 60000 divided by the RR interval in milliseconds. An 800 ms interval is 75 BPM. Because it's computed per beat, HR is naturally a "stair-step" signal: it gets a new value each heartbeat (roughly once a second) and holds steady in between. This is normal and expected, not a glitch.

**HRV (heart-rate variability), measured as RMSSD.** Heartbeats are not perfectly evenly spaced; the tiny variations in the gaps carry information. RMSSD (root mean square of successive differences) is one standard way to quantify that. You take the differences between consecutive RR intervals, square them, average, and square-root. A regular, metronome-like heart gives a low RMSSD; a healthy relaxed heart that varies beat-to-beat gives a higher RMSSD. Stress lowers HRV. We compute it over a rolling 10-second window of recent beats, in milliseconds. Typical resting adults sit somewhere in the 20-80 ms range.

Why RMSSD and not the more famous LF/HF ratio? Because RMSSD reflects the parasympathetic brake specifically, it stabilizes within about 10 seconds (the LF/HF ratio needs 20-30 seconds and assumes the signal isn't changing, which is false during VR exposure), and the modern literature considers LF/HF an unreliable stress measure. The math-pipeline document cites the sources for all of this.

## Signal cleaning

**ADC (analog-to-digital converter).** The chip in the PLUX hub that turns a sensor's voltage into a number a computer can store. Ours is 16-bit, meaning each reading is an integer from 0 to 65535. Those integers are "raw ADC counts" and mean nothing physical until you convert them with the sensor's known formula.

**Bandpass filter.** A filter that keeps only a chosen band of frequencies and removes the rest. For R-peak detection we bandpass the ECG to 5-15 Hz, which is where the sharp QRS energy lives. This throws away slow baseline drift (below 5 Hz) and high-frequency noise (above 15 Hz), leaving the heartbeats easy to find.

**EMA (exponential moving average).** A lightweight smoothing filter. Each new smoothed value is a blend of the new raw reading and the previous smoothed value: `new = alpha * raw + (1 - alpha) * previous`. A small alpha leans heavily on the past, so it smooths hard but reacts slowly; a larger alpha reacts faster but smooths less. We use 0.05 for EDA and HRV (slow signals, smooth hard) and 0.10 for HR. It turns the stair-stepped raw signals into continuous-looking traces.

**Zero-order hold.** A fancy name for a simple idea: when no new sample has arrived this instant, reuse the last one. Because HR updates only once per heartbeat but our loop runs 50 times a second, most of the time there's no new heart value, so we hold the previous one until the next beat. The acquisition log marks these ticks as HOLD_LAST.

**Peak prominence.** How much a peak stands out from the dips on either side of it, as opposed to its absolute height. Prominence is robust to a drifting baseline: a peak that rises 0.3 mV above its surroundings has a prominence of 0.3 mV whether the baseline is at zero or has wandered up. We detect R-peaks by prominence rather than raw height for exactly this reason.

**Refractory period.** A minimum enforced gap between detected peaks. After a real heartbeat there's a smaller bump (the T-wave) a few hundred milliseconds later; without a refractory window the detector might count it as a second beat. We require at least 300 ms between peaks, which also caps the maximum detectable rate at 200 BPM.

**3-sigma cleaning (outlier removal).** "Sigma" is the standard deviation, a measure of spread. In a normal distribution, 99.7% of values fall within three standard deviations of the mean. So during the baseline we throw out any sample more than three sigma from the mean and treat it as an artifact (a motion spike or electrode glitch) rather than real physiology. This happens once, at the end of the baseline.

## The baseline

**Baseline (personal baseline).** The first 120 seconds of every session, during which the participant sits still and we just collect data. From it we compute that specific person's resting average for EDA, HR, and HRV. Everything afterward is measured relative to these personal numbers, not to population averages, because "high stress" for a calm person and an anxious person look completely different in absolute terms. The baseline is recomputed fresh every session.

**sigma_baseline (the noise floor).** Even at rest, the stress index doesn't sit perfectly still; it jitters a little. sigma_baseline is how much it jitters at rest, computed by running the resting data through the full stress calculation and taking the standard deviation of the result. It's the reference for deciding what counts as a "real" stress response versus normal noise. It's frozen at the end of the baseline and never changes during the session, because if it adapted, a big stress response would raise the bar and hide itself.

## The stress numbers

**Percentage deviation.** For each signal, how far the current reading is from the personal baseline, as a percent. HR and EDA deviations are positive when above baseline. HRV is inverted (baseline minus current) so that "more stressed" is positive for all three, since stress lowers HRV.

**S_instant (instantaneous stress).** The three percentage deviations combined into one number with weights: half the weight on EDA, a third on HRV, a fifth on HR. EDA gets the most because it's the most specifically tied to stress arousal; HR gets the least because it's influenced by lots of unrelated things like posture and breathing. It's computed every tick and is noisy.

**S_t (the stress index).** S_instant smoothed over the last second (50 samples). This is the canonical stress number that everything downstream uses. Zero means at baseline; positive means more aroused than baseline; the bigger, the more aroused. Small negatives (slightly calmer than baseline) are possible and harmless.

**Thresholds (mild and high).** Two cutoffs derived from sigma_baseline: mild is 1.33 times it, high is 2.28 times it. Those multipliers correspond to roughly the 90th and 99th percentiles of a normal distribution. They split S_t into the three states and are frozen for the whole session.

**State (calm, stressed, ultra_stressed).** S_t bucketed by the thresholds. At or below mild is calm. Between mild and high is stressed, which is actually the therapeutic target zone, not a problem. Above high is ultra_stressed, the signal to back off. The state is what drives the balloon.

**Dashboard score (0-100).** A cosmetic remapping of S_t for quick reading by the operator: 0 at baseline, about 50 at the entry to the stressed zone, 100 at ultra. It's display-only and does not drive the balloon.

## The therapy output

**y_t (balloon altitude).** The target height of the VR balloon, in meters. It starts at the middle of the session's range and moves a little each tick based on state: up when the patient is calm (more height exposure), held when they're in the stressed target zone, down when they're ultra-stressed (relief). The patient can't move it directly; only their physiology can. That's what makes it biofeedback rather than a game.

**Session mode (easy, moderate, intense).** Three preset altitude ranges: easy is 20-30 m, moderate 30-45 m, intense 45-65 m. The movement rates scale with the range so the balloon feels equally responsive in each mode, and the time to traverse the full range under a sustained state is the same across modes (about 200 seconds up, 100 seconds down). Higher modes are used as the participant builds tolerance across sessions.

## The plumbing

**LSL (Lab Streaming Layer).** A small open-source networking protocol for streaming time-stamped signal data between programs on a network. The PLUX software publishes the raw signals over LSL; our pipeline publishes its results over LSL; the dashboard and (eventually) the Unity scene subscribe to those. It handles timestamps and ordering for us, so we just read values.

**Stream (inlet and outlet).** An outlet is a program publishing data on LSL; an inlet is a program subscribing to it. Our system has two named streams: "OpenSignals" (the raw signals coming in) and "Biofeedback_State" (our 18-channel results going out).

**Pipeline rate (50 Hz).** The core loop runs 50 times per second, once every 20 ms. The device may sample faster (200 or 1000 Hz), but the pipeline downsamples to 50 Hz, which is plenty for physiological signals that change over seconds.

## If you only need the one-liners

EDA is skin sweat (stress up). ECG is the heart's electrical trace, used to find heartbeats. An R-peak is one heartbeat. HR is beats per minute; HRV (RMSSD) is the beat-to-beat variation, which drops under stress. EMA smooths the signals. The baseline is the person's first two resting minutes. S_t is the single stress number built from how far the signals have moved from that baseline. The thresholds turn S_t into calm/stressed/ultra. y_t is the balloon height that the state moves. LSL is how the programs talk to each other.
