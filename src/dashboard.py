# src/dashboard.py
"""
Clinical biofeedback dashboard.

Two side-by-side panels, one per session phase. Both panels are symmetric
with Start + Stop:

  Left (Baseline)
    The 120 s baseline timer ticks only while in BASELINE state.
    Pipeline-uptime before Start does not count. Once baseline locks,
    captured values appear here and the status banner invites the
    operator to start the live session.

  Right (Live Session)
    Start stays disabled until a baseline is captured. Stop only enables
    once Start has been clicked. Clicking Start Live again after a Stop
    rotates the session CSV to a fresh file.

Buttons publish commands on `Biofeedback_Control` for main.py to read.
The main pipeline broadcasts its current SessionState back as channel
20 of `Biofeedback_State`, which is how this dashboard knows when to
enable or disable each button. The Unity last-command code is on
channels 22 / 23 and surfaces on the live panel as well.

Closing the window asks the operator whether to keep or discard the
baseline / live outputs separately, then shuts down the pipeline cleanly.
"""

import os
import sys
import time

import pyqtgraph as pg
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QFrame, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)
from pylsl import resolve_byprop, resolve_stream, StreamInlet

from config import Config
from session_control import Command, ControlPublisher, SessionState


# State -> (baseline buttons, live buttons) enable-disable tuples.
# Both panels are symmetric now: (start_enabled, stop_enabled).
_BUTTON_STATES = {
    SessionState.IDLE:           ((True,  False), (False, False)),
    SessionState.BASELINE:       ((False, True),  (False, False)),
    SessionState.BASELINE_DONE:  ((True,  False), (True,  False)),
    SessionState.LIVE:           ((False, False), (False, True)),
    SessionState.STOPPED:        ((True,  False), (True,  False)),
}


# Status-banner messages keyed by SessionState. The banner is the on-screen
# echo of the console "[STATE] X -> Y" line the operator already sees in
# the terminal — same intent, but visible in the room.
_STATE_BANNERS = {
    SessionState.IDLE:          ("Ready. Click Start Baseline to begin.", "#aaaaaa"),
    SessionState.BASELINE:      ("Baseline started — sit still, capture in progress.", "#ffff00"),
    SessionState.BASELINE_DONE: ("Baseline complete. You can start the Live Session.", "#0099ff"),
    SessionState.LIVE:          ("Live session in progress.", "#00ff00"),
    SessionState.STOPPED:       ("Live session stopped. Results saved.", "#888888"),
}


class ClinicalDashboard:
    def __init__(self):
        # ---- LSL inlets ----
        print("[DASHBOARD] Searching for Biofeedback_State stream...")
        try:
            streams = resolve_stream("name", Config.OUT_STREAM_NAME)
            self.inlet = StreamInlet(streams[0])
            print("[DASHBOARD] Connected to output stream.")
        except Exception as e:
            print(f"[ERROR] Could not find stream '{Config.OUT_STREAM_NAME}'")
            print("Make sure main.py is running.")
            raise RuntimeError(f"LSL stream not found: {str(e)}")

        # Optional ECG side stream — empty chart if unavailable, that's fine.
        self.ecg_inlet = None
        try:
            ecg_streams = resolve_byprop("name", Config.ECG_STREAM_NAME, timeout=2.0)
            if ecg_streams:
                self.ecg_inlet = StreamInlet(ecg_streams[0])
                print(f"[DASHBOARD] Connected to ECG side stream "
                      f"'{Config.ECG_STREAM_NAME}'.")
            else:
                print(f"[DASHBOARD] ECG side stream not present; chart will stay empty.")
        except Exception as e:
            print(f"[DASHBOARD] ECG side stream lookup failed ({e}); skipping.")

        # ---- Control publisher (button clicks -> main.py) ----
        self.control = ControlPublisher()

        # ---- Qt setup ----
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.win = QWidget()
        self.win.setWindowTitle("Clinical Biofeedback Dashboard")
        self.win.resize(1700, 950)
        self.win.setStyleSheet(self._stylesheet())

        # ---- Data buffers ----
        self.stress_data = {'x': [], 'y': []}
        self.eda_data    = {'x': [], 'y': []}
        self.hr_data     = {'x': [], 'y': []}
        self.hrv_data    = {'x': [], 'y': []}
        self.delta_eda_data = {'x': [], 'y': []}
        self.delta_hr_data  = {'x': [], 'y': []}
        self.delta_hrv_data = {'x': [], 'y': []}
        self.ecg_data       = {'x': [], 'y': []}
        self.tick_counter = 0
        self.ecg_tick = 0
        self.max_history = Config.DASHBOARD_MAX_HISTORY
        self.view_width = Config.DASHBOARD_VIEW_WIDTH
        self._signal_ranges_centered = False

        # Time-in-state counters (LIVE only)
        self.ticks_calm = 0
        self.ticks_stressed = 0
        self.ticks_ultra = 0

        # Background colors for the stress chart per state
        self.color_calm     = pg.mkColor(20, 50, 20)
        self.color_stressed = pg.mkColor(50, 50, 20)
        self.color_ultra    = pg.mkColor(50, 20, 20)

        # Track most recent SessionState we observed
        self._last_state_observed = SessionState.IDLE

        # ---- Build the UI ----
        self._build_ui()

        # Start with all buttons reflecting IDLE
        self._apply_button_states(SessionState.IDLE)

        # ---- 50 Hz tick ----
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_dashboard)
        self.timer.start(20)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self.win)
        outer.setSpacing(8)
        outer.setContentsMargins(10, 8, 10, 8)

        outer.addLayout(self._build_info_bar())

        panels = QHBoxLayout()
        panels.setSpacing(10)
        panels.addWidget(self._build_baseline_panel(), 1)
        panels.addWidget(self._build_live_panel(), 1)
        outer.addLayout(panels, 1)

        outer.addLayout(self._build_qa_strip())

    def _build_info_bar(self):
        layout = QHBoxLayout()
        patient_name = os.environ.get('PATIENT_NAME', 'PATIENT')
        patient_id = os.environ.get('PATIENT_ID', '000')

        self.label_patient = QLabel(f"Patient: {patient_name} ({patient_id})")
        self.label_patient.setFont(QFont("Arial", 14, QFont.Bold))
        self.label_patient.setStyleSheet("color: #0099ff;")

        self.label_phase = QLabel("Phase: IDLE")
        self.label_phase.setFont(QFont("Arial", 12, QFont.Bold))
        self.label_phase.setStyleSheet("color: #aaaaaa;")

        # The status banner mirrors the [STATE] transitions printed in the
        # terminal — so the operator sees the same news on screen.
        self.label_status = QLabel("Ready. Click Start Baseline to begin.")
        self.label_status.setFont(QFont("Arial", 11))
        self.label_status.setStyleSheet("color: #aaaaaa;")
        self.label_status.setWordWrap(True)

        layout.addWidget(self.label_patient, 2)
        layout.addWidget(self.label_phase, 1)
        layout.addWidget(self.label_status, 3)
        return layout

    def _build_baseline_panel(self):
        """Left panel: baseline buttons, captured values, signal charts."""
        group = QGroupBox("Baseline Calibration")
        group.setStyleSheet("QGroupBox { color: #0099ff; font-weight: bold; "
                            "border: 1px solid #333; border-radius: 4px; "
                            "margin-top: 8px; padding-top: 14px; }"
                            "QGroupBox::title { left: 10px; padding: 0 4px; }")
        v = QVBoxLayout(group)
        v.setSpacing(8)

        # Buttons (2: Start + Stop)
        btn_row = QHBoxLayout()
        self.btn_baseline_start = QPushButton("Start Baseline")
        self.btn_baseline_stop  = QPushButton("Stop")
        for b in (self.btn_baseline_start, self.btn_baseline_stop):
            b.setMinimumHeight(34)
            btn_row.addWidget(b)
        self.btn_baseline_start.clicked.connect(
            lambda: self.control.send(Command.BASELINE_START))
        self.btn_baseline_stop.clicked.connect(
            lambda: self.control.send(Command.BASELINE_STOP))
        v.addLayout(btn_row)

        # Per-panel duration: 0:00 / 2:00, counts only while in BASELINE
        # state. Freezes at the lock so the operator sees "completed in N s".
        self.label_baseline_duration = QLabel("Baseline duration: 00:00 / 02:00")
        self.label_baseline_duration.setFont(QFont("Arial", 11, QFont.Bold))
        self.label_baseline_duration.setStyleSheet("color: #ffff00;")
        v.addWidget(self.label_baseline_duration)

        # Captured-values readout (populated when baseline locks)
        values = QGroupBox("Personal Baseline (captured)")
        values.setStyleSheet("QGroupBox { color: #888888; border: none; }")
        vv = QVBoxLayout(values)
        vv.setSpacing(2)
        self.label_baseline_eda = QLabel("EDA: -- uS")
        self.label_baseline_hr  = QLabel("HR:  -- BPM")
        self.label_baseline_hrv = QLabel("HRV: -- ms")
        self.label_baseline_sigma = QLabel("sigma_baseline: --")
        self.label_baseline_thresh = QLabel("Thresholds: MILD = --, HIGH = --")
        self.label_baseline_artifacts = QLabel("Artifacts: EDA=-- HR=-- HRV=--")
        for lab in (self.label_baseline_eda, self.label_baseline_hr,
                    self.label_baseline_hrv, self.label_baseline_sigma,
                    self.label_baseline_thresh, self.label_baseline_artifacts):
            lab.setFont(QFont("Arial", 10))
            lab.setStyleSheet("color: #cccccc;")
            vv.addWidget(lab)
        v.addWidget(values)

        # Signal charts: EDA, HR, HRV, ECG stacked
        self.plot_eda = self._create_signal_plot("EDA (uS)", "#00ff66",
                                                  y_range=Config.EDA_PLOT_DEFAULT_RANGE)
        self.plot_hr = self._create_signal_plot("HR (BPM)", "#ff9933",
                                                  y_range=Config.HR_PLOT_DEFAULT_RANGE)
        self.plot_hrv = self._create_signal_plot("HRV (ms)", "#33aaff",
                                                  y_range=Config.HRV_PLOT_DEFAULT_RANGE)
        self.plot_ecg = self._create_signal_plot("ECG (mV)", "#ff66ff",
                                                  y_range=(-1.5, 1.5))
        for p in (self.plot_eda, self.plot_hr, self.plot_hrv, self.plot_ecg):
            v.addWidget(p)

        return group

    def _build_live_panel(self):
        """Right panel: live buttons, numeric strip, stress + deltas charts."""
        group = QGroupBox("Live Session")
        group.setStyleSheet("QGroupBox { color: #ff9933; font-weight: bold; "
                            "border: 1px solid #333; border-radius: 4px; "
                            "margin-top: 8px; padding-top: 14px; }"
                            "QGroupBox::title { left: 10px; padding: 0 4px; }")
        v = QVBoxLayout(group)
        v.setSpacing(8)

        # Buttons (2: Start + Stop) — symmetric with the baseline panel.
        # Clicking Start Live again after a Stop rotates to a fresh
        # session CSV (handled by main.py), so each live run gets its
        # own file without a separate Restart button.
        btn_row = QHBoxLayout()
        self.btn_live_start = QPushButton("Start Live Session")
        self.btn_live_stop  = QPushButton("Stop")
        for b in (self.btn_live_start, self.btn_live_stop):
            b.setMinimumHeight(34)
            btn_row.addWidget(b)
        self.btn_live_start.clicked.connect(
            lambda: self.control.send(Command.LIVE_START))
        self.btn_live_stop.clicked.connect(
            lambda: self.control.send(Command.LIVE_STOP))
        v.addLayout(btn_row)

        # Per-panel live duration. Free-running once Start Live is clicked,
        # freezes on Stop, resets on Restart.
        self.label_live_duration = QLabel("Live duration: 00:00")
        self.label_live_duration.setFont(QFont("Arial", 11, QFont.Bold))
        self.label_live_duration.setStyleSheet("color: #00ff00;")
        v.addWidget(self.label_live_duration)

        # Numeric strip
        strip = QGroupBox("Current state")
        strip.setStyleSheet("QGroupBox { color: #888888; border: none; }")
        sv = QVBoxLayout(strip)
        sv.setSpacing(2)
        self.label_state = QLabel("CALM")
        self.label_state.setFont(QFont("Arial", 16, QFont.Bold))
        self.label_state.setAlignment(Qt.AlignCenter)
        self.label_s_t = QLabel("S_t: --   Score: --")
        self.label_deltas = QLabel("dEDA: -- %   dHR: -- %   dHRV: -- %")
        self.label_thresholds = QLabel("Thresholds: MILD = --, HIGH = --")
        self.label_time_calm   = QLabel("Time CALM:     00:00")
        self.label_time_stress = QLabel("Time STRESSED: 00:00")
        self.label_time_ultra  = QLabel("Time ULTRA:    00:00")
        self.label_gate = QLabel("")
        # Unity command tracker — surfaces what the UDP bridge most
        # recently sent so the operator can see the throttle is real
        # (and confirm Unity is being driven by the patient's state).
        self.label_unity = QLabel("Unity: (no commands sent yet)")
        for lab in (self.label_state, self.label_s_t, self.label_deltas,
                    self.label_thresholds, self.label_time_calm,
                    self.label_time_stress, self.label_time_ultra,
                    self.label_gate, self.label_unity):
            if lab is not self.label_state:
                lab.setFont(QFont("Arial", 10))
                lab.setStyleSheet("color: #cccccc;")
            sv.addWidget(lab)
        v.addWidget(strip)

        # Stress chart
        self.plot_stress = self._create_stress_plot()
        v.addWidget(self.plot_stress, 2)
        # Deltas chart
        self.plot_deltas = self._create_deltas_plot()
        v.addWidget(self.plot_deltas, 1)

        return group

    def _build_qa_strip(self):
        """Bottom-of-screen data-quality counters."""
        layout = QHBoxLayout()
        group = QGroupBox("Data Quality")
        group.setStyleSheet("QGroupBox { color: #ff6600; font-weight: bold; }")
        gv = QHBoxLayout(group)
        self.label_samples = QLabel("Samples: 0")
        self.label_qa_invalid = QLabel("Invalid: 0")
        self.label_qa_oor     = QLabel("Out-of-range: 0")
        self.label_qa_disc    = QLabel("Disconnects: 0")
        for lab in (self.label_samples, self.label_qa_invalid,
                    self.label_qa_oor, self.label_qa_disc):
            lab.setFont(QFont("Arial", 10))
            lab.setStyleSheet("color: #888888; margin-right: 18px;")
            gv.addWidget(lab)
        gv.addStretch()
        layout.addWidget(group)
        return layout

    # ------------------------------------------------------------------
    # Chart helpers (unchanged from previous version)
    # ------------------------------------------------------------------

    def _create_stress_plot(self):
        plot_widget = pg.PlotWidget(
            title="Real-Time Stress Index (S_t)",
            labels={'left': 'Stress Level', 'bottom': 'Time (samples)'}
        )
        plot_widget.showGrid(x=True, y=True)
        plot_widget.setMouseEnabled(x=True, y=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        plot_widget.setMenuEnabled(False)
        plot_widget.hideButtons()
        plot_widget.setStyleSheet("border: 1px solid #333;")
        self.mild_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen('#ffff00', style=pg.QtCore.Qt.DashLine, width=2),
            label='MILD = {value:0.2f}',
            labelOpts={'color': '#ffff00', 'position': 0.92,
                       'movable': False, 'fill': (0, 0, 0, 160)})
        self.high_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen('#ff0000', style=pg.QtCore.Qt.DashLine, width=2),
            label='HIGH = {value:0.2f}',
            labelOpts={'color': '#ff0000', 'position': 0.92,
                       'movable': False, 'fill': (0, 0, 0, 160)})
        plot_widget.addItem(self.mild_line)
        plot_widget.addItem(self.high_line)
        self.curve_stress = plot_widget.plot([], [], pen=pg.mkPen('#ffffff', width=2))
        self.stress_plot = plot_widget
        return plot_widget

    def _create_deltas_plot(self):
        plot_widget = pg.PlotWidget(
            title="Component Deltas (% from baseline)",
            labels={'left': 'Delta (%)', 'bottom': 'Time (samples)'}
        )
        plot_widget.showGrid(x=True, y=True)
        plot_widget.setMouseEnabled(x=True, y=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        plot_widget.setYRange(-50, 150, padding=0)
        plot_widget.setMenuEnabled(False)
        plot_widget.hideButtons()
        plot_widget.setStyleSheet("border: 1px solid #333;")
        zero = pg.InfiniteLine(angle=0, pos=0,
                                pen=pg.mkPen('#666666',
                                             style=pg.QtCore.Qt.DashLine, width=1))
        plot_widget.addItem(zero)
        # phEDA: phasic EDA in microsiemens (PDF Cause 1). dHR / dHRV:
        # percent deviation from baseline. Different units on the same chart
        # is intentional — what matters here is each signal's deviation
        # direction over time, not absolute scale comparison.
        self.curve_delta_eda = plot_widget.plot([], [], pen=pg.mkPen('#00ff66', width=1.5), name='phEDA µS')
        self.curve_delta_hr  = plot_widget.plot([], [], pen=pg.mkPen('#ff9933', width=1.5), name='dHR %')
        self.curve_delta_hrv = plot_widget.plot([], [], pen=pg.mkPen('#33aaff', width=1.5), name='dHRV %')
        plot_widget.addLegend(offset=(10, 10))
        return plot_widget

    def _create_signal_plot(self, title: str, color: str, y_range=None):
        plot_widget = pg.PlotWidget(
            title=title,
            labels={'left': title, 'bottom': 'Time (samples)'}
        )
        plot_widget.showGrid(x=True, y=True)
        plot_widget.setMouseEnabled(x=True, y=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        plot_widget.setMenuEnabled(False)
        plot_widget.hideButtons()
        if y_range is not None:
            plot_widget.setYRange(*y_range, padding=0)
        plot_widget.setStyleSheet("border: 1px solid #333;")
        curve = plot_widget.plot([], [], pen=pg.mkPen(color, width=1.5))
        plot_widget.curve = curve
        plot_widget.title_text = title
        plot_widget.locked_y_range = y_range
        return plot_widget

    # ------------------------------------------------------------------
    # Style
    # ------------------------------------------------------------------

    def _stylesheet(self):
        return """
        QWidget { background-color: #1a1a1a; color: #ffffff; }
        QGroupBox { font-weight: bold; }
        QPushButton {
            background-color: #2a2a2a; color: #ffffff;
            border: 1px solid #555; border-radius: 4px;
            padding: 6px 12px; font-weight: bold;
        }
        QPushButton:hover:enabled  { background-color: #3a3a3a; }
        QPushButton:pressed:enabled { background-color: #0099ff; }
        QPushButton:disabled {
            background-color: #1a1a1a; color: #555555;
            border: 1px solid #333;
        }
        """

    # ------------------------------------------------------------------
    # Button state machine
    # ------------------------------------------------------------------

    def _apply_button_states(self, state: SessionState):
        bl, lv = _BUTTON_STATES.get(state, ((False, False), (False, False)))
        self.btn_baseline_start.setEnabled(bl[0])
        self.btn_baseline_stop.setEnabled(bl[1])
        self.btn_live_start.setEnabled(lv[0])
        self.btn_live_stop.setEnabled(lv[1])

        phase_text, phase_color = {
            SessionState.IDLE:          ("Phase: IDLE",          "#aaaaaa"),
            SessionState.BASELINE:      ("Phase: BASELINE",      "#ffff00"),
            SessionState.BASELINE_DONE: ("Phase: BASELINE DONE", "#0099ff"),
            SessionState.LIVE:          ("Phase: LIVE",          "#00ff00"),
            SessionState.STOPPED:       ("Phase: STOPPED",       "#888888"),
        }[state]
        self.label_phase.setText(phase_text)
        self.label_phase.setStyleSheet(f"color: {phase_color}; font-weight: bold;")

        banner_text, banner_color = _STATE_BANNERS.get(
            state, ("Ready.", "#aaaaaa"))
        self.label_status.setText(banner_text)
        self.label_status.setStyleSheet(f"color: {banner_color}; font-weight: bold;")

    # ------------------------------------------------------------------
    # 50 Hz update
    # ------------------------------------------------------------------

    def update_dashboard(self):
        # ECG side stream is pumped FIRST, independent of main-inlet
        # timing. The main inlet sometimes returns 0 samples on a given
        # 20 ms fire (50 Hz publish vs 50 Hz consume isn't exactly in
        # phase). If we early-return on no main samples, the ECG chart
        # stops drawing for that fire, and over time the ECG inlet backs
        # up enough that the chart goes blank. Pumping ECG independently
        # keeps the trace continuous regardless of main-inlet jitter.
        self._update_ecg_chart()

        # Drain the main inlet to the most recent sample. update_dashboard
        # does a lot of work (5-6 pyqtgraph curves, label updates, etc.)
        # and on slower machines can't always finish inside the 20 ms
        # timer slot. When it slips, samples queue in the LSL inlet, and
        # any UI driven by channel 20 (session_state) goes stale by
        # however many samples we're behind — concretely, clicking Start
        # Baseline appeared to do nothing for seconds because the
        # dashboard was still consuming pre-click IDLE samples. Taking
        # only the latest keeps the UI responsive at the cost of skipping
        # intermediate chart frames.
        samples, _ = self.inlet.pull_chunk(timeout=0.0)
        if not samples:
            return
        n_samples_drained = len(samples)
        sample = samples[-1]
        if len(sample) < 24:
            return

        # Channel layout (22 channels — see UnityBridge.CHANNELS):
        s_t             = sample[0]
        state_enum      = sample[1]
        dashboard_score = sample[2]
        eda             = sample[3]
        hr              = sample[4]
        hrv             = sample[5]
        delta_eda       = sample[6]
        delta_hr        = sample[7]
        delta_hrv       = sample[8]
        avg_eda         = sample[9]
        avg_hr          = sample[10]
        avg_hrv         = sample[11]
        thresh_mild     = sample[12]
        thresh_high     = sample[13]
        baseline_status = sample[14]
        baseline_elapsed_sec = float(sample[15])
        qa_invalid       = int(sample[16])
        qa_out_of_range  = int(sample[17])
        qa_disconnects   = int(sample[18])
        udp_gate_open    = bool(sample[19] >= 0.5)
        session_state    = SessionState(int(sample[20]))
        live_elapsed_sec = float(sample[21])
        unity_cmd_code   = int(sample[22])
        unity_cmd_total  = int(sample[23])

        # If main's state changed, refresh the button enable/disable picture.
        if session_state != self._last_state_observed:
            self._apply_button_states(session_state)
            self._last_state_observed = session_state

        # ---- State chip + S_t / deltas readout ----
        if state_enum == 0.0:
            state_label, state_color, bg = "CALM", "#00ff00", self.color_calm
        elif state_enum == 1.0:
            state_label, state_color, bg = "STRESSED", "#ffff00", self.color_stressed
        else:
            state_label, state_color, bg = "ULTRA STRESSED", "#ff0000", self.color_ultra

        self.label_state.setText(state_label)
        self.label_state.setStyleSheet(f"color: {state_color}; font-weight: bold;")
        self.label_s_t.setText(f"S_t: {s_t:6.2f}    Score: {dashboard_score:5.1f}/100")
        self.label_deltas.setText(
            f"phEDA: {delta_eda:+6.3f} µS   dHR: {delta_hr:+6.1f} %   dHRV: {delta_hrv:+6.1f} %"
        )

        # ---- Stress chart (only while baseline is locked) ----
        if thresh_mild > 0.0:
            self.stress_data['x'].append(self.tick_counter)
            self.stress_data['y'].append(s_t)
            if len(self.stress_data['x']) > self.max_history:
                self.stress_data['x'].pop(0); self.stress_data['y'].pop(0)
            self.curve_stress.setData(self.stress_data['x'], self.stress_data['y'])

            for buf, value, curve in (
                (self.delta_eda_data, delta_eda, self.curve_delta_eda),
                (self.delta_hr_data,  delta_hr,  self.curve_delta_hr),
                (self.delta_hrv_data, delta_hrv, self.curve_delta_hrv),
            ):
                buf['x'].append(self.tick_counter); buf['y'].append(value)
                if len(buf['x']) > self.max_history:
                    buf['x'].pop(0); buf['y'].pop(0)
                curve.setData(buf['x'], buf['y'])

            self.plot_deltas.setYRange(-50, 150, padding=0)

        x_min = max(0, self.tick_counter - self.view_width)
        self.stress_plot.setXRange(x_min, self.tick_counter)
        self.plot_deltas.setXRange(x_min, self.tick_counter)
        self.stress_plot.getViewBox().setBackgroundColor(bg)

        # ---- Signal charts ----
        for buf, value, plot in (
            (self.eda_data, eda, self.plot_eda),
            (self.hr_data,  hr,  self.plot_hr),
            (self.hrv_data, hrv, self.plot_hrv),
        ):
            buf['x'].append(self.tick_counter); buf['y'].append(value)
            if len(buf['x']) > self.max_history:
                buf['x'].pop(0); buf['y'].pop(0)
            plot.curve.setData(buf['x'], buf['y'])
            plot.setXRange(x_min, self.tick_counter)
            if plot.locked_y_range is not None:
                plot.setYRange(*plot.locked_y_range, padding=0)

        # ECG is now drawn by _update_ecg_chart() called at the top of
        # update_dashboard, before the main-inlet early return.

        # ---- Captured baseline display + threshold lines on stress chart ----
        if avg_eda > 0.0 and avg_hr > 0.0:
            self.label_baseline_eda.setText(f"EDA: {avg_eda:.2f} uS")
            self.label_baseline_hr.setText(f"HR:  {avg_hr:.2f} BPM")
            self.label_baseline_hrv.setText(f"HRV: {avg_hrv:.2f} ms")

            if not self._signal_ranges_centered:
                eda_range = (max(0.0, avg_eda - Config.EDA_PLOT_HALFRANGE),
                             avg_eda + Config.EDA_PLOT_HALFRANGE)
                hr_range  = (max(0.0, avg_hr  - Config.HR_PLOT_HALFRANGE),
                             avg_hr + Config.HR_PLOT_HALFRANGE)
                hrv_range = (max(0.0, avg_hrv - Config.HRV_PLOT_HALFRANGE),
                             avg_hrv + Config.HRV_PLOT_HALFRANGE)
                self.plot_eda.locked_y_range = eda_range
                self.plot_hr.locked_y_range  = hr_range
                self.plot_hrv.locked_y_range = hrv_range
                self.plot_eda.setYRange(*eda_range, padding=0)
                self.plot_hr.setYRange(*hr_range, padding=0)
                self.plot_hrv.setYRange(*hrv_range, padding=0)
                self._signal_ranges_centered = True

        if thresh_mild > 0.0 and self.mild_line.value() != thresh_mild:
            self.mild_line.setValue(thresh_mild)
        if thresh_high > 0.0 and self.high_line.value() != thresh_high:
            self.high_line.setValue(thresh_high)
        if thresh_mild > 0.0 and thresh_high > 0.0:
            t = f"Thresholds: MILD = {thresh_mild:.2f}, HIGH = {thresh_high:.2f}"
            self.label_thresholds.setText(t)
            self.label_baseline_sigma.setText(
                f"sigma_baseline: {thresh_mild / Config.THRESH_MILD_K:.3f}"
            )
            self.label_baseline_thresh.setText(t)

        # ---- UDP gate indicator ----
        if session_state == SessionState.LIVE and not udp_gate_open:
            self.label_gate.setText("UDP gate: WAITING for first calm reading")
            self.label_gate.setStyleSheet("color: #ff9933; font-weight: bold;")
        elif session_state == SessionState.LIVE and udp_gate_open:
            self.label_gate.setText("UDP gate: ACTIVE -- commands flowing to Unity")
            self.label_gate.setStyleSheet("color: #888888;")
        else:
            self.label_gate.setText("")

        # ---- Unity last-command tracker ----
        # _CODE_TO_NAME mirrors _COMMAND_CODES in unity_bridge.py; both
        # decode the integer on channel 22 into the human-readable name.
        _CODE_TO_NAME = {0: "(none)", 1: "INCREASE", 2: "DECREASE",
                         3: "START", 4: "STOP"}
        cmd_name = _CODE_TO_NAME.get(unity_cmd_code, f"?{unity_cmd_code}")
        # Per-state-change colouring so increases/decreases stand out
        # from each other and from lifecycle events.
        cmd_color = {
            "INCREASE": "#00ff66",
            "DECREASE": "#ff6666",
            "START":    "#0099ff",
            "STOP":     "#aaaaaa",
            "(none)":   "#666666",
        }.get(cmd_name, "#ffffff")
        if session_state in (SessionState.LIVE, SessionState.STOPPED):
            self.label_unity.setText(
                f"Unity last command: {cmd_name}     total sent: {unity_cmd_total}"
            )
            self.label_unity.setStyleSheet(f"color: {cmd_color}; font-weight: bold;")
        else:
            self.label_unity.setText("")

        # ---- Time-in-state (LIVE only) ----
        # Count every sample we just drained (not 1 per timer fire),
        # otherwise dropped-frame periods make these counters drift well
        # below the true LIVE duration. State changes are slow enough
        # that attributing a whole chunk to the latest sample's state is
        # accurate to a few hundred ms at worst.
        if session_state == SessionState.LIVE:
            if state_enum == 0.0:
                self.ticks_calm += n_samples_drained
            elif state_enum == 1.0:
                self.ticks_stressed += n_samples_drained
            else:
                self.ticks_ultra += n_samples_drained

        def _fmt(t):
            s = t // int(Config.PIPELINE_RATE)
            return f"{s // 60:02d}:{s % 60:02d}"
        self.label_time_calm.setText(f"Time CALM:     {_fmt(self.ticks_calm)}")
        self.label_time_stress.setText(f"Time STRESSED: {_fmt(self.ticks_stressed)}")
        self.label_time_ultra.setText(f"Time ULTRA:    {_fmt(self.ticks_ultra)}")

        # ---- Per-panel durations + QA strip ----
        if self.tick_counter % 10 == 0:
            b_sec = int(baseline_elapsed_sec)
            total_baseline = int(Config.BASELINE_SEC)
            self.label_baseline_duration.setText(
                f"Baseline duration: {b_sec // 60:02d}:{b_sec % 60:02d} "
                f"/ {total_baseline // 60:02d}:{total_baseline % 60:02d}"
            )
            l_sec = int(live_elapsed_sec)
            self.label_live_duration.setText(
                f"Live duration: {l_sec // 60:02d}:{l_sec % 60:02d}"
            )

        def _qa_color(n):
            return "color: #ff6666; margin-right: 18px;" if n > 0 else "color: #888888; margin-right: 18px;"
        self.label_samples.setText(f"Samples: {self.tick_counter}")
        self.label_qa_invalid.setText(f"Invalid: {qa_invalid}")
        self.label_qa_invalid.setStyleSheet(_qa_color(qa_invalid))
        self.label_qa_oor.setText(f"Out-of-range: {qa_out_of_range}")
        self.label_qa_oor.setStyleSheet(_qa_color(qa_out_of_range))
        self.label_qa_disc.setText(f"Disconnects: {qa_disconnects}")
        self.label_qa_disc.setStyleSheet(_qa_color(qa_disconnects))

        # If a reset just happened (state went back to IDLE), clear our buffers
        if session_state == SessionState.IDLE and self._signal_ranges_centered:
            self._reset_dashboard_buffers()

        self.tick_counter += 1

    def _update_ecg_chart(self):
        """Drain the ECG side stream and append every sample to the chart
        buffer. Called from the top of update_dashboard so ECG keeps
        flowing even on timer fires where the main inlet returns 0
        samples (which would otherwise early-return out of the whole
        update and starve this chart, leaving it visibly blank during
        LIVE while the rest of the dashboard is fine)."""
        if self.ecg_inlet is None:
            return
        ecg_samples, _ = self.ecg_inlet.pull_chunk(timeout=0.0)
        if not ecg_samples:
            return
        for s in ecg_samples:
            self.ecg_data['x'].append(self.ecg_tick)
            self.ecg_data['y'].append(s[0])
            self.ecg_tick += 1
            if len(self.ecg_data['x']) > self.max_history * 4:
                self.ecg_data['x'].pop(0); self.ecg_data['y'].pop(0)
        self.plot_ecg.curve.setData(self.ecg_data['x'], self.ecg_data['y'])
        self.plot_ecg.setXRange(max(0, self.ecg_tick - self.view_width * 4),
                                 self.ecg_tick)
        if self.plot_ecg.locked_y_range is not None:
            self.plot_ecg.setYRange(*self.plot_ecg.locked_y_range, padding=0)

    def _reset_dashboard_buffers(self):
        """Clear chart buffers when the operator resets baseline."""
        for buf in (self.stress_data, self.eda_data, self.hr_data,
                    self.hrv_data, self.delta_eda_data, self.delta_hr_data,
                    self.delta_hrv_data, self.ecg_data):
            buf['x'].clear(); buf['y'].clear()
        self.ticks_calm = self.ticks_stressed = self.ticks_ultra = 0
        self._signal_ranges_centered = False
        # Reset per-signal Y ranges back to defaults
        self.plot_eda.locked_y_range = Config.EDA_PLOT_DEFAULT_RANGE
        self.plot_hr.locked_y_range  = Config.HR_PLOT_DEFAULT_RANGE
        self.plot_hrv.locked_y_range = Config.HRV_PLOT_DEFAULT_RANGE
        for lab in (self.label_baseline_eda, self.label_baseline_hr,
                    self.label_baseline_hrv):
            lab.setText(lab.text().split(":")[0] + ": -- " + lab.text().split()[-1])
        self.label_baseline_sigma.setText("sigma_baseline: --")
        self.label_baseline_thresh.setText("Thresholds: MILD = --, HIGH = --")
        self.mild_line.setValue(0)
        self.high_line.setValue(0)

    # ------------------------------------------------------------------
    # Close-window prompt
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        """
        Hooked onto the window via self.win.closeEvent = self.closeEvent.

        Behaviour by current state:
          IDLE                       -> no data, close immediately.
          BASELINE                   -> partial baseline, nothing saved yet;
                                        ask Discard / Cancel.
          BASELINE_DONE              -> baseline exists, no live data;
                                        ask Save Baseline / Discard / Cancel.
          LIVE or STOPPED            -> baseline + (partial or finished) live
                                        data; ask Save Both / Keep Baseline
                                        Only / Discard Both / Cancel.

        The chosen action is encoded as a SHUTDOWN_* command and sent to
        main.py, which performs the file-level deletes and exits. The
        dashboard then closes; the launcher's poll-loop kills any survivors.
        """
        state = self._last_state_observed

        if state == SessionState.IDLE:
            event.accept()
            return

        msg = QMessageBox(self.win)
        msg.setWindowTitle("Closing — what should we do with the data?")
        msg.setIcon(QMessageBox.Question)

        if state == SessionState.BASELINE:
            msg.setText("A baseline is in progress and has not been saved.\n"
                        "Closing will discard the in-flight baseline.")
            discard_btn = msg.addButton("Discard and close",
                                        QMessageBox.AcceptRole)
            cancel_btn  = msg.addButton("Cancel",
                                        QMessageBox.RejectRole)
            msg.setDefaultButton(cancel_btn)
            msg.exec_()
            clicked = msg.clickedButton()
            if clicked is cancel_btn:
                event.ignore(); return
            self.control.send(Command.SHUTDOWN_DISCARD_BOTH)
            self._await_main_shutdown()
            event.accept(); return

        if state == SessionState.BASELINE_DONE:
            msg.setText("Baseline has been captured. No live session was run.\n"
                        "Keep the baseline file?")
            save_btn    = msg.addButton("Save baseline",
                                        QMessageBox.AcceptRole)
            discard_btn = msg.addButton("Discard everything",
                                        QMessageBox.DestructiveRole)
            cancel_btn  = msg.addButton("Cancel",
                                        QMessageBox.RejectRole)
            msg.setDefaultButton(save_btn)
            msg.exec_()
            clicked = msg.clickedButton()
            if clicked is cancel_btn:
                event.ignore(); return
            if clicked is discard_btn:
                self.control.send(Command.SHUTDOWN_DISCARD_BOTH)
            else:
                self.control.send(Command.SHUTDOWN_SAVE_BOTH)
            self._await_main_shutdown()
            event.accept(); return

        # LIVE or STOPPED: baseline file + session CSV both exist.
        live_label = "live session" if state == SessionState.LIVE else "stopped session"
        msg.setText(f"Baseline and {live_label} data are on disk.\n"
                    f"How would you like to handle them?")
        save_both_btn   = msg.addButton("Save both",
                                        QMessageBox.AcceptRole)
        keep_base_btn   = msg.addButton("Keep baseline only (discard live)",
                                        QMessageBox.AcceptRole)
        discard_both_btn = msg.addButton("Discard both",
                                         QMessageBox.DestructiveRole)
        cancel_btn      = msg.addButton("Cancel",
                                        QMessageBox.RejectRole)
        msg.setDefaultButton(save_both_btn)
        msg.exec_()
        clicked = msg.clickedButton()
        if clicked is cancel_btn:
            event.ignore(); return
        if clicked is save_both_btn:
            self.control.send(Command.SHUTDOWN_SAVE_BOTH)
        elif clicked is keep_base_btn:
            self.control.send(Command.SHUTDOWN_DISCARD_LIVE)
        else:
            self.control.send(Command.SHUTDOWN_DISCARD_BOTH)
        self._await_main_shutdown()
        event.accept()

    def _await_main_shutdown(self, delay_sec: float = 0.4):
        """Brief pause after pushing a SHUTDOWN_* command so main.py has
        time to read it from the LSL inlet, run the file-deletion side
        effects, and exit cleanly. Without this, the dashboard exits
        instantly, the launcher's poll-loop sees the dashboard die and
        calls p.terminate() on main *before* main has processed the
        command — the discard never happens."""
        try:
            time.sleep(delay_sec)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self):
        # Route window-X clicks into our save/discard prompt.
        self.win.closeEvent = self.closeEvent
        self.win.show()
        sys.exit(self.app.exec_())


if __name__ == '__main__':
    print("\n[DASHBOARD] Starting Clinical Dashboard...")
    print("[DASHBOARD] Waiting for data stream from main.py...\n")
    try:
        dash = ClinicalDashboard()
        dash.run()
    except Exception as e:
        print(f"\n[ERROR] Dashboard failed to start: {str(e)}")
        print("Ensure main.py is running and broadcasting on LSL.")
