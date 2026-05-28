# What the device gives us, and what we do with it

These are study notes for explaining the input side to anyone who asks. They cover what the PLUX hub actually sends, what the file on disk looks like, how the LSL network stream differs from the file, and how we get from raw analog-to-digital counts to the physiological values the rest of the pipeline cares about.

## The two outputs of OpenSignals

OpenSignals is the desktop software that talks to the PLUX hub over Bluetooth. When it's recording, it produces the same content in two different packages:

A `.txt` file on disk. Useful for offline analysis, sharing with collaborators, or sanity-checking what was captured.

A live LSL network stream. This is what our Python pipeline subscribes to during a session for real-time processing.

The two contain the same sensor values; the file just adds a couple of bookkeeping columns and a metadata header on top.

## The .txt file

A real example, the first few lines of `data/opensignals_2026-05-25_14-57-56.txt`:

```
# OpenSignals Text File Format. Version 1
# {"00:07:80:0F:31:9C": {"sampling rate": 200, ...full JSON metadata...}}
# EndOfHeader
0    0    32948    15163
1    0    33060    15198
2    0    32996    15216
```

The first three lines all start with `#` and form the header. After `EndOfHeader`, every line is one sample, tab-separated.

The second line is the interesting one. It's a single line of JSON that describes everything about the recording. The fields that matter to us:

- `sampling rate` — how many samples per second per channel. In this file, 200. PLUX hardware also supports 1000.
- `resolution` — the bit depth of the analog-to-digital converter, in this case 16 bits per channel. That means each sensor value is an integer between 0 and 65535.
- `sensor` — the order matters. `["ECG", "EDA"]` means channel 1 is ECG and channel 2 is EDA. In our older mock file from 2026-05-13 this is reversed, which is why we parse the header instead of hardcoding the column order.
- `column` — describes the columns of the data rows. Always `["nSeq", "DI", "CH1", "CH2"]` for a 2-sensor recording.
- `convertedValues` — when this is 0, the file holds raw ADC integers and we have to do the unit conversion ourselves. When it's 1, OpenSignals has already converted to physical units. Ours is always 0.

Other JSON fields (`firmware version`, `date`, `time`, `digital IO`, `sleeve color`, the SpO2 calibration array) are present but we don't use them.

## What each data row means

A row looks like `1    0    33060    15198`.

The four numbers are nSeq, DI, CH1, CH2.

`nSeq` is a monotonic sample counter: 0, 1, 2, 3... It increments by 1 every sample. If you see it skip — say it jumps from 1000 to 1003 — that means three samples were dropped, usually because of a Bluetooth hiccup.

`DI` is the state of a digital input pin on the hub. You can wire it to a footswitch or external event marker. We don't use it; it's always 0 in our recordings.

`CH1` and `CH2` are the raw 16-bit ADC counts for the two sensor channels. For this recording, CH1 is ECG and CH2 is EDA (the header told us). The integers themselves don't mean anything physical yet — they're just where the voltage on each electrode landed inside the converter's 0-to-65535 range.

## Turning ADC counts into meaningful units

The PLUX datasheet provides a transfer function for each sensor. The general shape is "fraction of the ADC range times the reference voltage divided by a sensor-specific constant", with a recentering step for bipolar signals.

For EDA the formula is:

```
EDA (in microsiemens) = (ADC / 65536) * 3.0 / 0.132
```

The 3.0 is the reference voltage the ADC measures against. Dividing the ADC count by 65536 gives a fraction of the full input range. The 0.132 is the EDA sensor's transducer constant, taken straight from the PLUX datasheet.

For ECG the formula has an extra step because ECG is a bipolar signal — it swings both positive and negative around a neutral baseline:

```
ECG (in millivolts) = ((ADC / 65536) - 0.5) * 3.0 / 1100 * 1000
```

The `- 0.5` recenters the fraction so the baseline sits at zero and the QRS spikes come out as positive numbers. The 1100 is the gain of the ECG sensor's instrumentation amplifier. The trailing `* 1000` converts volts to millivolts so the resulting numbers are easier to read.

Plugging the example row's numbers in:

EDA at sample 1: `(15198 / 65536) * 3.0 / 0.132` is about 5.27 microsiemens. That's a normal resting value.

ECG at sample 1: `((33060 / 65536) - 0.5) * 3.0 / 1100 * 1000` is about 0.012 millivolts. Close to the baseline, not at a heartbeat.

Across the whole recording the values look like:

- EDA ranges from about 3.8 to 9.7 microsiemens (normal sympathetic activity range)
- ECG swings between roughly -0.67 and +0.66 millivolts, which is the typical QRS amplitude window

If you ever see EDA outside roughly 0 to 50 microsiemens or ECG values much beyond 1 millivolt, something is wrong with the electrode contact.

## The LSL stream version

The LSL stream OpenSignals broadcasts is a network pipe carrying the same sample content, but stripped down. There's no `nSeq` and no `DI` — just the sensor values, one tuple per sample, going out at the configured sample rate.

The settings on the stream:

- Name: usually `OpenSignals`, configurable in OpenSignals preferences
- Type: usually the device MAC, like `00:07:80:0F:31:9C`
- Channel count: matches the number of active sensors. For our 2-sensor ECG+EDA setup, it's 2.
- Sample rate: matches whatever `sampling rate` is set to in the recording configuration.
- Channel format: 32-bit floats (the raw ADC integers cast to float for transport).

A single LSL sample, if you printed it, looks like:

```
([33060.0, 15198.0], 12345.6789)
```

The first part is the channel values. The order matches the `sensor` field in the file's JSON header, so for this recording the first value is ECG ADC and the second is EDA ADC. The second part is the LSL timestamp in seconds since the LSL clock started — useful for time-aligning multiple streams.

## How HR and HRV come out of the ECG voltage

The PLUX hub does not compute heart rate or heart rate variability. It only delivers the voltage trace. To get HR we need to find the R-peaks in the ECG — the tall positive spikes that happen once per heartbeat — and measure the time between consecutive ones. That time is called the RR interval.

The heart rate per beat is then:

```
HR (BPM) = 60000 / RR_interval_in_ms
```

So if two consecutive R-peaks are 800 ms apart, the instantaneous heart rate is 75 BPM. The next pair gives the next value, and the HR series steps along once per heartbeat, holding the previous value in between.

HRV in our system is measured as RMSSD (root mean square of successive differences). It's computed from the last 10 seconds of RR intervals:

```
RMSSD = sqrt( mean( (RR_n+1 - RR_n)^2 ) )
```

If the RR intervals are perfectly regular (which would be physiologically weird), RMSSD is zero. If they vary beat to beat, RMSSD is larger. Healthy adults at rest tend to sit in the 20 to 80 ms range. Acute stress lowers HRV; relaxation raises it.

Finding the R-peaks reliably is the tricky part, because real ECG is noisy and the peak amplitude differs from person to person and recording to recording. A fixed amplitude threshold breaks the moment a recording is noisier than expected — which is exactly what happened the first time we ran a longer, movement-heavy recording. So the detector is adaptive: it bandpasses the ECG to the 5-15 Hz QRS band, detects on peak prominence (how far a peak stands out from its local surroundings, which ignores slow baseline drift) rather than raw height, and uses a two-pass approach where it first gathers candidate peaks, measures the typical R-peak size in that specific signal, and then keeps peaks at a fraction of that size. A 300 ms refractory window stops the T-wave that follows each beat from being counted as a second heartbeat. The upshot is that the same code handles a clean short clip and a noisy 14-minute recording without any per-file tuning.

For the offline mock data this runs once over the whole recording at load time. For the live PLUX device the same logic runs incrementally on a rolling 5-second buffer of incoming ECG samples. Either way the math is identical.

## EDA needs no derivation

EDA is the simplest of the three signals to deal with. Once you've done the ADC-to-microsiemens conversion, that's already the value the rest of the pipeline uses. Skin conductance changes slowly (over seconds, not milliseconds), so the high sample rate of the device is just oversampling. The pipeline's EMA smoothing handles whatever residual noise is left.

## Putting it all together

The Python pipeline subscribes to the LSL stream, applies the ADC-to-units conversions described above, derives HR and HRV from the ECG voltage trace, and from that point onward operates on three continuous time series: EDA in microsiemens, HR in BPM, and HRV (as RMSSD) in milliseconds. The rest of the math, including the 120-second baseline, the percentage-deviation fusion, and the stress state classification, runs on those three numbers. That part is covered in `DATA_FLOW.md`.

## A short version, for when someone asks

If someone asks you "what is the device actually measuring?", a way to phrase it:

The PLUX hub samples the voltage on each electrode 200 times per second. Each voltage reading is stored as a 16-bit integer — a raw count from the analog-to-digital converter, between 0 and 65535. PLUX provides transfer formulas to turn those counts into physical units: microsiemens for the EDA electrodes, millivolts for the ECG leads. From the ECG voltage trace we find heartbeats — each R-peak is a tall spike — and the time between consecutive heartbeats gives us heart rate. The variability in those inter-beat times gives us HRV. So the device itself just delivers voltage; everything physiologically meaningful is computed by our middleware.
