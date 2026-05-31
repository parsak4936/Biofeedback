# src/acquisition.py
"""
Acquisition layer: 50 Hz consumer of the OpenSignals LSL stream.

This module pulls samples from whatever is publishing under the name
`Config.STREAM_NAME` (either our own streamer in mock mode, or the PLUX
OpenSignals software in real-device mode) and presents them to the rest
of the pipeline as a synchronous 50 Hz feed. Three responsibilities live
here that are easy to miss in the main loop:

  1. Draining the inlet to the LATEST sample each tick (not the oldest).
     If the hardware streams faster than 50 Hz (200 or 1000 Hz typical),
     the LSL inlet would otherwise pile up and the pipeline would drift
     minutes behind real time. We use pull_chunk() and take the tail.

  2. Sample validation. Any NaN / Inf gets rejected; any physiologically
     impossible value (HR > 220 BPM, EDA < 0, etc.) gets rejected. Last
     known good value is held in either case. The bounds and the rejection
     status codes (NEW_DATA / HOLD_LAST / NAN_REJ / OOR_REJ) are all
     visible in the per-tick acquisition_log_*.csv that this module writes.

  3. Disconnect detection. A rolling 15-second variance check on each
     signal — if any signal goes flat, the operator gets a one-shot
     warning ("possible electrode disconnect"). The pipeline keeps
     running so a partial recording is still saved.

A deadman switch raises ConnectionError if the source stops publishing
for more than Config.STREAM_TIMEOUT_SEC seconds.
"""

import collections
import math
import time
from pylsl import resolve_stream, StreamInlet
from config import Config
import csv
import datetime
import os


def _is_valid_number(x: float) -> bool:
    """Reject NaN, +/-Inf, and obviously corrupt sentinels."""
    return isinstance(x, (int, float)) and math.isfinite(x)


def _within(value: float, lo: float, hi: float) -> bool:
    return lo <= value <= hi


class BiofeedbackAcquisition:
    """
    Manages the Lab Streaming Layer (LSL) connection and data ingestion.
    Resolves multi-rate asynchronous biological signals into a synchronous 50Hz vector 
    using a Zero-Order Hold strategy.
    """
    
    def __init__(self):
        self.inlet = self._connect_to_stream()
        self.tick_counter = 0
        self.stale_tick_counter = 0  # consecutive empty pulls
        # State variables for Zero-Order Hold (ZOH)
        # These hold the most recent value if the stream has no new data on a given tick
        self.latest_eda = 0.0
        self.latest_hr = 0.0
        self.latest_hrv = 0.0

        # Diagnostics counters — flow into CODE_AUDIT visibility.
        self.invalid_sample_count = 0       # NaN / Inf
        self.out_of_range_count = 0         # outside physiological bounds
        self.disconnect_warnings_issued = 0  # constant-signal episodes

        # Rolling buffers used to detect electrode disconnect (signal pinned).
        window_n = int(Config.DISCONNECT_WINDOW_SEC * Config.PIPELINE_RATE)
        self._var_buffers = {
            'eda': collections.deque(maxlen=window_n),
            'hr':  collections.deque(maxlen=window_n),
            'hrv': collections.deque(maxlen=window_n),
        }
        
        # Diagnostic tracking
        self.tick_counter = 0
        self.log_path = None
        self.log_file = None
        self.csv_writer = None
        self._open_log()

    def _open_log(self):
        """(Re-)open the acquisition audit log with a fresh timestamp."""
        session_time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        data_dir = os.path.join(project_root, 'data')
        os.makedirs(data_dir, exist_ok=True)
        filename = f'acquisition_log_{session_time}.csv'
        self.log_path = os.path.join(data_dir, filename)
        self.log_file = open(self.log_path, mode='w', newline='')
        self.csv_writer = csv.writer(self.log_file)
        self.csv_writer.writerow(['timestamp', 'status', 'raw_eda', 'raw_hr', 'raw_hrv'])

    def discard_log(self, final: bool = False):
        """Close and delete the current acquisition log.

        final=False (default): reopen a fresh log for the next baseline.
        final=True: skip the reopen. Used on shutdown so the discarded
        file doesn't immediately come back as an empty one."""
        path = self.log_path
        try:
            if self.log_file is not None:
                self.log_file.flush()
                self.log_file.close()
                self.log_file = None
        except Exception:
            pass
        try:
            if path and os.path.exists(path):
                os.remove(path)
                print(f"[ACQUISITION] Discarded partial log: {os.path.basename(path)}")
        except OSError as e:
            print(f"[ACQUISITION] WARN: could not delete log {path}: {e}")
        if not final:
            self._open_log()
    def _connect_to_stream(self) -> StreamInlet:
        """
        Locates the target LSL stream on the local network and establishes an inlet.
        Blocks execution until the stream is found.
        """
        print(f"[SYSTEM] Scanning network for LSL Stream: {Config.STREAM_NAME}...")
        
        # Resolve stream by name matching our config
        streams = resolve_stream("name", Config.STREAM_NAME)
        
        if not streams:
            raise RuntimeError("Failed to resolve the LSL stream. Is mock_streamer.py running?")
            
        # Create an inlet to receive signal samples from the first matched stream
        inlet = StreamInlet(streams[0])
        # Reproducibility fix: discard anything that may already be queued in
        # the inlet by the time we got here. With the streamer's consumer-wait
        # handshake on the other side, the streamer is now blocked until it
        # sees us, and we have a clean buffer. The first sample we read will
        # be the first sample the streamer pushes (file index 0).
        inlet.flush()
        print(f"[SUCCESS] Connected to '{Config.STREAM_NAME}'. Inlet flushed and ready.")

        return inlet

    def get_synchronized_sample(self) -> list:
        """
        Pulls data from the LSL stream and returns the LATEST value.

        Walkthrough Step 0: "at every tick, ask: what is the latest available
        value for HR / HRV / EDA?" We drain the inlet with pull_chunk() and
        take the most recent sample. This is critical when the hardware rate
        (200 or 1000 Hz) exceeds the pipeline rate (50 Hz) — otherwise the
        LSL inlet buffers samples faster than we consume them, and we'd end
        up reading minutes of stale data behind real time.
        """
        # Drain everything available in the inlet (zero-block).
        samples, timestamps = self.inlet.pull_chunk(timeout=0.0)

        if samples:
            # Use the most recent sample only
            latest = samples[-1]
            new_eda, new_hr, new_hrv = float(latest[0]), float(latest[1]), float(latest[2])

            # ---- Sample validation ----
            # 1. NaN / Inf rejection: skip the bad sample, hold previous value.
            if not (_is_valid_number(new_eda) and _is_valid_number(new_hr)
                    and _is_valid_number(new_hrv)):
                self.invalid_sample_count += 1
                status = "NAN_REJ  "
            # 2. Physiological out-of-range rejection: same — hold previous.
            elif not (_within(new_eda, Config.EDA_MIN_uS, Config.EDA_MAX_uS)
                      and _within(new_hr, Config.HR_MIN_BPM, Config.HR_MAX_BPM)
                      and _within(new_hrv, Config.HRV_MIN_MS, Config.HRV_MAX_MS)):
                self.out_of_range_count += 1
                status = "OOR_REJ  "
            else:
                self.latest_eda = new_eda
                self.latest_hr = new_hr
                self.latest_hrv = new_hrv
                status = "NEW_DATA "

            # ---- Electrode-disconnect detection ----
            # Track variance over a rolling window; warn if a signal pins flat.
            self._var_buffers['eda'].append(self.latest_eda)
            self._var_buffers['hr'].append(self.latest_hr)
            self._var_buffers['hrv'].append(self.latest_hrv)
            self._check_disconnect()

            self.stale_tick_counter = 0  # Reset the deadman's switch
        else:
            # No new data; rely on the Zero-Order Hold
            self.stale_tick_counter += 1
            status = "HOLD_LAST"

            # The Deadman's Switch Evaluation
            max_stale_ticks = Config.STREAM_TIMEOUT_SEC * Config.PIPELINE_RATE
            if self.stale_tick_counter > max_stale_ticks:
                raise ConnectionError(
                    f"\n[CRITICAL ERROR] Stream Lost: No new data received for {Config.STREAM_TIMEOUT_SEC} seconds. "
                    f"Check PLUX device connection and battery."
                )

        # Diagnostic Printing
        if self.tick_counter % int(Config.PIPELINE_RATE) == 0:
            print(f"[TICK {self.tick_counter:05d}] {status} | "
                  f"EDA: {self.latest_eda:>6.2f} uS | "
                  f"HR: {self.latest_hr:>6.2f} BPM | "
                  f"HRV: {self.latest_hrv:>6.2f} ms")

        self.tick_counter += 1
        # Write to audit log
        current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        self.csv_writer.writerow([
            current_time, status.strip(), 
            round(self.latest_eda, 4), round(self.latest_hr, 4), round(self.latest_hrv, 4)
        ])
        return [self.latest_eda, self.latest_hr, self.latest_hrv]

    def _check_disconnect(self):
        """
        If any signal's variance over the last DISCONNECT_WINDOW_SEC seconds
        falls below threshold, that electrode is almost certainly disconnected
        or pinned to a rail. Print a one-shot warning per pin event.
        """
        # Only check once the buffer is full enough to be meaningful.
        eda_buf = self._var_buffers['eda']
        if len(eda_buf) < eda_buf.maxlen:
            return

        def _variance(buf):
            n = len(buf)
            if n < 2:
                return float('inf')
            mean = sum(buf) / n
            return sum((x - mean) ** 2 for x in buf) / n

        pinned = []
        if _variance(self._var_buffers['eda']) < Config.DISCONNECT_VAR_THRESHOLD_EDA:
            pinned.append('EDA')
        if _variance(self._var_buffers['hr']) < Config.DISCONNECT_VAR_THRESHOLD_HR:
            pinned.append('HR')
        if _variance(self._var_buffers['hrv']) < Config.DISCONNECT_VAR_THRESHOLD_HRV:
            pinned.append('HRV')

        if pinned:
            # Emit only when the state CHANGES (i.e., this tick is the first
            # one that observes the disconnect) so the operator gets one alert,
            # not 50 per second.
            if not getattr(self, '_last_pinned', None) == pinned:
                self.disconnect_warnings_issued += 1
                print(f"[WARN] Possible electrode disconnect: {', '.join(pinned)} "
                      f"signal(s) pinned for >{Config.DISCONNECT_WINDOW_SEC}s.")
                self._last_pinned = pinned
        else:
            self._last_pinned = None

    def run_standalone_test(self):
        """
        A diagnostic loop to verify ingestion before integrating into the main pipeline.
        Maintains a rigid 50 Hz cycle.
        """
        print("\n[ACQUISITION] Starting 50Hz ingestion test. Press Ctrl+C to stop.\n")
        
        # Calculate the exact time per tick (e.g., 50Hz = 0.02 seconds)
        tick_duration = 1.0 / Config.PIPELINE_RATE
        
        try:
            while True:
                start_time = time.time()
                
                # Fetch the synchronized vector
                vector = self.get_synchronized_sample()
                
                # Calculate how long the fetch took
                elapsed = time.time() - start_time
                
                # Sleep exactly the remainder of the 20ms window to enforce 50 Hz
                sleep_time = tick_duration - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                else:
                    # If processing took longer than 20ms, log a frame drop warning
                    print(f"[WARNING] Processing lag detected. Frame drop at tick {self.tick_counter}.")
                    
        except KeyboardInterrupt:
            print("\n[SYSTEM] Ingestion test terminated.")

if __name__ == "__main__":
    # Diagnostic tracking
    
    acq = BiofeedbackAcquisition()
    acq.run_standalone_test()