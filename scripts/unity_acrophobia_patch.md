# Unity patch — teach the Acrophobia scene to emit JSON telemetry

Step-by-step edit for `F:/VR_Phobias/Assets/Scripts/Acrophobia/BioFeedbackMiddleware.cs`,
so the existing Acrophobia scene stops sending the legacy `"height,N"`
plain-text packet and starts sending the JSON envelope biofeedback now
expects. Apply this once; the Arachnophobia and Public Speaking scenes
can reuse the same helper method verbatim.

**Prerequisite:** biofeedback (Python) is already on the JSON-only
receiver. If you run the old `"height,N"` Unity build against the new
Python side, telemetry will silently do nothing (parser rejects the
packet). This patch closes the loop.

You do not need to know C# to apply this — the edit is one method
replacement plus two very small changes. Copy/paste the exact blocks
below into any text editor (Rider, Visual Studio, VS Code, even
Notepad).

---

## Change 1 — replace `SendHeightTelemetry`

**File:** `F:\VR_Phobias\Assets\Scripts\Acrophobia\BioFeedbackMiddleware.cs`

**Find** (lines 71 – 96 in the current file):

```csharp
public void SendHeightTelemetry(float heightMeters)
{
    if (!enableHeightTelemetry) return;

    float minInterval = telemetryRateHz > 0f ? 1f / telemetryRateHz : 0f;
    if (Time.time - lastTelemetrySendTime < minInterval) return;

    lastTelemetrySendTime = Time.time;
    if (telemetryClient == null) telemetryClient = new UdpClient();

    string payload = string.Format(
        CultureInfo.InvariantCulture,
        "height,{0:F3}",
        heightMeters
    );

    try
    {
        byte[] data = Encoding.UTF8.GetBytes(payload);
        telemetryClient.Send(data, data.Length, telemetryHost, telemetryPort);
    }
    catch (System.Exception e)
    {
        Debug.LogWarning($"Height telemetry send failed: {e.Message}");
    }
}
```

**Replace with:**

```csharp
public void SendHeightTelemetry(float heightMeters)
{
    // Thin wrapper so existing callers (e.g. AcrophobiaBalloonFlight-
    // Controller) do not need to change. Delegates to the generic
    // SendTelemetry with the acrophobia scenario label.
    SendTelemetry("acrophobia", new System.Collections.Generic.Dictionary<string, float>
    {
        { "height", heightMeters }
    });
}

public void SendTelemetry(string scenario,
                          System.Collections.Generic.Dictionary<string, float> data)
{
    if (!enableHeightTelemetry) return;

    float minInterval = telemetryRateHz > 0f ? 1f / telemetryRateHz : 0f;
    if (Time.time - lastTelemetrySendTime < minInterval) return;

    lastTelemetrySendTime = Time.time;
    if (telemetryClient == null) telemetryClient = new UdpClient();

    // Build the compact JSON envelope by hand. Unity's JsonUtility does
    // not support Dictionary, so we assemble the string manually.
    // InvariantCulture is mandatory so '.' is the decimal separator on
    // every Windows locale (Italian would otherwise write '1,5' which
    // breaks JSON parsing on the Python side).
    var sb = new System.Text.StringBuilder(128);
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

**What this does:**
- The existing `SendHeightTelemetry(float)` still works exactly as
  before from every caller's point of view (the balloon flight
  controller does not need to be touched).
- Internally it delegates to a new, generic `SendTelemetry(scenario,
  dict)` method that emits the JSON envelope on the wire.
- `SendTelemetry(...)` is reusable — Arachnophobia and Public Speaking
  scenes can call it directly once their controllers are ready.

---

## Change 2 — nothing else needs to change

The `enableHeightTelemetry`, `telemetryHost`, `telemetryPort`, and
`telemetryRateHz` fields at the top of the class stay exactly as they
are. No Inspector changes are required — everything you see under the
"Telemetry" header in the Unity Inspector still works and controls the
new JSON path.

---

## Verification

1. Save `BioFeedbackMiddleware.cs` and let Unity recompile.
2. Enter Play mode with the Acrophobia scene loaded.
3. Move the balloon up (arrow keys, or via biofeedback commands).
4. In a separate terminal, run:
   ```
   python -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('127.0.0.1', 5006)); [print(s.recvfrom(4096)[0]) for _ in range(5)]"
   ```
5. You should see five JSON packets printed:
   ```
   b'{"scenario":"acrophobia","data":{"height":12.345}}'
   b'{"scenario":"acrophobia","data":{"height":13.421}}'
   ...
   ```
6. If you see the old `b'height,12.345'` instead, Unity did not
   recompile — try File → Save Project + let the editor rebuild.

7. Once verified, run the biofeedback pipeline (run.bat) as usual.
   The dashboard's scene panel should display "Scene: ACROPHOBIA"
   with the live height value and chart.

**Note:** step 4 only works if biofeedback (Python) is NOT running,
because both processes would want to bind UDP 5006. Stop biofeedback
first, then run the netcat/python test, then restart biofeedback.

---

## Extending to Arachnophobia and Public Speaking

Once `BioFeedbackMiddleware` has the `SendTelemetry(scenario, data)`
method, the other scenes call it from their own controllers with
their own scenario label + fields. See
[`SCENARIOS.md`](../SCENARIOS.md) for the field names each scenario
should use.

Example: an Arachnophobia controller would call

```csharp
middleware.SendTelemetry("arachnophobia",
    new Dictionary<string, float> {
        { "size",  spider.transform.localScale.x },
        { "count", visibleSpiderCount }
    });
```

on every Update — the middleware's built-in rate limiter (via
`telemetryRateHz`) throttles the actual UDP sends to 10 Hz regardless
of Update's framerate.

---

## Rollback

If you want to revert to the old plain-text `"height,N"` format for
some reason, delete the two new methods and paste the original
`SendHeightTelemetry(float)` (from the "Find" block at the top of this
document) back into `BioFeedbackMiddleware.cs`. On the biofeedback
side, you would need to re-add the legacy text parser to
`src/telemetry_receiver.py::_parse_packet` — see the git history of
that file for the exact code.
