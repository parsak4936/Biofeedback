# Scenario registry

The list of VR phobia scenarios biofeedback currently understands, and
what telemetry fields each one is expected to emit from Unity. Adding a
new scenario is a **one-place change** — add an entry to
`Config.SCENARIO_FIELDS` — and the intake dropdown, the samples.csv
header, the dashboard scene panel, and session_review all pick it up
automatically.

The physiology stack (EDA / HR / HRV → stress index) is fully
scenario-agnostic. This document only covers the extra per-scene
telemetry Unity streams to biofeedback.

For the transport spec (JSON envelope, UDP port, rate, encoding rules)
see [`UNITY_TELEMETRY_CONTRACT.md`](UNITY_TELEMETRY_CONTRACT.md).

## How samples.csv columns are chosen

The scenario is **selected at intake time** from the "Scenario" dropdown
in the patient intake form. Once chosen, the CSV header is fixed for
the whole session:

    ...s_t,<scenario field 1>,<scenario field 2>,...,artifacts_eda,...

For Acrophobia this is:

    ...s_t,height,artifacts_eda,artifacts_hr,artifacts_hrv

For Arachnophobia:

    ...s_t,size,count,artifacts_eda,artifacts_hr,artifacts_hrv

For Public Speaking:

    ...s_t,looking,audience,artifacts_eda,artifacts_hr,artifacts_hrv

Fields Unity sends that are NOT in the scenario's registered field list
are silently dropped (a one-time console warning per unknown field name
tells the operator to update the registry). Fields Unity fails to send
land as empty cells (pandas / Excel read those as NaN).

---

## Currently registered scenarios

### `acrophobia` (fear of heights)

**Unity scene:** `F:/VR_Phobias/Assets/Scenes/Acrophobia.unity`
**Middleware:** `Assets/Scripts/Acrophobia/BioFeedbackMiddleware.cs`

| Field | Type | Unit | Description |
|---|---|---|---|
| `height` | float | metres | Balloon altitude above ground. Range 0 to ~150 m. |

**Primary field for the scene panel chart:** `height` (metres).

**Example packet:**
```json
{"scenario": "acrophobia", "data": {"height": 12.345}}
```

**Notes:**
- Backward-compatible with legacy `"height,N"` recordings — the
  `height_m` column in `samples.csv` is still populated so old analysis
  code keeps working.
- Physiologically expected pattern: EDA + HR rise as altitude
  increases; return to baseline as the balloon descends.

---

### `arachnophobia` (fear of spiders)

**Unity scene:** `F:/VR_Phobias/Assets/Scenes/Arachnophobia.unity`
**Middleware:** *(to be created — same pattern as Acrophobia's)*

| Field | Type | Unit | Description |
|---|---|---|---|
| `size` | float | relative units | Size of the spider currently in view. Unity decides the scale — bigger number = bigger spider. |
| `count` | integer | count | Number of spiders currently visible to the participant. |

**Primary field for the scene panel chart:** `count` (number of spiders in view).

**Example packet:**
```json
{"scenario": "arachnophobia", "data": {"size": 1.5, "count": 3}}
```

**Optional future fields** (Unity can add them any time; biofeedback
just needs `SCENARIO_PRIMARY_FIELD` updated to switch which one drives
the chart):

- `distance` — distance in metres from participant to nearest spider.
- `speed` — locomotion speed of the fastest visible spider (m/s).

---

### `public_speaking` (fear of public speaking / glossophobia)

**Unity scene:** `F:/VR_Phobias/Assets/Scenes/Fear of Public Speaking.unity`
**Middleware:** *(to be created — same pattern as Acrophobia's)*

| Field | Type | Unit | Description |
|---|---|---|---|
| `looking` | integer | count | Number of virtual audience members currently looking directly at the speaker. |
| `audience` | integer | count | Total number of audience members in the room. |

**Primary field for the scene panel chart:** `looking` (audience members currently staring).

**Example packet:**
```json
{"scenario": "public_speaking", "data": {"looking": 42, "audience": 100}}
```

**Optional future fields:**

- `questions_asked` — running count of questions the audience has posed.
- `hostility_level` — 0-1 float describing crowd mood.

---

## Adding a new scenario

You want to add e.g. `claustrophobia` (fear of small spaces).

**Unity side (your teammate):**

1. Build the Unity scene as usual.
2. Add or reuse `BioFeedbackMiddleware` and call `SendTelemetry(...)`
   with `scenario = "claustrophobia"` and whatever numeric fields the
   scene tracks (e.g. `room_volume_m3`, `wall_distance_m`).
3. Follow the packet rules in [`UNITY_TELEMETRY_CONTRACT.md`](UNITY_TELEMETRY_CONTRACT.md).

**Biofeedback side (one file, three dicts):**

Edit `src/config.py` and add one entry to each of these dicts:

```python
SCENARIO_FIELDS = {
    'acrophobia':      ['height'],
    'arachnophobia':   ['size', 'count'],
    'public_speaking': ['looking', 'audience'],
    'claustrophobia':  ['wall_distance_m', 'room_volume_m3'],   # ← new
}

SCENARIO_PRIMARY_FIELD = {
    'acrophobia':      'height',
    'arachnophobia':   'count',
    'public_speaking': 'looking',
    'claustrophobia':  'wall_distance_m',   # ← new (which one to plot)
}

SCENARIO_PRIMARY_UNIT = {
    'acrophobia':      'm',
    'arachnophobia':   '',
    'public_speaking': '',
    'claustrophobia':  'm',                 # ← new (unit label)
}
```

That's it. The intake dropdown offers the new scenario, samples.csv
gets `wall_distance_m` + `room_volume_m3` columns for that session,
the dashboard scene panel renders the values live, and session_review
plots the primary field.

**Optional:** add a section to this document (`SCENARIOS.md`) listing
the fields your new scene emits, so future contributors know what to
expect.

---

## Field naming conventions

- **Lowercase, underscore-separated**: `wall_distance_m`, not
  `WallDistanceM`. Matches Python conventions and stays readable in
  CSV headers.
- **Include the unit as a suffix when the field is dimensionful and
  the unit is not obvious from the name**: `wall_distance_m` (metres),
  `looking_pct` (percentage). Skip the suffix when the name already
  implies it (`height` reads as metres, `count` reads as integer).
- **Do not repeat the scenario name in the field**: `size`, not
  `spider_size` — the scenario is already in the envelope.
- **Prefer flat numeric fields over nested objects**: biofeedback
  stores whatever is under `data` as flat CSV / JSON. Nested objects
  work but are harder to read in Excel.

---

## Deprecated / legacy formats

- **`"height,N.NNN"` plain-text packets** — used by the original
  Acrophobia scene. No longer accepted by the receiver as of 2026-07.
  The Unity scene must be updated to send the JSON envelope before
  running against the current biofeedback pipeline. See
  [`scripts/unity_acrophobia_patch.md`](scripts/unity_acrophobia_patch.md)
  for the exact code change.
