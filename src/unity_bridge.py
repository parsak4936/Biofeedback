# src/unity_bridge.py
"""
UDP command bridge to Unity.

Sends plain-text commands over UDP to Unity's BioFeedbackMiddleware,
which listens on port 5005 by default. Four valid commands, all
case-insensitive on the Unity side:

  start      Unity flips its session into running state
  stop       Unity stops the session
  increase   Bumps Unity's target altitude up by stepAmount (default 1 m)
  decrease   Bumps Unity's target altitude down by stepAmount

Sending nothing means Unity holds the current target altitude; the visual
lerp smooths motion either way. By design: when the patient is in the
"stressed" target zone, the balloon should stay put, so no command is
emitted rather than an explicit "hold".

Wait-for-calm gate. After "start", state-driven commands are held until
the patient first hits the calm state. Reason: when the live phase
begins the patient might be mid-adjustment (putting on the VR, settling
in), so the first stress reading is often unreliable. Waiting for the
system to observe a real calm before opening the gate prevents noise
from flooding Unity at session start. A short warmup window
(`Config.UDP_GATE_WARMUP_SEC`, default 1.5 s) is applied first so the
fusion engine's buffer-warmup default "calm" does not trip the gate
prematurely.

Audit log. Every packet actually sent is also written to a CSV
(`data/unity_udp_log_<ts>_<patient>.csv`) with timestamp, kind
(`lifecycle` for start/stop, `state` for increase/decrease), command,
current state, current S_t, and the gate-open flag. The audit log is
the ground truth for verifying throttle behaviour and for correlating
Unity's balloon motion with the stress trace.

This bridge runs in parallel with the LSL output in `src/output.py`.
LSL is the canonical stream for the dashboard and audit logs; UDP is
the dedicated, fire-and-forget channel for Unity. The two do not
interact: they share no state and use different sockets.

Why UDP rather than LSL? Two reasons:
  1. Unity's middleware was already written to listen on UDP when this
     project picked it up; using the existing protocol means no Unity
     code change to integrate.
  2. UDP is connectionless: if Unity is not running yet (or has
     crashed), sends silently disappear instead of blocking or raising
     errors. The Python side can start before Unity, in either order.
"""

import datetime
import os
import socket
import time
from config import Config


# State -> command mapping. "stressed" deliberately maps to None: the
# therapeutic target zone is where the balloon should hold position, which
# we communicate by silence (no UDP packet at all).
_STATE_TO_COMMAND = {
    "calm":           "increase",
    "stressed":       None,
    "ultra_stressed": "decrease",
}

# Numeric encoding of the last UDP command, broadcast on the LSL stream
# so the dashboard can display what Unity most recently saw. None is 0
# (haven't sent anything yet). "stressed" never gets a code because we
# don't send anything in that state — silence is the command.
_COMMAND_CODES = {
    None:       0,
    'increase': 1,
    'decrease': 2,
    'start':    3,
    'stop':     4,
}


class UnityUDPBridge:
    """Sends plain-text commands to Unity over UDP, with throttling."""

    def __init__(self, host: str = None, port: int = None,
                 interval_sec: float = None,
                 audit_log_path: str = None):
        self.host = host if host is not None else Config.UNITY_UDP_HOST
        self.port = port if port is not None else Config.UNITY_UDP_PORT
        self.interval = (interval_sec if interval_sec is not None
                         else Config.UNITY_COMMAND_INTERVAL_SEC)

        # Audit log: one row per actual UDP send. Lets you verify, after
        # the fact, exactly what Unity received and when — start/stop
        # lifecycle events plus state-driven increase/decrease commands.
        # If audit_log_path is None, no file is opened (silent).
        self._audit_path = audit_log_path
        self._audit_handle = None
        if audit_log_path:
            try:
                os.makedirs(os.path.dirname(audit_log_path) or '.', exist_ok=True)
                self._audit_handle = open(audit_log_path, 'w', newline='',
                                          buffering=1)
                self._audit_handle.write(
                    "timestamp,kind,command,state,s_t,gate_open\n"
                )
                print(f"[UNITY] Audit log: {os.path.basename(audit_log_path)}")
            except OSError as e:
                print(f"[UNITY] WARN: could not open audit log: {e}")
                self._audit_handle = None

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # On Windows, an ICMP "destination unreachable" can break the socket
        # for subsequent sends if Unity isn't listening when we try to send.
        # Suppress that behavior so the socket survives an absent receiver.
        # The exception types vary by platform / Python build, hence the
        # broad except.
        try:
            SIO_UDP_CONNRESET = 0x9800000C
            self._sock.ioctl(SIO_UDP_CONNRESET, False)
        except Exception:
            pass  # not Windows, or feature unsupported on this build

        # Throttle tracking — for state commands only. Lifecycle commands
        # (start, stop) bypass the throttle, see send_raw().
        self._last_state_emit_time = 0.0
        self._last_command = None

        # Wait-for-calm gate. Stays False (commands held) until the first
        # genuine calm state is observed after "start", then flips to True
        # for the rest of the session.
        self.gate_open = False
        self._start_time = None  # set when "start" is sent
        self._warmup_sec = Config.UDP_GATE_WARMUP_SEC

        # Public counters; main.py / dashboard can surface them.
        self.commands_sent = 0
        self.commands_throttled = 0
        self.commands_gated = 0  # held back because the gate isn't open yet

        print(f"[UNITY] UDP bridge ready: target {self.host}:{self.port}, "
              f"throttle {self.interval}s, gate warmup {self._warmup_sec}s.")

    def send_raw(self, command: str):
        """
        Send a command immediately, bypassing the throttle and the gate.
        Used for lifecycle commands (start, stop) that must never be
        delayed or dropped. Sending "start" also stamps the moment so the
        gate's warmup window can be measured from it.
        """
        try:
            self._sock.sendto(command.encode('utf-8'), (self.host, self.port))
            self.commands_sent += 1
            self._last_command = command
            print(f"[UNITY] sent '{command}'")
            self._audit_write(kind='lifecycle', command=command,
                              state=None, s_t=None)
        except OSError as e:
            # UDP send shouldn't normally fail since there's no connection
            # state, but on Windows a stale ICMP reset can sneak through.
            print(f"[UNITY] WARN: UDP send failed: {e}")
        if command == "start":
            self._start_time = time.time()

    def _audit_write(self, kind: str, command: str,
                     state: str = None, s_t: float = None):
        """Append one row to the audit CSV. Silent if no audit log."""
        if self._audit_handle is None:
            return
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        state_str = state if state else ""
        s_t_str   = f"{s_t:.4f}" if s_t is not None else ""
        gate_str  = "1" if self.gate_open else "0"
        try:
            self._audit_handle.write(
                f"{ts},{kind},{command},{state_str},{s_t_str},{gate_str}\n"
            )
        except Exception:
            pass

    def send_state(self, state: str, s_t: float = None) -> bool:
        """
        Map the current stress state to a balloon command and emit it, with
        the wait-for-calm gate and the throttle applied in that order.
        s_t is passed through to the audit log only.

        Order of checks:
          1. If "start" hasn't been sent yet, do nothing (we're not in a
             live session).
          2. If the gate is closed and we're still inside the warmup
             window, do nothing — the fusion engine may be reporting
             "calm" only because its buffer hasn't filled.
          3. If the gate is closed and the state is now "calm" (after
             warmup), open the gate permanently.
          4. If the gate is closed and the state is anything else, hold
             the command and increment the gated counter.
          5. Once the gate is open, "stressed" still emits silence, and
             "calm" / "ultra_stressed" go through the throttle.

        Returns True if a packet was actually sent.
        """
        if self._start_time is None:
            return False  # no live session yet

        now = time.time()

        # Stages 2-4: the gate.
        if not self.gate_open:
            if now - self._start_time < self._warmup_sec:
                # Warmup: ignore state entirely, including any synthetic
                # "calm" coming from the fusion engine's buffer warmup.
                return False
            if state != "calm":
                self.commands_gated += 1
                return False
            # First real calm observation — open the gate for good.
            self.gate_open = True
            print(f"[UNITY] gate opened: first calm reading "
                  f"({now - self._start_time:.1f}s after start). "
                  f"{self.commands_gated} commands held during wait.")
            # Fall through so the calm itself becomes the first emission.

        # Stage 5: normal flow.
        cmd = _STATE_TO_COMMAND.get(state)
        if cmd is None:
            return False  # stressed -> hold

        if now - self._last_state_emit_time < self.interval:
            self.commands_throttled += 1
            return False

        # send_raw will write a lifecycle row; overwrite with a state row
        # that carries the state + s_t for forensic clarity.
        try:
            self._sock.sendto(cmd.encode('utf-8'), (self.host, self.port))
            self.commands_sent += 1
            self._last_command = cmd
            print(f"[UNITY] sent '{cmd}' (state={state})")
            self._audit_write(kind='state', command=cmd, state=state, s_t=s_t)
        except OSError as e:
            print(f"[UNITY] WARN: UDP send failed: {e}")
            return False
        self._last_state_emit_time = now
        return True

    def reset(self):
        """
        Close the gate and clear timing state. Used when the operator
        resets the live session so a new live_start can re-establish
        the wait-for-calm gate from scratch.
        """
        self.gate_open = False
        self._start_time = None
        self._last_state_emit_time = 0.0
        self.commands_gated = 0
        print("[UNITY] Bridge reset: gate closed, ready for next start.")

    def last_command_code(self) -> int:
        """Numeric encoding of the most recent UDP command we sent.
        Used by main.py / dashboard to surface 'last sent to Unity' on
        screen. Returns 0 if we haven't sent anything yet."""
        return _COMMAND_CODES.get(self._last_command, 0)

    def close(self):
        """Close the UDP socket and the audit log. Safe to call repeatedly."""
        try:
            self._sock.close()
        except Exception:
            pass
        try:
            if self._audit_handle is not None:
                self._audit_handle.flush()
                self._audit_handle.close()
                self._audit_handle = None
        except Exception:
            pass


if __name__ == "__main__":
    # Standalone test: run a tiny loopback UDP receiver and verify the
    # bridge's throttling actually limits emission rate.
    import threading

    received = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(('127.0.0.1', Config.UNITY_UDP_PORT))
    listener.settimeout(0.05)

    def _recv_loop(deadline):
        while time.time() < deadline:
            try:
                data, _ = listener.recvfrom(1024)
                received.append(data.decode('utf-8'))
            except socket.timeout:
                continue
            except OSError:
                break

    deadline = time.time() + 4.0
    t = threading.Thread(target=_recv_loop, args=(deadline,), daemon=True)
    t.start()

    bridge = UnityUDPBridge(interval_sec=0.5)  # half-second throttle for the test
    bridge.send_raw("start")

    # Fire 60 "calm" attempts over 3 seconds. With a 0.5 s throttle we expect
    # roughly 6 packets out (1 every 500 ms), not 60.
    for _ in range(60):
        bridge.send_state("calm")
        time.sleep(0.05)

    # "stressed" should never produce a packet.
    for _ in range(10):
        bridge.send_state("stressed")
        time.sleep(0.05)

    bridge.send_raw("stop")
    bridge.close()

    t.join(timeout=1.0)
    listener.close()

    print(f"\nbridge.commands_sent      = {bridge.commands_sent}")
    print(f"bridge.commands_throttled = {bridge.commands_throttled}")
    print(f"messages received by loopback: {received}")
