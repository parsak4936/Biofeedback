# Unity → Biofeedback telemetry contract

**Audience:** the Unity/C# developer maintaining `BioFeedbackMiddleware.cs`
and adding new phobia scenes to `VR_Phobias`.

**Purpose:** define the one packet format every VR scene sends to the
biofeedback pipeline. Once implemented, the Python side automatically
handles any current or future scenario — no biofeedback code changes
required when a new phobia is added.

---

## The contract in one paragraph

Every VR scene sends one compact **JSON object per UDP datagram** to
`127.0.0.1:5006` at ~10 Hz, containing two keys: `scenario` (a lowercase
string identifying the scene) and `data` (an object of any numeric
fields relevant to that scene). Biofeedback stores everything verbatim
in `samples.csv` (`scenario` and `telemetry_json` columns) and forwards
the same envelope on an LSL string stream for the dashboard's live scene
panel to render.

---

## The packet format

```json
{"scenario": "<name>", "data": {<key>: <value>, ...}}
```

### Rules

| # | Rule | Why |
|---|---|---|
| 1 | **One JSON object per datagram.** No batching. | The UDP receiver treats each packet as one telemetry frame. |
| 2 | **Compact JSON, no pretty-printing, no trailing newline.** | Smaller packets, easier to parse. |
| 3 | **UTF-8 encoding.** | Standard. Matches what the C# `Encoding.UTF8.GetBytes` uses. |
| 4 | **Numbers use `CultureInfo.InvariantCulture` (dot as decimal separator).** | On Italian/French Windows, the default locale writes `1,5` which breaks JSON. |
| 5 | **Send at ~10 Hz.** | Matches `Config.PIPELINE_RATE`. Faster is wasteful; slower shows visible stutter on the dashboard. |
| 6 | **Same UDP port (5006) and host (`127.0.0.1`) for all scenes.** | Configured in the Unity Inspector as `telemetryHost` / `telemetryPort`. |
| 7 | **Payload under 4 KB.** | The receiver's UDP buffer is 4096 bytes. Comfortable room for hundreds of numeric fields. |
| 8 | **Scenario label is lowercase, no spaces, matches `SCENARIOS.md`.** | The dashboard looks up per-scenario configuration by this label. |
| 9 | **Every field is a JSON number, string, or bool** — no nested objects or arrays. | Keeps the CSV / LSL analysis pipeline flat. |

---

## Worked examples per scenario

The full registry lives in [`SCENARIOS.md`](SCENARIOS.md). Summary:

### Acrophobia (heights)

```json
{"scenario": "acrophobia", "data": {"height": 12.345}}
```

- `height` — balloon altitude in metres.

### Arachnophobia (spiders)

```json
{"scenario": "arachnophobia", "data": {"size": 1.5, "count": 3}}
```

- `size` — relative size of the spider currently in view (arbitrary unit; Unity decides the scale).
- `count` — number of spiders currently visible.

Later additions (optional): `distance` (metres to nearest spider), `speed` (m/s), etc. Any new field just appears in the JSON and biofeedback captures it — no Python change needed.

### Fear of Public Speaking

```json
{"scenario": "public_speaking", "data": {"looking": 42, "audience": 100}}
```

- `looking` — number of virtual audience members currently looking at the speaker.
- `audience` — total number of audience members in the room.

Later additions (optional): `questions_asked`, `hostility_level`, etc. Same rule — free-form additive.

---

## C# reference implementation

Drop-in replacement for `SendHeightTelemetry(float)` in
`Assets/Scripts/Acrophobia/BioFeedbackMiddleware.cs`. Note this uses no
third-party JSON library — Unity's built-in `JsonUtility` does not
support `Dictionary`, so the string is built manually. Small enough
that all three scenes can share this one method.

```csharp
using System.Collections.Generic;
using System.Globalization;
using System.Text;

public void SendTelemetry(string scenario, Dictionary<string, float> data)
{
    if (!enableTelemetry) return;

    float minInterval = telemetryRateHz > 0f ? 1f / telemetryRateHz : 0f;
    if (Time.time - lastTelemetrySendTime < minInterval) return;
    lastTelemetrySendTime = Time.time;

    if (telemetryClient == null) telemetryClient = new UdpClient();

    // Build the compact JSON envelope by hand. InvariantCulture is
    // mandatory so numbers use '.' as the decimal separator on every
    // Windows locale (Italian defaults to ',').
    var sb = new StringBuilder(128);
    sb.Append("{\"scenario\":\"").Append(scenario).Append("\",\"data\":{");
    bool first = true;
    foreach (var kv in data)
    {
        if (!first) sb.Append(',');
        first = false;
        sb.Append('"').Append(kv.Key).Append("\":");
        sb.Append(kv.Value.ToString("F3", CultureInfo.InvariantCulture));
    }
    sb.Append("}}");

    try
    {
        byte[] bytes = Encoding.UTF8.GetBytes(sb.ToString());
        telemetryClient.Send(bytes, bytes.Length, telemetryHost, telemetryPort);
    }
    catch (System.Exception e)
    {
        Debug.LogWarning($"Telemetry send failed: {e.Message}");
    }
}
```

Each scene invokes it with its own scenario label + relevant fields:

```csharp
// Acrophobia scene
middleware.SendTelemetry("acrophobia", new Dictionary<string, float> {
    { "height", currentAltitude }
});

// Arachnophobia scene
middleware.SendTelemetry("arachnophobia", new Dictionary<string, float> {
    { "size",  spider.transform.localScale.x },
    { "count", visibleSpiderCount }
});

// Fear of Public Speaking scene
middleware.SendTelemetry("public_speaking", new Dictionary<string, float> {
    { "looking",  npcController.NumberLookingAtPlayer },
    { "audience", npcController.TotalAudienceSize }
});
```

See [`scripts/unity_acrophobia_patch.md`](scripts/unity_acrophobia_patch.md)
for a step-by-step diff you can apply to `BioFeedbackMiddleware.cs`
without breaking the existing Acrophobia flight controller.

---

## What biofeedback stores per row

The operator picks the scenario in the intake dropdown at session
start. That choice determines the **dynamic columns** appended to
`samples.csv` between `s_t` and `artifacts_eda`:

| Scenario picked at intake | samples.csv dynamic columns |
|---|---|
| `acrophobia` | `height` |
| `arachnophobia` | `size`, `count` |
| `public_speaking` | `looking`, `audience` |

For example the Acrophobia header ends with:

    ...s_t,height,artifacts_eda,artifacts_hr,artifacts_hrv

and for Public Speaking:

    ...s_t,looking,audience,artifacts_eda,artifacts_hr,artifacts_hrv

Each row lifts the values straight out of the `data` object Unity
sends. Fields that were absent from a given packet become empty
cells; extra fields Unity sends that are not registered for the
scenario are dropped with a one-time console warning.

Reading in Python is dead simple — the columns are plain numeric:

```python
import pandas as pd
df = pd.read_csv("samples.csv")
df["height"].plot()          # Acrophobia
df[["size", "count"]].plot() # Arachnophobia
```

No JSON parsing required.

**Legacy note:** old Acrophobia sessions (from before the intake
dropdown existed) still contain a `height_m` column instead of
`height`. session_review detects this and renders them correctly.

---

## Two LSL streams

Biofeedback publishes two LSL outlets on the local machine:

1. **`Biofeedback_State`** — 25-channel float32 stream at 10 Hz.
   Physiology, session state, thresholds, Unity command stats. Channel
   24 carries `height_m` (Acrophobia only, NaN otherwise) for backward
   compatibility. Any external LSL consumer already using this stream
   continues to work.

2. **`Biofeedback_Telemetry`** — 1-channel string stream at 10 Hz.
   Each sample is a JSON envelope identical in shape to the incoming
   Unity packet. The dashboard's scene panel subscribes to this; any
   external tool can too. Empty string when the receiver has no fresh
   packet.

---

## Testing without changing the Unity project

Biofeedback ships a Python simulator that plays the role of Unity on
UDP 5006. Run the biofeedback pipeline as usual, then in a second
terminal run:

```bash
python scripts/telemetry_simulator.py acrophobia
python scripts/telemetry_simulator.py arachnophobia
python scripts/telemetry_simulator.py public_speaking
```

The simulator generates plausible telemetry at 10 Hz for the chosen
scene. The dashboard's scene panel populates in real time as if a real
Unity build were sending packets. Use this to verify a new scenario
end-to-end before your teammate ships the C# side.

---

## Common mistakes to avoid on the Unity side

- **Do NOT** send more than 4096 bytes per packet.
- **Do NOT** use `.ToString()` without `CultureInfo.InvariantCulture`.
- **Do NOT** pretty-print the JSON or add trailing newlines.
- **Do NOT** send nested objects or arrays inside `data` — flat values only.
- **Do NOT** send the packet on Update() without a rate limiter — 10 Hz is enough; 60+ Hz overwhelms the receiver.
- **Do NOT** reuse the same scenario label for two different scenes — the dashboard's chart title and top-bar card follow the label.

---

## One-line summary

> Send `{"scenario":"<name>","data":{...flat numeric fields...}}` on
> UDP `127.0.0.1:5006` at 10 Hz. Biofeedback records everything verbatim
> and renders the live scene panel automatically.
