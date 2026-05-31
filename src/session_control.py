# src/session_control.py
"""
Session control bus.

A tiny LSL stream that carries operator commands from the dashboard back to
the main pipeline. It's how the Start / Stop / Restart buttons on the
dashboard cause the pipeline to actually start, stop, or restart.

Why a separate stream? The data flow is one-way otherwise: main publishes
`Biofeedback_State` for the dashboard to read. We need the reverse for
control. LSL handles it cleanly — same protocol, no new dependency.

Commands are integer codes; the SessionState enum on the main side tracks
what we're currently doing. The relationship between commands and state
transitions is documented in `_TRANSITIONS` below. SHUTDOWN_* commands are
out-of-band: main.py treats them as "save/discard what's requested then
exit" and they don't appear in the transition table.
"""

from enum import IntEnum

from pylsl import StreamInfo, StreamInlet, StreamOutlet, resolve_stream


CONTROL_STREAM_NAME = "Biofeedback_Control"


class Command(IntEnum):
    """Operator commands sent from dashboard to main pipeline."""
    NONE = 0
    BASELINE_START = 1
    BASELINE_STOP = 2
    BASELINE_RESET = 3       # internal alias of BASELINE_STOP (kept for compat)
    LIVE_START = 4
    LIVE_STOP = 5
    LIVE_RESTART = 6         # was LIVE_RESET; semantics: flush live, back to BASELINE_DONE

    # Out-of-band shutdown commands sent by the dashboard's close-window
    # prompt. main.py acts on these *outside* the transition table: it
    # deletes the files the operator asked to discard, then exits.
    SHUTDOWN_SAVE_BOTH = 7         # keep baseline JSON + session CSV
    SHUTDOWN_DISCARD_LIVE = 8      # keep baseline JSON, drop session CSV
    SHUTDOWN_DISCARD_BOTH = 9      # drop both baseline JSON and session CSV


# Set of out-of-band commands main.py handles as immediate-shutdown, not
# as state transitions.
SHUTDOWN_COMMANDS = frozenset({
    Command.SHUTDOWN_SAVE_BOTH,
    Command.SHUTDOWN_DISCARD_LIVE,
    Command.SHUTDOWN_DISCARD_BOTH,
})


class SessionState(IntEnum):
    """The pipeline's current state. Drives which work each tick does."""
    IDLE = 0           # waiting for baseline_start; samples flow but nothing accumulates
    BASELINE = 1       # baseline buffer accumulating (gated to 120 s)
    BASELINE_DONE = 2  # baseline captured, waiting for live_start
    LIVE = 3           # full pipeline: fusion, state, UDP
    STOPPED = 4        # live session ended; waiting for restart or close


# Allowed state transitions per command. Anything not listed is a no-op
# (e.g. live_start while we're still in IDLE is ignored; the dashboard
# button enable logic should prevent it but main is defensive).
#
# Notable choices:
#   * BASELINE_DONE + BASELINE_START -> BASELINE: lets the operator redo
#     the baseline without quitting.
#   * STOPPED + LIVE_START -> LIVE: a second live session against the same
#     baseline.
#   * LIVE_RESTART (from LIVE or STOPPED) -> BASELINE_DONE: "flush this
#     live session, give me a clean slate but keep the baseline".
_TRANSITIONS = {
    (SessionState.IDLE,           Command.BASELINE_START):  SessionState.BASELINE,

    (SessionState.BASELINE,       Command.BASELINE_STOP):   SessionState.IDLE,
    (SessionState.BASELINE,       Command.BASELINE_RESET):  SessionState.IDLE,

    (SessionState.BASELINE_DONE,  Command.BASELINE_START):  SessionState.BASELINE,
    (SessionState.BASELINE_DONE,  Command.LIVE_START):      SessionState.LIVE,

    (SessionState.LIVE,           Command.LIVE_STOP):       SessionState.STOPPED,
    (SessionState.LIVE,           Command.LIVE_RESTART):    SessionState.BASELINE_DONE,

    (SessionState.STOPPED,        Command.LIVE_START):      SessionState.LIVE,
    (SessionState.STOPPED,        Command.LIVE_RESTART):    SessionState.BASELINE_DONE,
    (SessionState.STOPPED,        Command.BASELINE_START):  SessionState.BASELINE,
}


def apply_command(state: 'SessionState', cmd: 'Command') -> 'SessionState':
    """
    Return the new state after applying `cmd` to the current `state`.
    No-op if the (state, cmd) pair is not a valid transition.
    """
    return _TRANSITIONS.get((state, cmd), state)


class ControlPublisher:
    """Dashboard side. Pushes one command per button click."""

    def __init__(self):
        info = StreamInfo(
            name=CONTROL_STREAM_NAME,
            type='Control',
            channel_count=1,
            nominal_srate=0,  # irregular: commands fire only on click
            channel_format='int32',
            source_id='dashboard_control',
        )
        self.outlet = StreamOutlet(info)
        print(f"[CONTROL] Publisher ready on '{CONTROL_STREAM_NAME}'.")

    def send(self, command: Command):
        """Push a command. Idempotent — multiple identical clicks send multiple packets."""
        self.outlet.push_sample([int(command)])
        print(f"[CONTROL] sent {command.name}")


class ControlSubscriber:
    """Main side. Polls for the latest command."""

    def __init__(self, timeout_sec: float = 5.0):
        print(f"[CONTROL] Resolving '{CONTROL_STREAM_NAME}'...")
        streams = resolve_stream("name", CONTROL_STREAM_NAME)
        if not streams:
            raise RuntimeError(
                f"Control stream '{CONTROL_STREAM_NAME}' not found. "
                f"Is the dashboard running?"
            )
        self.inlet = StreamInlet(streams[0])
        # Drain whatever was queued before we attached — we only care about
        # commands the operator clicks from this moment forward.
        self.inlet.flush()
        print("[CONTROL] Subscriber connected.")

    def poll(self):
        """
        Pull any commands waiting in the inlet. Returns a list of Command
        enums (in arrival order), or an empty list if nothing new.
        """
        samples, _ = self.inlet.pull_chunk(timeout=0.0)
        if not samples:
            return []
        return [Command(int(s[0])) for s in samples]
