# src/telemetry_receiver.py
"""
Scenario-agnostic UDP telemetry receiver from Unity.

Every VR scenario (Acrophobia, Arachnophobia, Fear of Public Speaking,
future ones) sends one small JSON packet per Unity tick, throttled to
~10 Hz (`Config.TELEMETRY_RATE_HZ` on the Python side, `telemetryRateHz`
on the Unity Inspector side). One shape, all scenarios:

    UDP  →  127.0.0.1:5006
    payload:  {"scenario": "<name>", "data": {<key>: <value>, ...}}

Examples (see UNITY_TELEMETRY_CONTRACT.md for the full contract):

    {"scenario": "acrophobia",      "data": {"height": 12.345}}
    {"scenario": "arachnophobia",   "data": {"size": 1.5, "count": 3}}
    {"scenario": "public_speaking", "data": {"looking": 42,
                                              "audience": 100}}

Whatever `data` fields Unity puts in the packet are stored verbatim on
the Python side. Adding a fourth phobia later requires zero Python
changes — just publish a new scenario label with whatever fields make
sense for that scene, and biofeedback captures them.

Runs on a background thread so the 10 Hz pipeline never blocks on UDP.
main.py polls `.get_telemetry()` each tick and gets a snapshot of the
most recent scenario + data payload.

Port, host, stale-window, and rate are configured in `Config.TELEMETRY_*`.
"""

import json
import socket
import threading
import time

from config import Config


# Field-name aliases that biofeedback recognises as "an altitude in
# metres". Used only to keep the LSL height channel and the samples.csv
# `height_m` column populated for the Acrophobia scenario, so
# session_review's height chart works unchanged. Case-insensitive.
_HEIGHT_ALIASES = ("height", "height_m", "altitude", "altitude_m")


class TelemetryReceiver:
    """Listens for Unity telemetry packets on a background UDP thread.

    Public attributes:
      .latest_scenario   last received scenario label (str, "" if none yet)
      .latest_data       last received data dict (empty until first packet)
      .latest_height     last received height in metres (None if scenario
                         did not include a height-like field)
      .latest_timestamp  wall-clock time the last packet arrived
      .packets_received  running total
      .parse_errors      running total of packets that could not be decoded
      .is_stale()        True if no packet has arrived within stale_sec
      .get_telemetry()   → (scenario: str, data: dict)   primary API
      .get_height()      → float | None                  convenience alias
    """

    def __init__(self,
                 host: str = None, port: int = None,
                 enabled: bool = None, stale_sec: float = None):
        self.host = host if host is not None else Config.TELEMETRY_HOST
        self.port = port if port is not None else Config.TELEMETRY_PORT
        self.enabled = (enabled if enabled is not None
                        else Config.TELEMETRY_ENABLED)
        self.stale_sec = (stale_sec if stale_sec is not None
                          else Config.TELEMETRY_STALE_SEC)

        self.latest_scenario = ""
        self.latest_data = {}
        self.latest_height = None
        self.latest_timestamp = 0.0
        self.packets_received = 0
        self.parse_errors = 0

        self._sock = None
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        if self.enabled:
            self._start()
        else:
            print("[TELEMETRY] Receiver disabled in Config.")

    def _start(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            # A small timeout lets the thread wake up periodically to
            # check the stop flag, instead of blocking forever on recvfrom.
            self._sock.settimeout(0.2)
        except OSError as e:
            print(f"[TELEMETRY] WARN: could not bind UDP "
                  f"{self.host}:{self.port}: {e}")
            self._sock = None
            return

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[TELEMETRY] Receiver listening on UDP "
              f"{self.host}:{self.port} (JSON envelope).")

    def _run(self):
        # 4096-byte buffer — enough for public-speaking payloads with many
        # audience counters. See UNITY_TELEMETRY_CONTRACT.md §"Rules".
        while not self._stop_event.is_set():
            if self._sock is None:
                break
            try:
                data, _ = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            payload = data.decode('utf-8', errors='replace').strip()
            parsed = self._parse_packet(payload)
            if parsed is None:
                self.parse_errors += 1
                continue
            scenario, data_dict = parsed
            now = time.time()
            with self._lock:
                self.latest_scenario = scenario
                self.latest_data = data_dict
                self.latest_height = self._extract_height(data_dict)
                self.latest_timestamp = now
                self.packets_received += 1

    @staticmethod
    def _extract_height(data_dict: dict):
        """Return the numeric height in metres from a data dict, matching
        any of the known aliases (case-insensitive). None if absent."""
        if not data_dict:
            return None
        lowered = {str(k).lower(): v for k, v in data_dict.items()}
        for alias in _HEIGHT_ALIASES:
            if alias in lowered:
                try:
                    return float(lowered[alias])
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _parse_packet(payload: str):
        """
        Parse one UDP payload. Returns (scenario: str, data: dict) on
        success, or None on failure (caller counts as parse_error).

        Contract: one compact JSON object per datagram, with:
          - `Config.TELEMETRY_SCENARIO_KEY` (default "scenario") → non-empty string
          - `Config.TELEMETRY_DATA_KEY`     (default "data")     → object
        See UNITY_TELEMETRY_CONTRACT.md for the full spec.
        """
        if not payload or not payload.startswith('{'):
            return None
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(obj, dict):
            return None
        scenario = obj.get(Config.TELEMETRY_SCENARIO_KEY)
        data_dict = obj.get(Config.TELEMETRY_DATA_KEY, {})
        if not isinstance(scenario, str) or not scenario:
            return None
        if not isinstance(data_dict, dict):
            # Malformed data section — keep the label, drop payload.
            data_dict = {}
        return scenario.strip().lower(), data_dict

    def is_stale(self) -> bool:
        """True if the last packet was longer than stale_sec seconds ago,
        or if no packet has ever arrived."""
        if self.latest_timestamp == 0.0:
            return True
        return (time.time() - self.latest_timestamp) > self.stale_sec

    def get_telemetry(self):
        """Return (scenario: str, data: dict) for the latest fresh packet.
        On stale / never-received, returns ("", {}). Threadsafe snapshot.
        """
        if self.is_stale():
            return "", {}
        with self._lock:
            return self.latest_scenario, dict(self.latest_data)

    def get_height(self):
        """Convenience: return the latest height in metres, or None if
        the current scenario has no height-like field / stale / never
        received. Threadsafe."""
        if self.is_stale():
            return None
        with self._lock:
            return self.latest_height

    def close(self):
        """Stop the receiver thread and release the socket."""
        self._stop_event.set()
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        print("[TELEMETRY] Receiver stopped.")


if __name__ == "__main__":
    # Standalone smoke test: send one JSON packet per known scenario and
    # verify each round-trips cleanly. For a broader test harness see
    # scripts/telemetry_simulator.py.
    rcv = TelemetryReceiver()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    packets = [
        b'{"scenario":"acrophobia","data":{"height":12.345}}',
        b'{"scenario":"arachnophobia","data":{"size":1.5,"count":3}}',
        b'{"scenario":"public_speaking","data":{"looking":42,"audience":100}}',
    ]
    for pkt in packets:
        s.sendto(pkt, ('127.0.0.1', Config.TELEMETRY_PORT))
        time.sleep(0.3)
        scenario, data = rcv.get_telemetry()
        print(f"scenario={scenario!r}  data={data!r}  height={rcv.get_height()!r}")

    s.sendto(b'garbage', ('127.0.0.1', Config.TELEMETRY_PORT))
    time.sleep(0.3)
    print(f"\nTotal: received={rcv.packets_received} parse_errors={rcv.parse_errors}")

    rcv.close()
    s.close()
