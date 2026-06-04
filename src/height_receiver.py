# src/height_receiver.py
"""
UDP receiver for balloon-height telemetry from Unity.

Unity's BioFeedbackMiddleware.SendHeightTelemetry (see
scripts/BioFeedbackMiddleware.cs in this repo) emits one UDP packet per
frame during a live session, throttled to telemetryRateHz (default 10 Hz
on the Unity side). Each packet's payload is plain UTF-8 text of the
form:

    "height,12.345"

Where the float is the balloon's altitude in meters. Some early Unity
builds may send a bare float "12.345" with no prefix; the parser
accepts both.

This module runs the receiver on a background thread so the 50 Hz
pipeline never blocks on UDP. main.py polls `.latest_height` each tick
and includes it in the LSL broadcast and the per-tick CSV row.

If Unity is not running (`enableHeightTelemetry = false` on the Unity
side, or VR not started yet), no packets arrive and `.latest_height`
stays at None. Downstream code treats None as "no height available
yet" and writes an empty cell to the CSV.
"""

import socket
import threading
import time

from config import Config


class HeightReceiver:
    """Listens for Unity height-telemetry packets on a background thread.

    Public attributes:
      .latest_height        last received height in meters (None until first packet)
      .latest_timestamp     wall-clock time the last packet arrived
      .packets_received     running total
      .is_stale()           True if no packet has arrived recently
    """

    def __init__(self,
                 host: str = None, port: int = None,
                 enabled: bool = None, stale_sec: float = None):
        self.host = host if host is not None else Config.HEIGHT_TELEMETRY_HOST
        self.port = port if port is not None else Config.HEIGHT_TELEMETRY_PORT
        self.enabled = (enabled if enabled is not None
                        else Config.HEIGHT_TELEMETRY_ENABLED)
        self.stale_sec = (stale_sec if stale_sec is not None
                          else Config.HEIGHT_TELEMETRY_STALE_SEC)

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
            print("[HEIGHT] Telemetry receiver disabled in Config.")

    def _start(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            # A small timeout lets the thread wake up periodically to check
            # the stop flag, instead of blocking forever on recvfrom.
            self._sock.settimeout(0.2)
        except OSError as e:
            print(f"[HEIGHT] WARN: could not bind UDP {self.host}:{self.port}: {e}")
            self._sock = None
            return

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[HEIGHT] Telemetry receiver listening on UDP "
              f"{self.host}:{self.port}.")

    def _run(self):
        while not self._stop_event.is_set():
            if self._sock is None:
                break
            try:
                data, _ = self._sock.recvfrom(64)
            except socket.timeout:
                continue
            except OSError:
                break
            payload = data.decode('utf-8', errors='replace').strip()
            value = self._parse_packet(payload)
            if value is None:
                self.parse_errors += 1
                continue
            now = time.time()
            with self._lock:
                self.latest_height = value
                self.latest_timestamp = now
                self.packets_received += 1

    @staticmethod
    def _parse_packet(payload: str):
        """Accept "height,12.345" (the documented Unity format) or a bare
        "12.345" (some early Unity builds). Anything else returns None
        and is counted as a parse error."""
        if not payload:
            return None
        try:
            if ',' in payload:
                # e.g. "height,12.345"  - split, parse the numeric tail
                _, num = payload.rsplit(',', 1)
            else:
                num = payload
            return float(num)
        except (ValueError, IndexError):
            return None

    def is_stale(self) -> bool:
        """True if the last packet was longer than stale_sec seconds ago,
        or if no packet has ever arrived."""
        if self.latest_height is None:
            return True
        return (time.time() - self.latest_timestamp) > self.stale_sec

    def get_height(self):
        """Return the latest height in meters, or None if stale / never received."""
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
        print("[HEIGHT] Telemetry receiver stopped.")


if __name__ == "__main__":
    # Standalone smoke test: bind, send a few packets to self, read them.
    import time
    rcv = HeightReceiver()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for h in (5.0, 12.5, 42.0):
        s.sendto(f"height,{h}".encode('utf-8'),
                 ('127.0.0.1', Config.HEIGHT_TELEMETRY_PORT))
        time.sleep(0.1)
    time.sleep(0.3)
    print(f"got {rcv.packets_received} packets; latest = {rcv.latest_height}")
    rcv.close()
    s.close()
