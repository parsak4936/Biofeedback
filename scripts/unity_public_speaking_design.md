# Fear of Public Speaking — Unity scene design

Design doc + drop-in C# scripts for the audience scene. Complements
`unity_acrophobia_patch.md`. Biofeedback (Python) does not need any
changes — it already receives whatever `{"scenario":"public_speaking",
"data":{...}}` this scene emits.

## Concept — three decoupling layers

Biofeedback commands are **discrete** (one packet per second). The scene
should feel **continuous**. Direct one-to-one triggering causes visible
twitching (people oscillating between looking up and down every second).
The fix is three layers between commands and animations:

```
UDP command                (fast: 1 Hz)
     │
     ▼
intensityScore (int)      absorbs multiple commands into a target load
     │
     ▼
reaction cooldown         one scene event every N seconds max
     │
     ▼
weighted random pick      choose which person, with bias
     │
     ▼
person state machine      per-person minStateHoldSec prevents oscillation
```

## Person state machine

```
    Idle
      │ (spawned, walks to seat)
      ▼
    Sitting  ◄────────┐
      │  (increase)   │  (decrease)
      ▼               │
    LookingAtPlayer ──┘
      │
      │ (increase, rare)
      ▼
    StandingUp    ◄── NEVER re-recruited by decrease
      │
      ▼
    WalkingOut    ◄── NEVER re-recruited by decrease
      │
      ▼
    Despawned
```

**Touchable states** (biofeedback commands can affect them):
`Sitting`, `LookingAtPlayer`

**Immutable states** (transient — biofeedback ignores them):
`Idle`, `StandingUp`, `WalkingOut`, `Despawned`

**Per-person hold time**: a person that just entered `LookingAtPlayer`
must stay there at least `minStateHoldSec` (e.g. 5–8 s) before a
`decrease` can move them back. This solves the "60 look-ups per minute"
problem entirely.

## Weighted random pick

When a command fires and the cooldown is free, the controller filters
the audience to touchable persons past their min-hold, then picks one
weighted-random. Weights are tunable in the Inspector on each person:

| Weight | For | Suggested |
|---|---|---|
| `weightBaseline` | Any pick | 1.0 (equal chance) |
| `weightWhenClose` | Close-to-player picks | 2.0 (front rows react first) |
| `weightRecentlyChanged` | Avoid ping-pong on the same person | 0.3 |

The controller normalises the weights and picks one with
`Random.value * totalWeight`. Non-touchable persons contribute 0.

## Reaction cooldown

Two cooldowns keep everything smooth:

- **Scene cooldown** (`sceneReactionCooldownSec`, default 3 s) — the
  audience as a whole reacts at most this often, no matter how many
  biofeedback commands arrive.
- **Per-person cooldown** (`personReactionCooldownSec`, default 5 s) —
  the same person can't be picked twice in this window.

If biofeedback fires 5 `increase` commands in 5 seconds during a scene
cooldown, the intensity score climbs to 5. When the cooldown expires,
the controller compares intensity to the current effective audience
load and takes ONE step in the right direction. Excess intensity stays
banked for the next tick.

## Bounds

- `minAudience` (default 3) — never leaves the room emptier than this.
  If `decrease` arrives and only `minAudience` remain, the command is
  reinterpreted as "look away" instead of "leave".
- `maxAudience` (default 40) — never spawns more than this. If
  `increase` arrives and everyone's already in, reinterpret as "look
  up" instead of "spawn new".

## Telemetry back to biofeedback

Each Unity tick, `AudienceController.SendPublicSpeakingTelemetry()`
sends the packet biofeedback expects:

```json
{"scenario": "public_speaking",
 "data": {"looking": <N looking at player right now>,
          "audience": <N present, i.e. not despawned>}}
```

Both are integers. Any future scenes add / remove fields freely — the
Python side captures whatever `data` fields arrive.

---

## Script 1 — `AudienceMember.cs`

Attach one instance to every audience person prefab. Handles that
person's state, animations, movement, and cooldowns. Uses only
built-in Unity APIs.

```csharp
using UnityEngine;

public class AudienceMember : MonoBehaviour
{
    public enum State { Idle, Sitting, LookingAtPlayer, StandingUp, WalkingOut, Despawned }

    [Header("Setup")]
    public Animator animator;                    // must expose the trigger names below
    public Transform seat;                       // where this person sits
    public Transform doorway;                    // where this person enters / leaves
    public Transform playerHead;                 // reference the VR camera transform

    [Header("Movement")]
    public float walkSpeed = 1.2f;               // m/s
    public float rotateSpeed = 180f;             // deg/s

    [Header("Timing")]
    public float minStateHoldSec = 5f;           // don't allow a re-trigger before this
    public float personReactionCooldownSec = 5f; // controller ignores this person for this long after a change

    [Header("Weighting")]
    public float weightBaseline = 1f;
    public float weightWhenClose = 2f;
    public float weightRecentlyChanged = 0.3f;

    // Animator trigger names — must match the Animator Controller you build in Unity.
    const string TRIG_SIT   = "TrigSit";
    const string TRIG_LOOK_UP = "TrigLookUp";
    const string TRIG_LOOK_DOWN = "TrigLookDown";
    const string TRIG_STAND = "TrigStand";
    const string TRIG_WALK  = "TrigWalk";

    public State CurrentState { get; private set; } = State.Idle;
    float stateEnteredAt;
    float lastPickedAt;

    void Awake()
    {
        stateEnteredAt = -999f;
        lastPickedAt = -999f;
        // Everyone starts at the doorway and walks to their seat.
        transform.position = doorway.position;
        transform.rotation = doorway.rotation;
    }

    void OnEnable()
    {
        // Auto-start the walk-in animation as soon as the person is enabled.
        StartCoroutine(WalkTo(seat.position, onArrive: () => {
            transform.rotation = seat.rotation;
            EnterState(State.Sitting);
            if (animator) animator.SetTrigger(TRIG_SIT);
        }));
    }

    // ---------- Controller-facing API ----------

    public bool CanBeTouched()
    {
        if (CurrentState != State.Sitting && CurrentState != State.LookingAtPlayer)
            return false;
        if (Time.time - stateEnteredAt < minStateHoldSec)
            return false;
        if (Time.time - lastPickedAt < personReactionCooldownSec)
            return false;
        return true;
    }

    public float WeightForPick(float distanceToPlayer, float closeThreshold = 3f)
    {
        float w = weightBaseline;
        if (distanceToPlayer < closeThreshold) w *= weightWhenClose;
        if (Time.time - lastPickedAt < personReactionCooldownSec * 2f) w *= weightRecentlyChanged;
        return w;
    }

    public void ApplyIncrease()
    {
        // Sitting → look up. Looking → stand up + leave (rare).
        if (CurrentState == State.Sitting)
        {
            EnterState(State.LookingAtPlayer);
            if (animator) animator.SetTrigger(TRIG_LOOK_UP);
        }
        else if (CurrentState == State.LookingAtPlayer)
        {
            // Escalation branch: person gets up angrily. Rare enough that
            // the controller usually picks somebody else, but handled.
            EnterState(State.StandingUp);
            if (animator) animator.SetTrigger(TRIG_STAND);
            StartCoroutine(WalkOutAfterDelay(1.2f));
        }
    }

    public void ApplyDecrease()
    {
        // Looking → sit + look down. Sitting → stand up and leave.
        if (CurrentState == State.LookingAtPlayer)
        {
            EnterState(State.Sitting);
            if (animator) animator.SetTrigger(TRIG_LOOK_DOWN);
        }
        else if (CurrentState == State.Sitting)
        {
            EnterState(State.StandingUp);
            if (animator) animator.SetTrigger(TRIG_STAND);
            StartCoroutine(WalkOutAfterDelay(1.2f));
        }
    }

    public bool IsLookingAtPlayer => CurrentState == State.LookingAtPlayer;
    public bool IsPresent => CurrentState != State.Despawned && CurrentState != State.Idle;

    // ---------- Internals ----------

    void EnterState(State s)
    {
        CurrentState = s;
        stateEnteredAt = Time.time;
        lastPickedAt = Time.time;
    }

    System.Collections.IEnumerator WalkOutAfterDelay(float delay)
    {
        yield return new WaitForSeconds(delay);
        EnterState(State.WalkingOut);
        if (animator) animator.SetTrigger(TRIG_WALK);
        yield return WalkTo(doorway.position, onArrive: () => {
            EnterState(State.Despawned);
            gameObject.SetActive(false);
        });
    }

    System.Collections.IEnumerator WalkTo(Vector3 target, System.Action onArrive)
    {
        if (animator) animator.SetTrigger(TRIG_WALK);
        while (Vector3.Distance(transform.position, target) > 0.05f)
        {
            Vector3 dir = (target - transform.position);
            dir.y = 0f;
            if (dir.sqrMagnitude > 0.0001f)
            {
                Quaternion targetRot = Quaternion.LookRotation(dir);
                transform.rotation = Quaternion.RotateTowards(
                    transform.rotation, targetRot, rotateSpeed * Time.deltaTime);
            }
            transform.position = Vector3.MoveTowards(
                transform.position, target, walkSpeed * Time.deltaTime);
            yield return null;
        }
        onArrive?.Invoke();
    }
}
```

---

## Script 2 — `AudienceController.cs`

Attach one instance to a manager GameObject in the scene. Holds the
pool of persons, listens to biofeedback commands, applies the
intensity → reaction pipeline, sends telemetry back.

```csharp
using System.Collections.Generic;
using System.Linq;
using UnityEngine;

public class AudienceController : MonoBehaviour
{
    [Header("Wiring")]
    public BioFeedbackMiddleware middleware;   // for SendTelemetry
    public Transform playerHead;
    public AudienceMember[] roster;            // fill with every seat's person (active + inactive)

    [Header("Bounds")]
    public int minAudience = 3;
    public int maxAudience = 40;
    public int startingAudience = 8;

    [Header("Reaction")]
    public float sceneReactionCooldownSec = 3f;
    public float closeThreshold = 3f;          // metres → "front row"

    [Header("Telemetry")]
    public float telemetryRateHz = 10f;

    int intensity;                             // banked commands: + increase, - decrease
    float lastReactionAt = -999f;
    float lastTelemetryAt = -999f;

    void Start()
    {
        // Turn on the initial audience.
        for (int i = 0; i < roster.Length; i++)
            roster[i].gameObject.SetActive(i < startingAudience);
    }

    // Public entry points — call these from BioFeedbackMiddleware's
    // command dispatch, next to where "increase" / "decrease" are
    // currently handled for the balloon altitude.
    public void OnCommandIncrease() { intensity++; }
    public void OnCommandDecrease() { intensity--; }

    void Update()
    {
        // ---- Reaction layer: at most one event per cooldown ----
        if (Time.time - lastReactionAt >= sceneReactionCooldownSec && intensity != 0)
        {
            if (intensity > 0)
            {
                if (TryEscalate()) { intensity--; lastReactionAt = Time.time; }
                else intensity = 0;         // capped — drop the bank
            }
            else
            {
                if (TryDeescalate()) { intensity++; lastReactionAt = Time.time; }
                else intensity = 0;
            }
        }

        // ---- Telemetry back to biofeedback ----
        float dt = telemetryRateHz > 0f ? 1f / telemetryRateHz : 0f;
        if (Time.time - lastTelemetryAt >= dt && middleware != null)
        {
            lastTelemetryAt = Time.time;
            middleware.SendTelemetry("public_speaking",
                new Dictionary<string, float> {
                    { "looking",  CountLooking() },
                    { "audience", CountPresent() },
                });
        }
    }

    // ---------- Escalation / de-escalation ----------

    bool TryEscalate()
    {
        // 1. If under maxAudience and there is an unused roster slot, prefer
        //    spawning a new person (audience grows visibly).
        var sleepers = roster.Where(m => !m.gameObject.activeSelf).ToArray();
        int present = CountPresent();
        if (present < maxAudience && sleepers.Length > 0)
        {
            sleepers[Random.Range(0, sleepers.Length)].gameObject.SetActive(true);
            return true;
        }
        // 2. Otherwise pick a sitting person to look up.
        var picked = PickWeighted(m => m.CurrentState == AudienceMember.State.Sitting);
        if (picked != null) { picked.ApplyIncrease(); return true; }
        return false;
    }

    bool TryDeescalate()
    {
        int present = CountPresent();
        // 1. Prefer softening a looking person back to the book.
        var picked = PickWeighted(m => m.CurrentState == AudienceMember.State.LookingAtPlayer);
        if (picked != null) { picked.ApplyDecrease(); return true; }
        // 2. Otherwise, if above minAudience, send a sitter home.
        if (present > minAudience)
        {
            picked = PickWeighted(m => m.CurrentState == AudienceMember.State.Sitting);
            if (picked != null) { picked.ApplyDecrease(); return true; }
        }
        return false;
    }

    AudienceMember PickWeighted(System.Func<AudienceMember, bool> stateFilter)
    {
        var candidates = new List<(AudienceMember m, float w)>();
        float total = 0f;
        foreach (var m in roster)
        {
            if (!m.gameObject.activeSelf) continue;
            if (!stateFilter(m)) continue;
            if (!m.CanBeTouched()) continue;
            float dist = playerHead != null ?
                Vector3.Distance(m.transform.position, playerHead.position) : 999f;
            float w = m.WeightForPick(dist, closeThreshold);
            if (w > 0f)
            {
                candidates.Add((m, w));
                total += w;
            }
        }
        if (candidates.Count == 0 || total <= 0f) return null;
        float r = Random.value * total;
        float acc = 0f;
        foreach (var (m, w) in candidates)
        {
            acc += w;
            if (r <= acc) return m;
        }
        return candidates[candidates.Count - 1].m;
    }

    int CountLooking()
    {
        int n = 0;
        foreach (var m in roster) if (m.gameObject.activeSelf && m.IsLookingAtPlayer) n++;
        return n;
    }
    int CountPresent()
    {
        int n = 0;
        foreach (var m in roster) if (m.gameObject.activeSelf && m.IsPresent) n++;
        return n;
    }
}
```

---

## Wiring inside `BioFeedbackMiddleware.cs`

The middleware already receives commands from biofeedback (see the
existing `ProcessCommands()` in `BioFeedbackMiddleware.cs`). Add a
reference to `AudienceController` and route the commands to it in
addition to the balloon. Only one scene runs at a time, so a null-check
is enough.

```csharp
// Somewhere near the top of BioFeedbackMiddleware.cs:
public AudienceController audienceController;    // drag the manager GameObject in the Inspector

// Inside ProcessCommands(), replace the acrophobia-only block:
if (cmd == "increase")
{
    // Acrophobia
    targetAltitude = Mathf.Min(targetAltitude + stepAmount, maxAltitude);
    // Public speaking
    if (audienceController != null) audienceController.OnCommandIncrease();
}
else if (cmd == "decrease")
{
    targetAltitude = Mathf.Max(targetAltitude - stepAmount, minAltitude);
    if (audienceController != null) audienceController.OnCommandDecrease();
}
```

The same middleware works for every scene: assign either
`audienceController` OR the acrophobia altitude, whichever the loaded
scene has. `SendTelemetry(scenario, data)` is called by whichever
controller is active.

---

## Unity setup — step by step for a non-Unity-expert

1. **Get free character models + animations.** Sign up (free) at
   [mixamo.com](https://www.mixamo.com/), download 5–10 characters with
   these Mixamo animations rigged to each: `Idle Sitting`,
   `Look Up`, `Look Down`, `Standing Up`, `Walking`, `Walking In Circle`.
   You only need one Idle Sitting per character; the transitions
   (LookUp / LookDown) are separate clips.

2. **Build one AudienceMember prefab.** In Unity:
   - Drag a Mixamo character into the scene, wire it up as a prefab.
   - Add an `Animator` component with a new `Animator Controller` asset.
   - In the Animator Controller, create states: `Sitting`, `LookingUp`,
     `LookingAtPlayer`, `LookingDown`, `Standing`, `Walking`. Draw
     transitions with **trigger parameters** named exactly as in the
     script (`TrigSit`, `TrigLookUp`, `TrigLookDown`, `TrigStand`,
     `TrigWalk`). Set default state to `Sitting`.
   - Add the `AudienceMember` script component. Drag the character's
     `Animator` into the `animator` slot.

3. **Place seats + doorway.** Create empty GameObjects named `Seat_01`,
   `Seat_02`, …, `Seat_40` at each chair position. Create one empty
   GameObject named `Doorway` at the entry point.

4. **Instance the prefab per seat.** Duplicate the prefab 40 times,
   drag each into a seat position. On each instance, drag the matching
   `Seat_XX` transform into the `seat` slot and the `Doorway`
   transform into the `doorway` slot. Drag the VR camera transform
   into every instance's `playerHead` slot.

5. **Add the manager GameObject.** Create an empty GameObject named
   `AudienceManager`. Add the `AudienceController` script. In the
   Inspector:
   - Drag `BioFeedbackMiddleware` into `middleware`.
   - Drag the VR camera into `playerHead`.
   - Drag every audience-member GameObject into the `roster` array
     (Unity has a "lock inspector + drag multiple" trick — select all
     40 in the Hierarchy and drag into the array slot in one go).

6. **Wire the middleware.** Open `BioFeedbackMiddleware.cs`, add the
   `audienceController` field, and route `increase` / `decrease`
   commands as shown above. Drag the `AudienceManager` GameObject into
   the middleware's `audienceController` slot in the Inspector.

7. **Set up baseline size.** On the `AudienceController` component,
   set `Starting Audience = 8` (or whatever fits your scene). The
   first 8 roster slots activate at start; the rest sleep until
   `increase` grows the audience.

That's it. No packages, no NavMesh, no scripting outside these two
files. If a NavMesh IS available in the scene you can later swap
`Vector3.MoveTowards` for `NavMeshAgent.SetDestination` for smarter
paths around obstacles, but the built-in `MoveTowards` works fine for
straight aisle → seat paths.

---

## Testing without biofeedback running

For quick Unity-side testing, add these hotkeys to `AudienceController.Update()`:

```csharp
if (Input.GetKeyDown(KeyCode.RightArrow)) OnCommandIncrease();
if (Input.GetKeyDown(KeyCode.LeftArrow))  OnCommandDecrease();
```

Now pressing → in Play mode fires an `increase` and pressing ← fires a
`decrease`, without needing the full biofeedback + PLUX stack running.
When you commit the scene, remove the hotkeys or gate them behind a
`bool enableKeyboardSimulation` flag next to the one already in
`BioFeedbackMiddleware`.

---

## Performance for 3–40 people

- Update() cost is dominated by `PickWeighted` — O(N) scan of the roster,
  with `N ≤ 40`. Under a millisecond on any PC.
- `Vector3.MoveTowards` runs only while a person is walking (Idle /
  StandingUp / WalkingOut states). Everyone else just plays an
  animation loop, essentially free.
- No allocation per frame — the only `new List` is inside
  `PickWeighted`, called once per `sceneReactionCooldownSec` = ~3 s.
  If you want zero-alloc, hoist the list to a field and `Clear()` it.
- 40 characters with skinned meshes is well within Meta Quest 2/3
  budget provided you use low-poly Mixamo models (~7-10k tris each).
