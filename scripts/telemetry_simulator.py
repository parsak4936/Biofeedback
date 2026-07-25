"""
telemetry_simulator.py
======================

Pretend to be a Unity VR scene emitting telemetry JSON packets, so the
biofeedback pipeline can be tested end-to-end without touching the
Unity project.

Runs a small script that opens a UDP socket to Config.TELEMETRY_HOST:
Config.TELEMETRY_PORT (127.0.0.1:5006 by default) and pushes one JSON
envelope every 100 ms (10 Hz), matching the real Unity contract in
UNITY_TELEMETRY_CONTRACT.md.

Usage
-----

    # Simulate the current registered scenarios one at a time:
    python scripts/telemetry_simulator.py acrophobia
    python scripts/telemetry_simulator.py arachnophobia
    python scripts/telemetry_simulator.py public_speaking

    # Custom rate and duration:
    python scripts/telemetry_simulator.py acrophobia --rate 20 --seconds 30

    # Ad-hoc scenario for future phobias:
    python scripts/telemetry_simulator.py claustrophobia --field wall_distance_m --pattern sweep

Typical workflow
----------------

Terminal 1: run the biofeedback pipeline as usual (run.bat launches
main + dashboard).
Terminal 2: run this simulator with the scenario you want to test.

The dashboard's scene panel populates in real time. Ctrl+C stops the
simulator; biofeedback keeps running.

Nothing here depends on Unity, OpenSignals, or the PLUX hardware. It's
a pure UDP publisher plus a small library of plausible per-scenario
value patterns.
"""

from __future__ import annotations
import argparse
import json
import math
import os
import socket
import sys
import time

# Make src/ importable for Config.TELEMETRY_* constants.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), 'src'))
from config import Config

# --------------------------------------------------------------------
# Per-scenario value generators.
# Each returns a callable  fn(t: float) -> dict  where t is seconds
# since the simulator started. Keep the outputs plausible enough that
# the dashboard chart looks realistic during testing.
# --------------------------------------------------------------------

def _acrophobia_generator():
    """Ramps up like a rising balloon, then plateaus around 100 m."""
    def _tick(t):
        # 0 → ~100 m over the first 90 s, then small drift.
        base = min(100.0, 1.1 * t)
        wobble = 0.5 * math.sin(t * 0.7)
        return {"height": round(base + wobble, 3)}
    return _tick


def _arachnophobia_generator():
    """Number of spiders climbs slowly; size pulses."""
    def _tick(t):
        count = 1 + int(t // 20)               # +1 spider every 20 s
        size = 0.8 + 0.4 * math.sin(t * 1.3)   # 0.4–1.2 arbitrary units
        return {"size": round(size, 3), "count": count}
    return _tick


def _public_speaking_generator():
    """Audience of 100; 'looking' oscillates as they glance around."""
    def _tick(t):
        looking = 40 + int(30 * math.sin(t * 0.4))   # 10–70 range
        return {"looking": looking, "audience": 100}
    return _tick


def _generic_generator(field_name: str, pattern: str = "sweep"):
    """Fallback for ad-hoc scenarios: one field, one of a few patterns."""
    if pattern == "constant":
        def _tick(_t):
            return {field_name: 1.0}
    elif pattern == "ramp":
        def _tick(t):
            return {field_name: round(t * 0.1, 3)}
    elif pattern == "sine":
        def _tick(t):
            return {field_name: round(math.sin(t * 0.5), 3)}
    else:  # sweep
        def _tick(t):
            return {field_name: round(0.5 + 0.5 * math.sin(t * 0.3), 3)}
    return _tick


_KNOWN_GENERATORS = {
    'acrophobia':      _acrophobia_generator,
    'arachnophobia':   _arachnophobia_generator,
    'public_speaking': _public_speaking_generator,
}


# --------------------------------------------------------------------
# Main run loop
# --------------------------------------------------------------------

def run(scenario: str, rate_hz: float = 10.0, duration_sec: float = 0,
        field: str = None, pattern: str = "sweep",
        host: str = None, port: int = None):
    """Send JSON telemetry packets at the given rate until Ctrl+C or
    duration_sec elapses (0 = run forever)."""

    host = host or Config.TELEMETRY_HOST
    port = port or Config.TELEMETRY_PORT
    if host == '0.0.0.0':
        host = '127.0.0.1'   # UDP send needs a routable address

    scenario = scenario.strip().lower()
    if scenario in _KNOWN_GENERATORS:
        gen = _KNOWN_GENERATORS[scenario]()
    else:
        if not field:
            print(f"[SIM] Scenario '{scenario}' is not registered. "
                  f"Pass --field <name> to specify which value to emit.",
                  file=sys.stderr)
            sys.exit(2)
        gen = _generic_generator(field, pattern)

    interval = 1.0 / max(rate_hz, 1e-3)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print(f"[SIM] Emitting scenario={scenario!r} at {rate_hz:.1f} Hz "
          f"→ {host}:{port}")
    if duration_sec > 0:
        print(f"[SIM] Will stop after {duration_sec:.1f} s. "
              "Ctrl+C for early exit.")
    else:
        print(f"[SIM] Running until Ctrl+C.")
    print()

    start = time.time()
    tick = 0
    try:
        while True:
            t = time.time() - start
            if duration_sec > 0 and t >= duration_sec:
                break

            data = gen(t)
            envelope = {
                Config.TELEMETRY_SCENARIO_KEY: scenario,
                Config.TELEMETRY_DATA_KEY: data,
            }
            payload = json.dumps(envelope,
                                  separators=(',', ':'), ensure_ascii=True)
            sock.sendto(payload.encode('utf-8'), (host, port))

            # Print one status line every second so the operator sees
            # what's flowing. Cheap enough at 10 Hz.
            if tick % max(1, int(rate_hz)) == 0:
                pretty = ", ".join(f"{k}={v}" for k, v in data.items())
                print(f"  t={t:6.1f}s  {pretty}")
            tick += 1

            # Sleep the remainder of this interval.
            elapsed_in_tick = (time.time() - start) - t
            sleep_for = interval - elapsed_in_tick
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        print(f"\n[SIM] Stopped after {tick} packets "
              f"({(time.time() - start):.1f} s).")


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Simulate a Unity VR scene emitting biofeedback "
                    "telemetry over UDP. See SCENARIOS.md for the "
                    "registered scenarios."
    )
    p.add_argument('scenario',
                   help="Scenario label: acrophobia / arachnophobia / "
                        "public_speaking / <custom>.")
    p.add_argument('--rate', type=float, default=10.0,
                   help="Packets per second (default 10 Hz, matching "
                        "the real Unity build).")
    p.add_argument('--seconds', type=float, default=0.0,
                   help="Stop after this many seconds (0 = run until "
                        "Ctrl+C, default).")
    p.add_argument('--field', default=None,
                   help="For an unregistered scenario: name of the "
                        "single numeric field to emit.")
    p.add_argument('--pattern', choices=('sweep', 'sine', 'ramp',
                                          'constant'),
                   default='sweep',
                   help="Value pattern for --field (default sweep).")
    p.add_argument('--host', default=None,
                   help="Target host (default from Config.TELEMETRY_HOST).")
    p.add_argument('--port', type=int, default=None,
                   help="Target port (default from Config.TELEMETRY_PORT).")
    args = p.parse_args()

    run(scenario=args.scenario, rate_hz=args.rate,
        duration_sec=args.seconds, field=args.field, pattern=args.pattern,
        host=args.host, port=args.port)


if __name__ == "__main__":
    main()
