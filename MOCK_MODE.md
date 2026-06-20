# What the device provides, and what the pipeline does with it

Notes on the input side. They cover what the PLUX hub actually sends, what the file on disk looks like, how the LSL network stream differs from the file, and how raw analog-to-digital counts become the physiological values the rest of the pipeline cares about.

## The two outputs of OpenSignals

OpenSignals is the desktop software that talks to the PLUX hub over Bluetooth. When recording, it produces the same content in two different packages:

A `.txt` file on disk. Useful for offline analysis, sharing with collaborators, or sanity-checking what was captured.

A live LSL network stream. The Python pipeline subscribes to this during a session for real-time processing.

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

The second line is the interesting one. A single line of JSON describing everything about the recording. The fields that matter:

- `sampling rate`: samples per second per channel. In this file, 200. PLUX hardware also supports 1000.
- `resolution`: the bit depth of the analog-to-digital converter, in this case 16 bits per channel. That means each sensor value is an integer between 0 and 65535.
- `sensor`: the order matters. `["ECG", "EDA"]` means channel 1 is ECG and channel 2 is EDA. In the older mock file from 2026-05-13 this is reversed, which is why the code parses the header instead of hardcoding the column order.
- `column`: describes the columns of the data rows. Usually `["nSeq", "DI", "CH1", "CH2"]` for a 2-sensor recording.
- `convertedValues`: when 0, the file holds raw ADC integers and the unit conversion must be done downstream. When 1, OpenSignals has already converted to physical units. Recordings in this repo are always 0.

Other JSON fields (`firmware version`, `date`, `time`, `digital IO`, `sleeve color`, the SpO2 calibration array) are present but unused.

## What each data row means

A row looks like `1    0    33060    15198`.

The four numbers are nSeq, DI, CH1, CH2.

`nSeq` is a monotonic sample counter: 0, 1, 2, 3... It increments by 1 every sample. A jump (say from 1000 to 1003) means three samples were dropped, usually because of a Bluetooth hiccup.

`DI` is the state of a digital input pin on the hub. It can be wired to a footswitch or external event marker. The pipeline does not use it; it is always 0 in these recordings.

`CH1` and `CH2` are the raw 16-bit ADC counts for the two sensor channels. For this recording, CH1 is ECG and CH2 is EDA (the header confirms). The integers themselves do not mean anything physical yet: they are just where the voltage on each electrode landed inside the converter's 0-to-65535 range.

## Turning ADC counts into meaningful units

The PLUX datasheet provides a transfer function for each sensor. The general shape is "fraction of the ADC range times the reference voltage divided by a sensor-specific constant", with a recentering step for bipolar signals.

For EDA the formula is:

```
EDA (in microsiemens) = (ADC / 65536) * 3.0 / 0.132
```

The 3.0 is the reference voltage the ADC measures against. Dividing the ADC count by 65536 gives a fraction of the full input range. The 0.132 is the EDA sensor's transducer constant, taken straight from the PLUX datasheet.

For ECG the formula has an extra step because ECG is a bipolar signal that swings both positive and negative around a neutral baseline:

```
ECG (in millivolts) = ((ADC / 65536) - 0.5) * 3.0 / 1100 * 1000
```

The `- 0.5` recenters the fraction so the baseline sits at zero and the QRS spikes come out as positive numbers. The 1100 is the gain of the ECG sensor's instrumentation amplifier. The trailing `* 1000` converts volts to millivolts so the resulting numbers are easier to read.

Plugging the example row's numbers in:

EDA at sample 1: `(15198 / 65536) * 3.0 / 0.132` is about 5.27 microsiemens. A normal resting value.

ECG at sample 1: `((33060 / 65536) - 0.5) * 3.0 / 1100 * 1000` is about 0.012 millivolts. Close to the baseline, not at a heartbeat.

Across the whole recording the values look like:

- EDA ranges from about 3.8 to 9.7 microsiemens (normal sympathetic activity range)
- ECG swings between roughly -0.67 and +0.66 millivolts, which is the typical QRS amplitude window

EDA outside roughly 0 to 50 microsiemens or ECG values much beyond 1 millivolt indicate an electrode contact problem.

## The LSL stream version

The LSL stream OpenSignals broadcasts is a network pipe carrying the same sample content, but stripped down. There is no `nSeq` and no `DI`: just the sensor values, one tuple per sample, going out at the configured sample rate.

The settings on the stream:

- Name: usually `OpenSignals`, configurable in OpenSignals preferences
- Type: usually the device MAC, like `00:07:80:0F:31:9C`
- Channel count: matches the number of active sensors. For a 2-sensor ECG+EDA setup, it is 2. A 3-channel setup is also common (digital input + 2 sensors).
- Sample rate: matches whatever `sampling rate` is set to in the recording configuration.
- Channel format: 32-bit floats (the raw ADC integers cast to float for transport).

A single LSL sample, if printed, looks like:

```
([33060.0, 15198.0], 12345.6789)
```

The first part is the channel values. The order matches the `sensor` field in the file's JSON header, so for this recording the first value is ECG ADC and the second is EDA ADC. The second part is the LSL timestamp in seconds since the LSL clock started, useful for time-aligning multiple streams.

### Channel auto-detection

`real_plux` reads the per-channel label from the LSL stream's metadata at startup and maps EDA / ECG by name (case-insensitive). If labels are missing or unrecognised, `Config.REAL_PLUX_EDA_CHANNEL` and `REAL_PLUX_ECG_CHANNEL` are used as the fallback. Either way the resolved indices are printed at startup so the operator can confirm them in the launcher terminal.

The mock path reads the file's JSON header and maps sensors by name, so switching to a recording with a different channel order requires no edits.

## How HR and HRV come out of the ECG voltage

The PLUX hub does not compute heart rate or heart rate variability. It only delivers the voltage trace. To get HR the pipeline must find the R-peaks in the ECG (the tall positive spikes that happen once per heartbeat) and measure the time between consecutive ones. That time is the RR interval.

The heart rate per beat is:

```
HR (BPM) = 60000 / RR_interval_in_ms
```

So if two consecutive R-peaks are 800 ms apart, the instantaneous heart rate is 75 BPM. The next pair gives the next value, and the HR series steps along once per heartbeat, holding the previous value in between.

HRV is measured as RMSSD (root mean square of successive differences). Computed from the last 10 seconds of RR intervals:

```
RMSSD = sqrt( mean( (RR_n+1 - RR_n)^2 ) )
```

If the RR intervals are perfectly regular (which would be physiologically weird), RMSSD is zero. If they vary beat to beat, RMSSD is larger. Healthy adults at rest tend to sit in the 20 to 80 ms range. Acute stress lowers HRV; relaxation raises it.

The window length is a clinical trade-off. The classical short-term HRV standard (Task Force 1996 / ESC) is 5 minutes (300 s), the most stable estimate but too laggy for real-time biofeedback. The ultra-short-term literature (Shaffer & Ginsberg 2017, Munoz et al. 2015) treats 60 s as the practical minimum: at 60 s the standard deviation of the RMSSD estimate is roughly 12 ms, versus roughly 56 ms at 10 s. The pipeline defaults both knobs (`Config.RMSSD_WINDOW_SEC` and `Config.DS2_HRV_WINDOW_SEC`) to 60 s.

Finding the R-peaks reliably is the tricky part, because real ECG is noisy and the peak amplitude differs from person to person and recording to recording. The pipeline offers two derivation methods, selected by `Config.DATA_SOURCE`:

Both derivation paths run on the same canonical NeuroKit2 chain, following the design principle stated in the VRET technical report Section 1: every number is computed by a validated library call rather than hand-rolled signal processing. The chain is `nk.ecg_clean` then `nk.ecg_peaks(correct_artifacts=True)` (which delegates to `nk.signal_fixpeaks` internally for missed / doubled-beat correction, the technical-report Bug 3 fix). The two paths differ only in how the resulting peaks are turned into per-tick HR and RMSSD output:

HR via `nk.ecg_rate` over a trailing `HR_WINDOW_SEC` (default 30 s).
RMSSD via `_gated_rmssd_from_peaks` (Kubios + Malik 20 % gate) over a
trailing `RMSSD_WINDOW_SEC` (default 60 s). Values are recomputed
every 0.5 s and held with zero-order hold between updates. RMSSD
outside [5, 300] ms is rejected as a detector failure.

For mock mode the detection runs once over the whole recording at
load time and the HR / HRV series are stored. For the live PLUX path
the same logic runs incrementally on a rolling 65 s ECG buffer.
Exact formulas for both backends in [METHODS.md](METHODS.md).

## EDA needs no derivation

EDA is the simplest of the three signals. Once the ADC-to-microsiemens conversion is done, that is already the value the rest of the pipeline uses. Skin conductance changes slowly (over seconds, not milliseconds), so the high sample rate of the device is just oversampling. The pipeline does NOT smooth EDA per the PDF spec — instead it decomposes raw EDA into tonic + phasic via `nk.eda_phasic` on a rolling 60-second window, and only the phasic component (tonic drift removed) feeds the stress score.

## Putting it all together

The Python pipeline subscribes to the LSL stream, applies the ADC-to-units conversions, derives HR and HRV from the ECG voltage trace (by one of the two methods above), and from that point onward operates on three continuous time series: EDA in microsiemens, HR in BPM, and HRV (as RMSSD) in milliseconds. The rest of the math, including the 120-second baseline, the percentage-deviation fusion, and the stress state classification, runs on those three numbers. That part is covered in `DATA_FLOW.md`.

## A short version

PLUX samples the voltage on each electrode 200 or 1000 times per second. Each voltage reading is a 16-bit integer (a raw count from the analog-to-digital converter, between 0 and 65535). PLUX provides transfer formulas to turn those counts into physical units: microsiemens for the EDA electrodes, millivolts for the ECG leads. From the ECG voltage trace, the pipeline finds heartbeats (each R-peak is a tall spike) and the time between consecutive heartbeats gives heart rate. The variability in those inter-beat times gives HRV. The device itself just delivers voltage; everything physiologically meaningful is computed by the Python middleware.
