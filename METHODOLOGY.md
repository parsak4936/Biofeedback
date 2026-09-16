# VR Biofeedback Framework — Methodology and Architecture

Working chapter for the paper. Covers only the biofeedback engineering
contribution. The psychology chapter and the Unity chapter are
separate and sit alongside this one.

Full-length version. Compression down to conference-format word
counts happens later, off this draft.

---

## 1. Scope

What this chapter is about. The biofeedback engine that runs on the
lab workstation: how physiological signals become a stress index, how
that index becomes commands for a VR scene, how the recording lands
on disk.

Three things are deliberately outside the scope. Clinical protocol
choices — session count per participant, order of exposures, use of
questionnaires, familiarisation runs — belong to the psychology
chapter. The VR scenes themselves — balloon flight for acrophobia,
spider dynamics for arachnophobia, audience behaviour for public
speaking — belong to the Unity chapter. Study ethics — approval
numbers, consent language, retention policy — belong to a general
section governed by the lab administration.

Everything below is engineering that can be defended from the
physiological signal literature or from software practice cited in
the reference list. Nothing that requires clinical or presentation
authority.

---

## 2. Framing

Central design decision came first. Physiological measurement,
per-participant baselining and stress computation, and VR
presentation are three different problems. Prior systems in the
adaptive VR literature — Moldoveanu and colleagues for acrophobia,
Garcia-Palacios and colleagues for arachnophobia, Anderson and
colleagues for social anxiety — bundle all three into one codebase
tied to one scene. That coupling means adding a second phobia
duplicates the physiology stack.

Our engine is a server. Each Unity phobia scene is a client. Two
network channels between them: short text commands from the server
to Unity, structured JSON telemetry from Unity back to the server.
The contract is fixed and does not know which scene is running. As a
consequence, adding a fourth phobia is Unity-side work only.

The physiology chain we run is essentially Moldoveanu's, cited
explicitly. What is new here is not the fusion formula but the way
scene-agnostic infrastructure supports a range of exposure paradigms
within the same lab, on identical instrumentation. That reduces
cost, standardises measurement across studies, and makes
cross-phobia comparisons possible.

An earlier study at this lab, reported by Daghooghi and colleagues,
ran a similar VR exposure protocol without any biofeedback loop.
The current framework is the technical extension that closes that
loop. Continuity between the two versions of the study matters for
how the paper reads.

---

## 3. Hardware and materials

Each participant wears a wireless biosignalsplux hub on a chest band,
sold by PLUX Wireless Biosignals (Lisbon). Two sensor cables plug
in: a three-lead ECG cable in a standard Lead I configuration, and a
two-pad electrodermal cable on the non-dominant hand. Sixteen-bit
analog-to-digital resolution, three-volt reference, sampling at
200 Hz.

Bluetooth carries the data from the hub to a Windows workstation.
The vendor software (OpenSignals) publishes the signals over the Lab
Streaming Layer network protocol; anything running locally can
subscribe. Our engine is one such subscriber. There is one subtlety
worth flagging: the OpenSignals bridge publishes already-converted
physical units (microsiemens for skin conductance, millivolts for
ECG), not raw analog-to-digital integers as commonly assumed.
Documentation is silent on the point and we spent several days
tracking down a "constant 25 microsiemens" reading that turned out
to be an already-converted saturated ADC value. A configuration flag
switches between the two conventions so the same code handles
archived recordings that carry raw values.

Immersive display uses a Meta Quest headset tethered to the
workstation over Quest Link. Unity 2022 LTS renders the scene and
talks to the biofeedback engine through local UDP sockets, not
through the tether. Nothing clinically important depends on the
specific headset model; a different tethered or standalone device
would work identically.

---

## 4. System architecture

### 4.1 Component overview

The server is a Python 3.10 process, split into modules with one
job each. Communication between modules is by explicit function
calls; no shared mutable state to worry about.

On the input side, a data-source module subscribes to the Lab
Streaming Layer feed, reads the channel labels to figure out which
channel carries which sensor, and applies the conversion formulas
when needed. Sanity bounds reject samples outside plausible
physiological ranges, holding the last valid value in their place.

In the middle, a signal-processing module maintains the rolling
window fed to the phasic decomposition and accumulates the baseline
buffer while the operator is in the baseline phase. A fusion module
computes per-signal deltas against the frozen baseline, standardises
them, combines them with the Moldoveanu weights, and classifies the
result into three states.

On the output side, one module broadcasts everything on the Lab
Streaming Layer for the dashboard to consume. Another sends short
UDP commands to Unity. A third listens for JSON telemetry packets
from Unity and hands them to the recorder.

Persistence sits in a session-manager module. It writes three files
per session: a clinical record, a forensic per-tick trace, and a
JSON metadata file that carries intake plus baseline plus locked
thresholds.

Not one of these modules knows which scenario is running. The
scenario label is learned at runtime from the first telemetry packet
Unity sends, and it determines which extra columns get added to the
clinical record.

### 4.2 Diagram 1 — system topology

Where each component sits and who talks to whom. Renderable in
mermaid.live or importable in draw.io as a mermaid diagram.

```
flowchart LR
    P([Participant]) -->|ECG, EDA| PLUX[PLUX biosignalsplux hub]
    PLUX -->|Bluetooth| OS[OpenSignals]
    OS -->|LSL: physical units, 200 Hz| BF[Biofeedback Engine<br/>Python 3.10]

    BF -->|UDP :5005: text commands| U[Unity VR Scene]
    U -->|UDP :5006: JSON telemetry, 10 Hz| BF

    BF -->|LSL: state + telemetry| DASH[Operator Dashboard<br/>PyQt5]
    BF -->|CSV, JSON files| DISK[(Session Record)]

    U -->|render| HMD[Meta Quest HMD]
    HMD --- P
```

ASCII fallback for readers who cannot render mermaid:

```
   Participant  ---  ECG, EDA  --->  PLUX biosignalsplux hub
                                             |
                                             |  Bluetooth
                                             v
                                        OpenSignals
                                             |
                                             |  Lab Streaming Layer, 200 Hz
                                             v
   Operator Dashboard <--- LSL ----   BIOFEEDBACK ENGINE   ---> Session record (CSV, JSON)
                                        ^          |
                                        |          |
                                  JSON  |          |  text commands
                             (UDP 5006) |          v  (UDP 5005)
                                       Unity VR Scene
                                            |
                                            |  render
                                            v
                                       Meta Quest HMD
                                            |
                                       back to Participant
```

### 4.3 Diagram 2 — multi-scenario Unity contract

Same engine, different Unity scenes as clients. The engine does not
change when you add a new phobia; the contract is fixed.

```
flowchart TB
    BF[Biofeedback Engine<br/>one server, always the same]

    BF -.->|"same 4 text commands"| A[Acrophobia scene<br/>balloon flight]
    BF -.->|"same 4 text commands"| B[Arachnophobia scene<br/>spider dynamics]
    BF -.->|"same 4 text commands"| C[Public Speaking scene<br/>audience behaviour]
    BF -.->|"same 4 text commands"| D[Future scene N]

    A -->|"JSON<br/>&#123;height: 12.3&#125;"| BF
    B -->|"JSON<br/>&#123;size, count&#125;"| BF
    C -->|"JSON<br/>&#123;looking, audience&#125;"| BF
    D -->|"JSON<br/>&#123;whatever fields&#125;"| BF
```

ASCII fallback:

```
                          BIOFEEDBACK ENGINE
                    (same 4 commands out to every scene)
                              |    ^
              +---------------+    +---------------+
              |               |    |               |
              v               v    v               v
           Acro-         Arachno-  Public         Future
           phobia        phobia    Speaking       scene N
           scene         scene     scene
              |               |    |               |
              |               |    |               |
   JSON: {height:12.3}   {size,   {looking,     {whatever}
                          count}   audience}
              |               |    |               |
              +---------------+----+---------------+
                              |
                              v
                    BIOFEEDBACK ENGINE
             (captures scenario label + all fields
              verbatim into samples.csv)
```

Design intent is that the recording captures whatever any scene
chooses to emit, without knowing in advance what those fields are.
The first telemetry packet of a session establishes the column set;
everything afterward is a value fill for those columns. See section 9
for the recording detail.

### 4.4 The contract between server and Unity

Two directions, asymmetric on purpose.

Commands from the server go out as short UTF-8 strings, one per UDP
datagram. Four values only: `start` to mark the beginning of the
live phase, `stop` to mark the end, `increase` when the participant
is calm and the scene may escalate, `decrease` when the participant
is stressed and the scene should back off. Unity interprets each
command however the loaded scene requires. For a balloon flight,
`increase` raises altitude; for an audience scene, `increase` might
bring a new person into the room. The engine does not care.

Telemetry from Unity uses one shape for every scene:

    {"scenario": "<name>", "data": {<field>: <value>, ...}}

Server treats the packet as opaque. The scenario label labels the
scene; the fields inside data are recorded verbatim. Nothing on the
server declares which fields are legal or expected. Add a fourth
phobia and no biofeedback change is required.

---

## 5. Signal acquisition and conditioning

Sensor packets arrive on the LSL feed and pass through three checks
before any pipeline math touches them.

Range first. Values outside plausible physiological limits are
rejected as sensor artifacts. Skin conductance is clipped to the
zero to eighty microsiemens interval; ECG to a few millivolts either
side of zero; heart rate to thirty to two hundred and twenty beats
per minute; RMSSD to five to three hundred milliseconds. Rejected
samples never enter the baseline buffer, and the previous valid
value is held so downstream code always sees something plausible.

Then a disconnect check. If the variance of a signal collapses below
a small threshold for more than fifteen seconds, the code assumes an
electrode has come loose or a cable has been unplugged, and issues a
warning to the operator console. It does not abort. The warning is
a hint for the operator, not a hard failure.

A three-tier sampling-rate cascade sits under all this. The device
runs at 200 Hz. The internal processing tick is 10 Hz; the physiology
does not change faster than that, and both HR and HRV have their
own trailing-window estimators underneath. The Unity command channel
is throttled to at most one command per second because rapid balloon
or audience changes look unnatural in VR. Three rates, decoupled,
each configurable in one place.

Two extraction backends live in the code in parallel. NeuroKit2 is
the default, matching the reference implementation from the lab's
prior work. biosignalsnotebooks (the PLUX official package) is the
alternative. Both feed identical downstream math. Redundancy is
there for a reason: on any recorded session we can rerun both chains
offline and quantify their agreement, producing correlation
coefficients for HR, RMSSD, and the composite stress index. That
check appears in the results section of any paper as evidence that
the choice of extraction library does not drive the finding.

### 5.1 Heart rate and heart-rate variability

Cleaning comes first, always. The raw ECG passes through a
high-pass filter at 0.5 Hz to remove baseline drift and a notch
filter at 50 Hz to remove mains hum (60 Hz in the United States).
Cleaning is not optional: the Pan and Tompkins detector returns
zero peaks on raw mains-contaminated ECG, and even the hybrid
NeuroKit2 detector fares only marginally better.

Peak detection follows. Both backends produce a series of R-peak
indices. NeuroKit2 uses a hybrid method with Kubios artifact
correction on top (Lipponen and Tarvainen 2019), which interpolates
missed beats before they hit the RMSSD calculation.
biosignalsnotebooks uses the classic Pan-Tompkins detector directly
with no interpolation.

From the R-peaks, per-window heart rate is the reciprocal of the
mean interval between successive peaks, expressed in beats per
minute:

$$
HR = \frac{60 \cdot f_{s}}{\overline{\Delta R}}
\quad \text{beats per minute}
$$

where $\Delta R = R_{i+1} - R_{i}$ is the sample-index difference
between successive R-peaks and $f_{s}$ is the sampling rate. Heart
rate is recomputed every 0.5 seconds over a trailing window of
thirty seconds, and held with zero-order hold between recomputes.

Heart-rate variability, as reported here, means the root mean
square of successive differences between consecutive RR intervals.
That measure is the most widely used time-domain HRV metric in
short recordings:

$$
RMSSD = \sqrt{ \frac{1}{N-1} \sum_{i=1}^{N-1}
\bigl( RR_{i+1} - RR_{i} \bigr)^{2} }
\quad \text{milliseconds}
$$

Intervals themselves are $RR_{i} = 1000 \cdot \Delta R_{i} / f_{s}$
in milliseconds. RMSSD is recomputed every 0.5 seconds over a
trailing window of sixty seconds.

Sixty seconds is deliberate and cites two bodies of literature.
Task Force 1996 guidelines recommend 300 seconds for stable
short-term HRV. Ultra-short-term literature since — Munoz and
colleagues, Shaffer and Ginsberg — establishes 60 seconds as the
practical minimum where the standard deviation of the RMSSD
estimator drops into a usable range (about 12 ms at 60 s versus
about 56 ms at 10 s). Anything shorter and estimator noise
overwhelms real physiological signal.

Malik and colleagues (Task Force 1996) contribute the RMSSD
artifact gate. After Kubios interpolation, each RR interval is
compared to its predecessor. Pairs whose relative change exceeds
20 percent are removed:

$$
\text{keep } \Delta RR_{i} \text{ if }
\frac{ \lvert RR_{i+1} - RR_{i} \rvert }{ RR_{i} } \le 0.20
$$

Rationale: ectopic beats that survive Kubios correction are, by
construction, outliers in the successive-difference series and
would inflate RMSSD dramatically. The alternative backend uses the
Lippman-Stein-Lerman ectopy rule instead, which is a slightly
different formulation of the same idea (removing pairs rather than
single beats).

### 5.2 Electrodermal activity

Skin conductance is the sum of two components with very different
timescales. The tonic level drifts over minutes and reflects
electrode hydration, ambient temperature, and general arousal
baseline. Superimposed on it are faster, seconds-scale bumps called
skin conductance responses, one per sympathetic event. Only phasic
is useful for real-time stress feedback; tonic varies too slowly
and too often for the wrong reasons.

Greco and colleagues (2016) formulated the cvxEDA algorithm to
recover the two components jointly. It solves an inverse problem
under a sparsity prior on the phasic driver:

$$
\min_{\mathbf{t}, \mathbf{p}, \mathbf{q}}
\frac{1}{2} \lVert \mathbf{y} - \mathbf{t} - \mathbf{p} -
\mathbf{L} \mathbf{q} \rVert_{2}^{2}
+ \alpha \lVert \mathbf{A} \mathbf{p} \rVert_{1}
+ \frac{\gamma}{2} \lVert \mathbf{B} \mathbf{t} \rVert_{2}^{2}
$$

Observed skin conductance is $\mathbf{y}$, tonic is $\mathbf{t}$,
phasic driver is $\mathbf{p}$, and $\mathbf{q}$ absorbs unmodelled
dynamics. Full derivation in Greco et al. 2016; we call the
implementation wrapped by NeuroKit2.

Every half second, cvxEDA runs on the most recent 60 seconds of
raw EDA, resampled to 10 Hz. The last sample of the recovered
phasic component becomes the current phasic value. One safeguard:
any phasic reading whose absolute magnitude exceeds 1 microsiemens
is discarded as ringing from the resampler at the window edge.
Real responses rarely exceed that magnitude and never appear as
isolated one-sample spikes.

The alternative backend has no cvxEDA. It offers a simpler recipe
from the biosignalsnotebooks EDA tutorial: subtract from the raw
signal its low-pass filtered version, with the cutoff at the
canonical 0.05 Hz boundary. We support that mode too, for
comparison; the default remains cvxEDA.

---

## 6. Personal calibration

Each session opens with 120 seconds of baseline. Participant sits
quietly, sensors already on, VR scene not yet started. Three things
happen during and after this window.

Averages come first. Cleaned EDA, HR, and RMSSD across the baseline
are averaged after a three-sigma outlier filter removes movement
artifacts:

$$
\mu_{x} = \frac{1}{N} \sum_{i=1}^{N} x_{i}
\quad \text{for } x \in \{ \text{EDA}, \text{HR}, \text{RMSSD} \}
$$

Summation restricts to samples within three standard deviations of
the crude sample mean. Rejected-sample counts are logged.

Standard deviations come next, one per delta signal. These divide
the live delta during z-scoring in the live phase.

Sigma floors sit on top. If a measured sigma falls below a
physiological minimum, it is clamped upward to that floor and the
console prints a warning. Three floors: two percent of resting HR,
five percent of resting RMSSD, and 0.02 microsiemens for phasic
EDA. Purpose is to stop a flat baseline — from an unusually still
participant or from a technical acquisition issue — producing
enormous standardised scores on trivial live deviations. Values
were chosen from the physiological ranges reported in Task Force
1996, Shaffer and Ginsberg 2017, and Boucsein 2012. They are
conservative rather than tight.

Baseline collection is the most fragile part of the whole
procedure. Nervous participants have already-elevated baselines,
and every subsequent live tick then looks calm by comparison.
Operator protocol includes a short briefing before baseline and
monitoring the trace to catch fidgeting. In practice we discard
any baseline where a sigma had to be floored more than once and
restart the session.

---

## 7. Stress index calculation

Three physiological channels reduce to one scalar following the
weighted-composite formulation of Moldoveanu and colleagues.

### 7.1 Per-signal deltas

Each channel first turns into a delta against the frozen personal
baseline:

$$
\begin{aligned}
\Delta_{\text{EDA}} &= EDA_{\text{phasic}}
&& \text{microsiemens} \\
\Delta_{\text{HR}} &= \frac{ HR_{\text{live}} -
\mu_{\text{HR}} }{ \mu_{\text{HR}} } \cdot 100
&& \text{percent} \\
\Delta_{\text{HRV}} &= \frac{ \mu_{\text{HRV}} -
HRV_{\text{live}} }{ \mu_{\text{HRV}} } \cdot 100
&& \text{percent, inverted}
\end{aligned}
$$

Sign convention: every delta increases during stress. EDA rises
with sympathetic arousal, so its phasic value serves as-is. Heart
rate rises for the same reason, and the percentage change is signed
positive for stress. HRV works the other way. Parasympathetic
withdrawal during stress shortens vagally-mediated beat-to-beat
variability, so a decrease in RMSSD contributes positively to the
composite. The inversion is standard in the psychophysiology
literature (Kim and colleagues 2018, Kreibig 2010, Boucsein 2012).

### 7.2 Standardisation

Each delta divides by its own baseline standard deviation:

$$
z_{x} = \frac{ \Delta_{x} - \mu_{\Delta_{x}} }
{ \sigma_{\Delta_{x}} }
\quad \text{for } x \in \{ \text{EDA}, \text{HR}, \text{HRV} \}
$$

Standardisation matters because the three raw deltas have wildly
different natural magnitudes. Skin conductance moves in tenths of
microsiemens; heart rate in single-digit percentages; RMSSD in
tens of percent. Without standardisation the signal with the
largest raw span would dominate the composite by scale alone,
regardless of how informative it was physiologically.
Standardisation also makes cross-participant comparisons
meaningful, because the score is expressed in units of the
participant's own baseline variability.

### 7.3 Weighted composite

Three standardised deltas combine with fixed weights from
Moldoveanu 2023:

$$
S_{\text{instant}} = 0.5 \cdot z_{\text{EDA}}
+ 0.3 \cdot z_{\text{HRV}}
+ 0.2 \cdot z_{\text{HR}}
$$

Weights reflect established knowledge about the specificity of
each signal to sympathetic arousal on short timescales. Skin
conductance is the most direct real-time sympathetic indicator and
gets the largest share. HRV integrates parasympathetic withdrawal
over longer windows and gets the next. Raw heart rate is
non-specific — it moves for physical activity as much as for
emotional arousal — so it must be weighted down or it swamps the
composite during normal movement (Healey and Picard 2005; Sano and
Picard 2013).

Using published weights instead of tuning our own keeps the
paper's contribution on the framework rather than on rediscovering
a fusion rule.

### 7.4 Smoothing

The instantaneous composite gets a rolling mean before it is used
for classification:

$$
S_{t} = \frac{1}{W} \sum_{k=0}^{W-1}
S_{\text{instant}, t-k}
\quad \text{with } W = f_{p} \cdot \tau_{\text{smooth}}
$$

Smoothing constant $\tau_{\text{smooth}} = 3$ seconds and
$f_{p} = 10$ Hz, so $W = 30$ samples. Three seconds is a
compromise. Shorter windows let per-tick recompute artifacts from
the HR and RMSSD estimators leak into the classification. Longer
windows attenuate real, fast physiological transients that we want
to catch. Choice was empirical: inspecting live traces from four
early participants, plotting the smoothed trajectory at one, three,
and five seconds, and confirming with the supervisor that three
seconds preserved every clinically meaningful event while
suppressing tick-level noise.

### 7.5 Diagram 3 — signal processing chain

What happens inside the biofeedback engine, layer by layer.

```
flowchart TB
    subgraph Inputs
        A[Raw ECG mV] --> C1[ecg_clean: 0.5Hz HP + 50Hz notch]
        B[Raw EDA microsiemens] --> D1[Rolling 60s window]
    end

    subgraph HR_HRV
        C1 --> C2[R-peak detection<br/>+ Kubios correction]
        C2 --> C3[HR: 60 / mean RR<br/>over 30s trailing window]
        C2 --> C4[RMSSD: Malik 20% gate<br/>over 60s trailing window]
    end

    subgraph EDA
        D1 --> D2[cvxEDA phasic decomposition]
        D2 --> D3[Last sample; reject if abs > 1 uS]
    end

    C3 --> F1[delta HR = percent change from baseline]
    C4 --> F2[delta HRV = percent decrease from baseline]
    D3 --> F3[delta EDA = phasic microsiemens]

    F1 --> Z[Standardise each delta<br/>against its own baseline sigma]
    F2 --> Z
    F3 --> Z

    Z --> W[Weighted sum:<br/>0.5 EDA + 0.3 HRV + 0.2 HR]
    W --> S[3-second rolling mean<br/>yields S_t]
    S --> CL[Classify against<br/>MILD and HIGH thresholds]
    CL --> CMD[UDP command to Unity]
```

ASCII fallback:

```
   raw ECG ----> clean ----> R-peaks ----> HR   (30s window, recompute every 0.5s)
                                       \-> RMSSD (60s window + Malik 20% gate)

   raw EDA ----> 60s window ---> cvxEDA ---> phasic (last sample, reject if > 1 uS)

     HR         --> delta HR   = (live - baseline) / baseline * 100
     RMSSD      --> delta HRV  = (baseline - live) / baseline * 100   (inverted)
     phasic EDA --> delta EDA  = phasic value in microsiemens

     each delta --> standardise against its own baseline sigma
                --> weighted sum   0.5 * z(EDA) + 0.3 * z(HRV) + 0.2 * z(HR)
                --> 3-second rolling mean, yields S_t
                --> classify against MILD and HIGH thresholds
                --> send command to Unity
```

---

## 8. Classification and the feedback loop

Two thresholds partition the smoothed stress index into three
states. They freeze at the end of the baseline window using the
standard deviation of the raw instantaneous composite during that
same baseline:

$$
\begin{aligned}
\theta_{\text{MILD}} &= \mu_{S_{t}}^{\text{baseline}}
+ 1.28 \cdot \sigma_{S_{\text{instant}}}^{\text{baseline}} \\
\theta_{\text{HIGH}} &= \mu_{S_{t}}^{\text{baseline}}
+ 2.33 \cdot \sigma_{S_{\text{instant}}}^{\text{baseline}}
\end{aligned}
$$

Multipliers 1.28 and 2.33 are the standard normal z-scores at the
90th and 99th percentiles. During a calm baseline the participant
therefore spends 90 percent of the time in the calm band, 9 percent
in the stressed band, and 1 percent in the ultra-stressed band, by
construction. Live excursions above those bands mark real stress
episodes rather than baseline noise.

Subtle point about which standard deviation goes into the
threshold. The sigma comes from the raw per-tick composite series,
not from the smoothed series. Rolling-mean smoothing shrinks
variance by roughly the square root of the window length; using
the smoothed sigma would compress the bands and every real arousal
would jump straight from calm to ultra-stressed. The mean on the
other hand uses the smoothed series because the live classification
operates on the smoothed value; means are invariant under rolling
averages, so the asymmetry is safe.

State transitions drive Unity in a very simple way. Calm allows
escalation: balloon rises, a new audience member walks in. Stressed
holds the scene at its current level. Ultra-stressed signals a
back-off: balloon lowers, an audience member looks away. The UDP
bridge throttles so at most one command per second reaches Unity;
extra commands during a throttle window drop, they do not queue.
This keeps the VR experience smooth even when the physiology
briefly oscillates near a threshold.

### 8.1 Diagram 4 — session state machine

Operator drives the pipeline through five phases. Nothing
auto-transitions.

```
stateDiagram-v2
    [*] --> IDLE
    IDLE --> BASELINE: Start Baseline click
    BASELINE --> BASELINE_DONE: 120 seconds elapsed
    BASELINE_DONE --> LIVE: Start Live Session click
    LIVE --> STOPPED: Stop click
    STOPPED --> LIVE: Start Live Session click (LIVE_RESTART)
    STOPPED --> [*]: Close dashboard window

    note right of BASELINE
        No telemetry from Unity yet.
        Physiology rows buffer in memory.
    end note
    note right of BASELINE_DONE
        Averages, sigma, thresholds
        locked. Metadata written.
    end note
    note right of LIVE
        Fusion active. UDP commands
        flow to Unity. First telemetry
        packet commits the CSV header.
    end note
```

ASCII fallback:

```
    [start]
       |
       v
    +--------+  Start Baseline    +------------+   120 s elapsed
    | IDLE   |------------------> | BASELINE   |------------------+
    +--------+                    +------------+                  |
                                                                  v
                                                         +----------------+
                                                         | BASELINE_DONE  |
                                                         +----------------+
                                                                  | Start Live
                                                                  v
    +---------+     Stop       +-------+
    | STOPPED | <------------- | LIVE  |
    +---------+                +-------+
        |
        | Start Live again (rotates CSV, same baseline)
        v
    (back to LIVE)

        |
        | Close dashboard window
        v
      [end]
```

### 8.2 Diagram 5 — feedback loop detail

The state-to-command mapping with the throttle and the
wait-for-calm gate, at tick granularity.

```
flowchart LR
    ST[S_t at this tick] --> CL{Classify}
    CL -->|calm| CMD_INC[Candidate command: INCREASE]
    CL -->|stressed| HOLD[No command; scene holds]
    CL -->|ultra| CMD_DEC[Candidate command: DECREASE]

    CMD_INC --> GATE{Wait-for-calm<br/>gate open?}
    CMD_DEC --> GATE
    GATE -->|no| DROP1[Drop and count]
    GATE -->|yes| THROT{Throttle window<br/>elapsed?}
    THROT -->|no| DROP2[Drop and count]
    THROT -->|yes| SEND[Send UDP packet<br/>to Unity]

    SEND --> AUDIT[Log to unity_udp.csv]
    SEND --> RESET[Reset throttle window]
```

ASCII fallback:

```
   S_t at this tick
        |
        v
    +------------+
    | Classify   |
    +------------+
       /   |   \
      /    |    \
     v     v     v
   calm  stressed  ultra
     |     |         |
     v     v         v
  INCREASE HOLD    DECREASE
   (candidate)     (candidate)
     |               |
     +-------+-------+
             |
             v
   +-------------------+
   | wait-for-calm     |
   | gate open?        |
   +-------------------+
      no          yes
      |            |
      v            v
   drop &      +----------------+
   count       | throttle       |
               | 1 s elapsed?   |
               +----------------+
                  no         yes
                  |           |
                  v           v
               drop &      send UDP
               count       reset throttle
                              |
                              v
                         log to unity_udp.csv
```

The wait-for-calm gate serves a specific purpose. When the live
phase first starts, the participant may still be adjusting (putting
on the headset, settling in). First stress readings are often
noisy. In parallel, the fusion engine returns a synthetic calm
during its first buffer-warmup second that should not count as a
real reading. Gate ignores both. It watches for the first genuine
calm state, and once it sees one, the gate opens permanently for
the session.

---

## 9. Session record

Every session produces one folder on disk. Four files inside it.

The clinical record, samples.csv, captures per-second the smoothed
physiological values, the deltas, the standardised composite, the
current state, and any telemetry Unity is emitting. Telemetry
columns are dynamic. Fields inside the first non-empty telemetry
packet Unity sends become the header for the whole session. Rows
that arrive before Unity Play (during baseline) buffer in memory
and flush to disk once the schema is known. If Unity never
connects, the header commits with no dynamic columns and the
physiology data still lands on disk.

The forensic trace, diagnostic.csv, records the raw signal at
every internal pipeline tick along with an acquisition status code
(NEW-DATA, HOLD-LAST, INVALID, OUT-OF-RANGE). This file lets a
reviewer reconstruct exactly what the pipeline saw at any moment.
It is verbose but cheap; the whole file is only a few megabytes
for a typical session.

Session metadata sits in metadata.json. It contains the intake
information, the frozen baseline means and sigmas, the two
thresholds, and the scenario label learned from the first telemetry
packet. Written once at session start (intake only), amended once
at baseline lock.

Finally unity-udp.csv logs every UDP packet sent to Unity with
its timestamp, target state, actual state, and gate status. Audit
lets us reconstruct the full exposure trajectory independently of
the physiology traces. Useful when investigating whether a
physiological change followed a scene event or preceded it.

---

## 10. Backend approach

Two extraction backends coexist in the codebase. NeuroKit2 is the
default. biosignalsnotebooks is the alternative. Both can be swapped
with a single configuration flag.

Default choice reflects three considerations. Broader signal
coverage — including cvxEDA for electrodermal decomposition, which
the PLUX package lacks. Kubios artifact correction bundled in
(Lipponen and Tarvainen 2019), which handles missed beats more
robustly than Pan and Tompkins on its own. And it is the toolbox
used in an earlier reference implementation from the same lab, so
keeping it as the default preserves comparability with prior work.

Keeping the alternative alongside serves two purposes. Independent
implementation of the same algorithms means any finding survives
the "did the library manufacture this?" question. And it respects a
specific supervisor request early in the project, that we implement
extraction using functions from the official PLUX package. Both
backends share the entire downstream fusion and classification
chain; the switch changes only the extraction step.

Quantitative agreement measurement is available. On one clean
archive session replayed through both chains, Pearson correlations
of extracted signals sit at $r = 0.76$ for HR, $r = 0.87$ for RMSSD
on clean segments, and $r = 0.90$ for the composite stress index.
RMSSD medians differ in absolute terms because
biosignalsnotebooks does not apply the Malik gate. The comparison
is easy to expand: `compare_backends.py` runs both chains on any
recording and reports the numbers plus a side-by-side plot. Later
papers would present the comparison across several sessions rather
than one, but the machinery is already in place.

---

## 11. Reproducibility

Five design decisions in the codebase support reproducibility for
publishable research.

Deterministic streaming. When the pipeline replays a recorded
session for offline analysis, the mock data source waits for the
acquisition consumer to subscribe before publishing the first
sample. Eliminates a startup race that used to produce different
baselines on different runs of the same input file.

Fixed calibration constants. Analog-to-digital conversion uses the
constants from the biosignalsnotebooks reference conversion routine
(reference voltage of three, EDA gain of 0.12, ECG gain of 1.019,
ECG offset of 0.5). All are hard-coded in one place and documented
per sensor.

Two independent extractions. See section 10. Every claim can be
checked against an alternative chain.

Full forensic trail. The diagnostic file records the raw signal at
every internal tick along with the status code, so any peculiar
behaviour in the clinical record can be traced back to what the
pipeline actually saw.

Complete UDP audit. Every command sent to Unity is recorded to
disk, so the sequence of exposure changes during the session can
be reconstructed independently of physiology.

---

## 12. Ethics and data protection

This section reflects the decisions taken by the supervisor and
the lab administration and applies from the current version of the
system onwards. Prior recordings collected before the name-removal
refactor remain on file under the original schema and will be
pseudonymised in place before any external sharing.

Written informed consent is collected before every session. Consent
covers the collection of physiological signals (ECG and
electrodermal activity), the storage of derived measures, and the
right to withdraw from the study at any time. Withdrawal triggers
deletion of the participant's session records within a reasonable
window; because no cross-references to the participant exist
outside the session folder, deletion is a single-folder operation.

Personal identifiers stored on disk are limited to a lab-assigned
patient identifier, gender, session number, and session date. Name
and last name are not written to any file the engine produces;
they are held only in operator memory during the intake step, for
the purpose of confirming the correct participant on the day.
Session folders are named by patient identifier rather than by
name.

The lab administrator at the NISC lab, University of Messina,
Elaheh Bakhtiari, acts as data controller. All requests for data
access, correction, or erasure go there.

Retention. Technical recordings (per-session CSV and JSON files)
are kept for five years after the session. Research-facing
artifacts (analysis outputs, published figures, derived tables)
are kept for ten years. Both sets are destroyed after the
respective horizon.

Storage sits on the lab workstation. Encryption at rest is not
currently in place; access is controlled physically through the
lab door. Data is shared only on explicit request and only through
direct transfer. No cloud service, backup provider, or code
repository holds a copy — the data folder is excluded from version
control by policy.

Positioning against prior lab work. This experiment continues an
earlier study conducted at the same lab and reported by Daghooghi
and colleagues (Ref 37). The earlier study did not include the
biofeedback loop; the framework described in this chapter is the
technical extension that enables adaptive VR exposure based on
physiological state. The two versions of the study should be read
as continuous rather than parallel.

The specific ethics-committee approval reference will be added to
the camera-ready version once confirmed.

---

## 13. Reference list

Every entry carries full author list, full title, venue, volume,
issue, article number or pages, year, DOI, and — at the end — a
short note on which section of this document cites it.

Numbering is stable across the document; every "Ref N" in the
prose above corresponds to the same N here.

### 13.1 Adaptive VR and biofeedback (anchor citation)

Ref 1. Moldoveanu, Alin; Moldoveanu, Florica; Asavei, Victor;
Egner, Andrei-Traian; Bălan, Oana. Immersive Phobia Therapy
through Adaptive Virtual Reality and Biofeedback. Applied
Sciences, Vol. 13, No. 18, Article ID 10365, September 2023.
Publisher: MDPI, Basel. DOI:
https://doi.org/10.3390/app131810365. Open access.
Used in: sections 1, 2 (framing); 7.3 (fusion weights); 8
(threshold scheme). Anchor citation for the whole methodology.

### 13.2 VR exposure therapy — foundational and clinical

Ref 2. Rothbaum, Barbara Olasov; Hodges, Larry F.; Kooper, Rob;
Opdyke, Dan; Williford, James S.; North, Max. Effectiveness of
Computer-Generated (Virtual Reality) Graded Exposure in the
Treatment of Acrophobia. American Journal of Psychiatry, Vol. 152,
No. 4, pp. 626 to 628, April 1995. DOI:
https://doi.org/10.1176/ajp.152.4.626.
Used in: introduction / related work — first RCT of VR for
acrophobia.

Ref 3. Emmelkamp, Paul M. G.; Krijn, Merel; Hulsbosch, A. M.;
de Vries, S.; Schuemie, Martijn J.; van der Mast, Charles A. P. G.
Virtual reality treatment versus exposure in vivo: a comparative
evaluation in acrophobia. Behaviour Research and Therapy, Vol. 40,
No. 5, pp. 509 to 516, May 2002. DOI:
https://doi.org/10.1016/S0005-7967(01)00023-7.
Used in: introduction / related work — establishes equivalence of
VR and in-vivo exposure.

Ref 4. Powers, Mark B.; Emmelkamp, Paul M. G. Virtual reality
exposure therapy for anxiety disorders: A meta-analysis. Journal
of Anxiety Disorders, Vol. 22, No. 3, pp. 561 to 569, April 2008.
DOI: https://doi.org/10.1016/j.janxdis.2007.04.006.
Used in: related work — meta-analytic evidence for VR exposure.

Ref 5. Botella, Cristina; Fernández-Álvarez, Javier; Guillén,
Verónica; García-Palacios, Azucena; Baños, Rosa. Recent Progress
in Virtual Reality Exposure Therapy for Phobias: A Systematic
Review. Current Psychiatry Reports, Vol. 19, No. 7, Article 42,
July 2017. DOI:
https://doi.org/10.1007/s11920-017-0788-4.
Used in: related work — recent systematic review.

Ref 6. Wiederhold, Brenda K.; Wiederhold, Mark D. Virtual Reality
Therapy for Anxiety Disorders: Advances in Evaluation and
Treatment. American Psychological Association, Washington DC,
2005. ISBN 978-1-59147-198-7. DOI:
https://doi.org/10.1037/10858-000.
Used in: related work — textbook reference for the field.

Ref 7. Freeman, Daniel; Haselton, Polly; Freeman, Jason; Spanlang,
Bernhard; Kishore, Sameer; Albery, Emily; Denne, Megan; Brown,
Poppy; Slater, Mel; Nickless, Alecia. Automated psychological
therapy using immersive virtual reality for treatment of fear of
heights: a single-blind, parallel-group, randomised controlled
trial. The Lancet Psychiatry, Vol. 5, No. 8, pp. 625 to 632,
August 2018. DOI:
https://doi.org/10.1016/S2215-0366(18)30226-8.
Used in: related work — recent large-scale VR acrophobia RCT.

Ref 8. García-Palacios, Azucena; Hoffman, Hunter G.; Carlin,
Albert; Furness, Thomas A.; Botella, Cristina. Virtual reality in
the treatment of spider phobia: a controlled study. Behaviour
Research and Therapy, Vol. 40, No. 9, pp. 983 to 993, September
2002. DOI:
https://doi.org/10.1016/S0005-7967(01)00068-7.
Used in: framing — prior VR system for arachnophobia (section 2).

Ref 9. Anderson, Page L.; Price, Matthew; Edwards, Shannan M.;
Obasaju, Mayowa A.; Schmertz, Stefan K.; Zimand, Elana; Calamaras,
Martha R. Virtual reality exposure therapy for social anxiety
disorder: A randomized controlled trial. Journal of Consulting and
Clinical Psychology, Vol. 81, No. 5, pp. 751 to 760, October 2013.
DOI: https://doi.org/10.1037/a0033559.
Used in: framing — prior VR system for social anxiety (section 2).

Ref 10. Slater, Mel; Pertaub, David-Paul; Barker, Chris; Clark,
David M. An Experimental Study on Fear of Public Speaking Using a
Virtual Environment. CyberPsychology and Behavior, Vol. 9, No. 5,
pp. 627 to 633, October 2006. DOI:
https://doi.org/10.1089/cpb.2006.9.627.
Used in: related work — VR public-speaking exposure.

### 13.3 Biofeedback in exposure therapy

Ref 11. Repetto, Claudia; Riva, Giuseppe. From virtual reality to
interreality in the treatment of anxiety disorders.
Neuropsychiatry, Vol. 1, No. 1, pp. 31 to 43, February 2011. DOI:
https://doi.org/10.2217/npy.11.5.
Used in: related work — biofeedback and VR integration prior art.

Ref 12. Cikajlo, Imre; Čižman Staba, Urša; Vrhovac, Suzana;
Larkin, Frank; Roddy, Mark. A Cloud-Based Virtual Reality App for
a Novel Telemindfulness Service: Rationale, Design and Feasibility
Evaluation. JMIR Research Protocols, Vol. 9, No. 11, Article
e19532, November 2020. DOI:
https://doi.org/10.2196/19532.
Used in: related work — cloud-based VR therapy delivery, for
comparison to our server-based framework.

### 13.4 Physiological stress indicators

Ref 13. Kreibig, Sylvia D. Autonomic nervous system activity in
emotion: A review. Biological Psychology, Vol. 84, No. 3, pp. 394
to 421, July 2010. DOI:
https://doi.org/10.1016/j.biopsycho.2010.03.010.
Used in: section 7.1 — background on autonomic response patterns.

Ref 14. Thayer, Julian F.; Åhs, Fredrik; Fredrikson, Mats; Sollers
III, John J.; Wager, Tor D. A meta-analysis of heart rate
variability and neuroimaging studies: implications for heart rate
variability as a marker of stress and health. Neuroscience and
Biobehavioral Reviews, Vol. 36, No. 2, pp. 747 to 756, February
2012. DOI:
https://doi.org/10.1016/j.neubiorev.2011.11.009.
Used in: section 7.1 — HRV as validated stress marker.

Ref 15. Task Force of the European Society of Cardiology and the
North American Society of Pacing and Electrophysiology (Camm,
A. J. and Malik, M., chairs). Heart rate variability: standards of
measurement, physiological interpretation, and clinical use.
European Heart Journal, Vol. 17, No. 3, pp. 354 to 381, March
1996. DOI:
https://doi.org/10.1093/oxfordjournals.eurheartj.a014868.
Used in: sections 5.1, 6, 7.1 — the RMSSD definition, the Malik
20 percent ectopy rule, the recommended window lengths. Primary
methods citation for HRV.

Ref 16. Kim, Hye-Geum; Cheon, Eun-Jin; Bai, Dai-Seg; Lee, Young
Hwan; Koo, Bon-Hoon. Stress and Heart Rate Variability: A
Meta-Analysis and Review of the Literature. Psychiatry
Investigation, Vol. 15, No. 3, pp. 235 to 245, March 2018. DOI:
https://doi.org/10.30773/pi.2017.08.17.
Used in: sections 7.1, 7.3 — inversion of HRV sign for stress
weighting.

Ref 17. Muñoz, María L.; van Roon, Arie; Riese, Harriëtte; Thio,
Chris; Oostenveld, Emil; Westrik, Wim; Snieder, Harold; Nolte,
Ilja M. Validity of (Ultra-)Short Recordings for Heart Rate
Variability Measurements. PLoS ONE, Vol. 10, No. 9, Article
e0138921, September 2015. DOI:
https://doi.org/10.1371/journal.pone.0138921.
Used in: section 5.1 — justifies the 60-second RMSSD window as
practical minimum.

Ref 18. Shaffer, Fred; Ginsberg, J. P. An Overview of Heart Rate
Variability Metrics and Norms. Frontiers in Public Health, Vol. 5,
Article 258, September 2017. DOI:
https://doi.org/10.3389/fpubh.2017.00258.
Used in: sections 5.1, 6 — HRV norms and sigma-floor rationale.

Ref 19. Boucsein, Wolfram. Electrodermal Activity. 2nd edition.
Springer, New York, 2012. ISBN 978-1-4614-1125-3. DOI:
https://doi.org/10.1007/978-1-4614-1126-0.
Used in: sections 5.2, 6 (EDA sigma floor), 7.1 (background on
tonic and phasic separation). Authoritative textbook.

Ref 20. Dawson, Michael E.; Schell, Anne M.; Filion, Diane L. The
Electrodermal System. In Cacioppo, John T.; Tassinary, Louis G.;
Berntson, Gary G. (eds.), Handbook of Psychophysiology, 4th
edition, Cambridge University Press, pp. 217 to 243, 2016. ISBN
978-1-107-05852-1.
Used in: section 5.2 — canonical EDA methods chapter.

Ref 21. Fowles, Don C. The Three Arousal Model: Implications of
Gray's Two-Factor Learning Theory for Heart Rate, Electrodermal
Activity, and Psychopathy. Psychophysiology, Vol. 17, No. 2, pp.
87 to 104, March 1980. DOI:
https://doi.org/10.1111/j.1469-8986.1980.tb00117.x.
Used in: section 5.2 — theoretical background for EDA reflecting
sympathetic branch specifically.

### 13.5 Signal-processing algorithms

Ref 22. Pan, Jiapu; Tompkins, Willis J. A Real-Time QRS Detection
Algorithm. IEEE Transactions on Biomedical Engineering, Vol.
BME-32, No. 3, pp. 230 to 236, March 1985. DOI:
https://doi.org/10.1109/TBME.1985.325532.
Used in: section 5.1 — R-peak detection in the alternative
backend.

Ref 23. Lipponen, Jukka A.; Tarvainen, Mika P. A robust algorithm
for heart rate variability time series artefact correction using
novel beat classification. Journal of Medical Engineering and
Technology, Vol. 43, No. 3, pp. 173 to 181, 2019. DOI:
https://doi.org/10.1080/03091902.2019.1640306.
Used in: section 5.1 — Kubios artifact correction invoked by
`nk.ecg_peaks(correct_artifacts=True)`.

Ref 24. Lippman, N.; Stein, Kenneth M.; Lerman, Bruce B.
Comparison of methods for removal of ectopy in measurement of
heart rate variability. American Journal of Physiology - Heart and
Circulatory Physiology, Vol. 267, No. 1, pp. H411 to H418, July
1994. DOI:
https://doi.org/10.1152/ajpheart.1994.267.1.H411.
Used in: section 5.1 — ectopy removal in the alternative backend.

Ref 25. Makowski, Dominique; Pham, Tam; Lau, Zen J.; Brammer,
Jan C.; Lespinasse, François; Pham, Hung; Schölzel, Christopher;
Chen, S. H. Annabel. NeuroKit2: A Python toolbox for
neurophysiological signal processing. Behavior Research Methods,
Vol. 53, No. 4, pp. 1689 to 1696, August 2021. DOI:
https://doi.org/10.3758/s13428-020-01516-y.
Used in: sections 5.1, 5.2, 10 — the default extraction toolbox.

Ref 26. Kothe, Christian; Medine, David; Boulay, Chadwick;
Grivich, Michael; Stenner, Thomas. The Lab Streaming Layer for
synchronised multi-modal recording. Preprint, 2019. Project page:
https://labstreaminglayer.org. Repository:
https://github.com/sccn/labstreaminglayer.
Used in: sections 3, 4 — network transport layer.

Ref 27. PLUX Wireless Biosignals S.A. biosignalsnotebooks:
Documented Jupyter notebooks demonstrating the use of PLUX
biosignals. Lisbon, Portugal, 2020. Repository:
https://github.com/pluxbiosignals/biosignalsnotebooks.
Used in: sections 3, 5.1, 5.2, 10, 11 — official PLUX package
providing the alternative backend and the ADC conversion
constants.

Ref 28. Greco, Alberto; Valenza, Gaetano; Lanata, Antonio;
Scilingo, Enzo Pasquale; Citi, Luca. cvxEDA: A Convex Optimization
Approach to Electrodermal Activity Processing. IEEE Transactions
on Biomedical Engineering, Vol. 63, No. 4, pp. 797 to 804, April
2016. DOI:
https://doi.org/10.1109/TBME.2015.2474131.
Used in: section 5.2 — the phasic decomposition algorithm.

### 13.6 Multimodal stress detection (context)

Ref 29. Healey, Jennifer A.; Picard, Rosalind W. Detecting Stress
During Real-World Driving Tasks Using Physiological Sensors. IEEE
Transactions on Intelligent Transportation Systems, Vol. 6, No. 2,
pp. 156 to 166, June 2005. DOI:
https://doi.org/10.1109/TITS.2005.848368.
Used in: related work — foundational multimodal stress detection.

Ref 30. Sano, Akane; Picard, Rosalind W. Stress Recognition Using
Wearable Sensors and Mobile Phones. In Proceedings of the 2013
Humaine Association Conference on Affective Computing and
Intelligent Interaction (ACII 2013), IEEE, pp. 671 to 676,
September 2013. DOI:
https://doi.org/10.1109/ACII.2013.117.
Used in: section 7.3 — HR weighting rationale (HR non-specificity
to arousal).

Ref 31. Schmidt, Philip; Reiss, Attila; Duerichen, Robert;
Marberger, Claus; Van Laerhoven, Kristof. Introducing WESAD, a
Multimodal Dataset for Wearable Stress and Affect Detection. In
Proceedings of the 20th ACM International Conference on Multimodal
Interaction (ICMI 2018), ACM, pp. 400 to 408, October 2018. DOI:
https://doi.org/10.1145/3242969.3242985.
Used in: section 7.3, related work — comparison stress-detection
dataset using similar physiological triad.

Ref 32. Alberdi, Ane; Aztiria, Asier; Basarab, Adrian. Towards an
automatic early stress recognition system for office environments
based on multimodal measurements: A Review. Journal of Biomedical
Informatics, Vol. 59, pp. 49 to 75, February 2016. DOI:
https://doi.org/10.1016/j.jbi.2015.11.007.
Used in: related work — survey of physiological stress detection
literature.

### 13.7 Virtual reality immersion and presence

Ref 33. Slater, Mel. Place illusion and plausibility can lead to
realistic behaviour in immersive virtual environments.
Philosophical Transactions of the Royal Society B: Biological
Sciences, Vol. 364, No. 1535, pp. 3549 to 3557, December 2009.
DOI: https://doi.org/10.1098/rstb.2009.0138.
Used in: discussion — theoretical framework for VR presence.

Ref 34. Diemer, Julia; Alpers, Georg W.; Peperkorn, Henrik M.;
Shiban, Youssef; Mühlberger, Andreas. The impact of perception and
presence on emotional reactions: a review of research in virtual
reality. Frontiers in Psychology, Vol. 6, Article 26, January
2015. DOI:
https://doi.org/10.3389/fpsyg.2015.00026.
Used in: discussion — links VR presence to physiological
emotional response.

Ref 35. Bailenson, Jeremy N.; Blascovich, Jim; Beall, Andrew C.;
Loomis, Jack M. Interpersonal Distance in Immersive Virtual
Environments. Personality and Social Psychology Bulletin, Vol. 29,
No. 7, pp. 819 to 833, July 2003. DOI:
https://doi.org/10.1177/0146167203029007002.
Used in: discussion — spatial-behaviour realism in VR, background
for the public-speaking audience scene.

### 13.8 Unity and VR authoring

Ref 36. Unity Technologies. Unity 2022 LTS (Long Term Support).
San Francisco, CA, 2024. Documentation:
https://docs.unity3d.com.
Used in: sections 3, 4.4 — the authoring environment for all VR
scenes.

### 13.9 Prior work at the same lab

Ref 37. Daghooghi and colleagues. Full title and citation pending;
the working file (`Daghooghi_et_al_International_Conference_on_
Research_in_Psychology (1).docx`) is in the references folder.
Ping the lab administrator (Elaheh Bakhtiari) for the final
published citation before submission.
Used in: section 12 — positions the current work as the technical
extension of the earlier experiment conducted at the same lab
without the biofeedback loop.

### 13.10 Cross-reference summary

Quick lookup when the supervisor rearranges the chapter into
different papers.

Introduction and related work: Refs 1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
11, 12, 29, 32, 37.

Hardware and materials: Refs 26, 27, 36.

Architecture: Refs 1, 8, 9 (framing against prior art), 26, 36.

Signal acquisition (HR / HRV / EDA extraction): Refs 15, 17, 18,
19, 20, 21, 22, 23, 24, 25, 27, 28.

Personal calibration: Refs 15, 18, 19.

Stress-index fusion: Refs 1, 13, 14, 15, 16, 19, 30, 31.

Classification: Ref 1.

Backend comparison: Refs 22, 23, 25, 27, 28.

VR presence discussion: Refs 33, 34, 35.

Ethics and data protection (positioning against prior lab work):
Ref 37.

---

## 14. What is not in this chapter

To keep coordination with co-authors clean.

Clinical protocol — number of sessions per participant, order of
exposures, familiarisation runs where the participant inhabits the
scene without stimuli, use of standardised questionnaires
(Acrophobia Questionnaire, Fear of Spiders Questionnaire, Personal
Report of Confidence as a Speaker, and so on), pre and post
session interviews — is decided by the psychology team and appears
in their chapter. Not here.

VR scenes themselves — how the balloon is animated in acrophobia,
how the spider approaches in arachnophobia, how the audience
members enter and look in public speaking, avatar choices, audio
design, visual fidelity, safety protocols for VR-induced nausea —
are the Unity team's contribution and appear in their chapter. Not
here.

Ethics arrangements — approval number, consent form language,
retention policy, right-to-erasure procedure — belong to the
general study section governed by the university Data Protection
Officer. Section 12 above draws on decisions the supervisor and
lab administration have already made and points at the running
questions document for open items.

Results, participant numbers, statistical analysis of exposure
outcomes — belong to the experimental section, which is written
after data collection and does not overlap with the methodology.
Not here.
