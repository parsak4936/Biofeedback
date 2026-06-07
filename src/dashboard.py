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

        # Optional ECG side stream. Lazy-attached because MockDataSource
        # creates this outlet after the main pipeline opens its own (we
        # resolve `Biofeedback_State` above first, then the data source
        # does ~3-4 s of file load + R-peak detection before publishing
        # `OpenSignals_ECG`). The dashboard previously resolved this
        # once at init and gave up — leaving the ECG chart permanently
        # empty. Now we try once at startup, then retry inside the
        # update tick every ECG_RETRY_INTERVAL_SEC until it appears.
        self.ecg_inlet = None
        self._ecg_last_retry_time = 0.0
        self._ecg_retry_interval_sec = 2.0
        self._try_attach_ecg(initial=True)

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
        self.height_data = {'x': [], 'y': []}
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

        # Background tints for the stress chart per state. Very subtle --
        # operator should perceive the state shift from peripheral vision
        # without being distracted by it. Tuned to roughly match the
        # desaturated stop-light card palette.
        self.color_calm     = pg.mkColor(24, 32, 28)
        self.color_stressed = pg.mkColor(36, 32, 22)
        self.color_ultra    = pg.mkColor(40, 24, 24)

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
        layout = QVBoxLayout()
        patient_name = os.environ.get('PATIENT_NAME', 'PATIENT')
        patient_id = os.environ.get('PATIENT_ID', '000')

        # ---- Top row: patient / phase / status ----
        top = QHBoxLayout()
        self.label_patient = QLabel(f"Patient: {patient_name} ({patient_id})")
        self.label_patient.setFont(QFont("Arial", 13, QFont.Bold))
        self.label_patient.setStyleSheet("color: #e0e0e0;")

        self.label_phase = QLabel("Phase: IDLE")
        self.label_phase.setFont(QFont("Arial", 11, QFont.Bold))
        self.label_phase.setStyleSheet("color: #9a9a9a;")

        # The status banner mirrors the [STATE] transitions printed in the
        # terminal so the operator sees the same news on screen.
        self.label_status = QLabel("Ready. Click Start Baseline to begin.")
        self.label_status.setFont(QFont("Arial", 11))
        self.label_status.setStyleSheet("color: #9a9a9a;")
        self.label_status.setWordWrap(True)

        top.addWidget(self.label_patient, 2)
        top.addWidget(self.label_phase, 1)
        top.addWidget(self.label_status, 3)
        layout.addLayout(top)

        # ---- Bottom row: live numeric readout. ALWAYS visible (including
        # during IDLE / BASELINE) so the operator can confirm signals are
        # flowing before clicking Start Baseline. Updated every tick with
        # the latest EDA / HR / HRV / ECG values from the LSL stream. ----
        live_row = QHBoxLayout()
        live_row.setSpacing(16)
        self.label_live_readout_title = QLabel("Live signals")
        self.label_live_readout_title.setFont(QFont("Arial", 10, QFont.Bold))
        self.label_live_readout_title.setStyleSheet("color: #6fb3d4;")

        self.label_live_eda = QLabel("EDA: -- uS")
        self.label_live_hr  = QLabel("HR: -- BPM")
        self.label_live_hrv = QLabel("HRV: -- ms")
        self.label_live_ecg = QLabel("ECG: -- mV")
        self.label_live_phasic = QLabel("phEDA: -- uS")
        for lab in (self.label_live_eda, self.label_live_hr,
                    self.label_live_hrv, self.label_live_ecg,
                    self.label_live_phasic):
            lab.setFont(QFont("Consolas", 11, QFont.Bold))
            lab.setStyleSheet("color: #e0e0e0;")
        live_row.addWidget(self.label_live_readout_title)
        live_row.addWidget(self.label_live_eda)
        live_row.addWidget(self.label_live_hr)
        live_row.addWidget(self.label_live_hrv)
        live_row.addWidget(self.label_live_phasic)
        live_row.addWidget(self.label_live_ecg)
        live_row.addStretch()
        layout.addLayout(live_row)

        return layout

    def _build_baseline_panel(self):
        """Left panel: baseline buttons, captured values, signal charts."""
        group = QGroupBox("Baseline Calibration")
        # Inherit the global stylesheet (calmer chrome); only override the
        # title accent so the two panels read as different sections at a
        # glance.
        group.setStyleSheet("QGroupBox::title { color: #6fb3d4; }")
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

        # Captured-values readout (populated when baseline locks).
        # EDA / HR / HRV get rendered as large card-style labels so they
        # are visible from across the room. Sigma / thresholds / artifact
        # counts stay as the smaller summary lines below.
        values = QGroupBox("Personal Baseline (captured)")
        values.setStyleSheet("QGroupBox { color: #888888; border: none; }")
        vv = QVBoxLayout(values)
        vv.setSpacing(6)

        card_row = QHBoxLayout()
        card_row.setSpacing(10)
        # Calmer outlined-card style: transparent fill, soft accent border
        # and text in a single cool palette. No competing primaries.
        _BASELINE_CARD_QSS = (
            "QLabel {{"
            " background-color: transparent;"
            " border: 1px solid {edge};"
            " border-radius: 6px;"
            " padding: 12px 6px;"
            " color: {edge};"
            " font-weight: 600;"
            "}}"
        )
        self.label_baseline_eda = QLabel("EDA\n-- uS")
        self.label_baseline_hr  = QLabel("HR\n-- BPM")
        self.label_baseline_hrv = QLabel("HRV\n-- ms")
        for lab, edge in (
            (self.label_baseline_eda, "#6fd1a8"),
            (self.label_baseline_hr,  "#d9a86c"),
            (self.label_baseline_hrv, "#6fb3d4"),
        ):
            lab.setFont(QFont("Arial", 16, QFont.Bold))
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet(_BASELINE_CARD_QSS.format(edge=edge))
            card_row.addWidget(lab, 1)
        vv.addLayout(card_row)

        self.label_baseline_sigma = QLabel("sigma_baseline: --")
        self.label_baseline_thresh = QLabel("Thresholds: MILD = --, HIGH = --")
        self.label_baseline_artifacts = QLabel("Artifacts: EDA=-- HR=-- HRV=--")
        for lab in (self.label_baseline_sigma,
                    self.label_baseline_thresh,
                    self.label_baseline_artifacts):
            lab.setFont(QFont("Arial", 10))
            lab.setStyleSheet("color: #cccccc;")
            vv.addWidget(lab)
        v.addWidget(values)

        # Signal charts laid out in a 2-row x 2-col grid instead of
        # the previous tall stack. Each chart now occupies a quarter
        # of the panel rather than ~1/4 vertical, which roughly doubles
        # the height available to each curve and keeps them readable
        # when the panel is short. Charts auto-rescale dynamically
        # (see _autoscale_signal_chart in update_dashboard) so a
        # small EDA rise or fall stays visible.
        from PyQt5.QtWidgets import QGridLayout
        # Calmer monochromatic-with-accent palette: each signal gets a soft
        # accent colour, all sharing the same lightness so no single chart
        # dominates the operator's eye. Titles are explicit about the unit.
        self.plot_eda = self._create_signal_plot("Raw EDA  (uS)", "#6fd1a8",
                                                  y_range=Config.EDA_PLOT_DEFAULT_RANGE)
        self.plot_hr = self._create_signal_plot("HR  (BPM)", "#d9a86c",
                                                  y_range=Config.HR_PLOT_DEFAULT_RANGE)
        self.plot_hrv = self._create_signal_plot("HRV / RMSSD  (ms)", "#6fb3d4",
                                                  y_range=Config.HRV_PLOT_DEFAULT_RANGE)
        self.plot_ecg = self._create_signal_plot("ECG  (mV)", "#c992c9",
                                                  y_range=(-1.5, 1.5))
        signal_grid = QGridLayout()
        signal_grid.setSpacing(6)
        signal_grid.addWidget(self.plot_eda, 0, 0)
        signal_grid.addWidget(self.plot_hr,  0, 1)
        signal_grid.addWidget(self.plot_hrv, 1, 0)
        signal_grid.addWidget(self.plot_ecg, 1, 1)
        v.addLayout(signal_grid, 1)

        return group

    def _build_live_panel(self):
        """Right panel: live buttons, numeric strip, stress + deltas charts."""
        group = QGroupBox("Live Session")
        group.setStyleSheet("QGroupBox::title { color: #d4b86a; }")
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

        # Numeric strip. All readouts are outlined-card style now
        # (transparent fill, coloured border + text), per the sketch.
        # Layout, top to bottom:
        #   row 1: three TIME-IN-STATE cards (CALM / STRESSED / ULTRA)
        #   row 2: three SMALL info cards    (S_t  / Score   / Height)
        #   row 3: big CURRENT STATE card (sits right above the charts)
        #   row 4: small summary lines (thresholds / gate / Unity cmd)
        strip = QGroupBox("Current state")
        strip.setStyleSheet("QGroupBox { color: #888888; border: none; }")
        sv = QVBoxLayout(strip)
        sv.setSpacing(8)

        _CARD_QSS = (
            "QLabel {{"
            " background-color: transparent;"
            " border: 1px solid {edge};"
            " border-radius: 6px;"
            " padding: {pad};"
            " color: {edge};"
            " font-weight: 600;"
            "}}"
        )

        # ---- Row 1: time-in-state cards. Stop-light palette but
        # desaturated so the room stays calm to look at. ----
        time_row = QHBoxLayout()
        time_row.setSpacing(10)
        self.label_time_calm   = QLabel("CALM\n00:00")
        self.label_time_stress = QLabel("STRESSED\n00:00")
        self.label_time_ultra  = QLabel("ULTRA\n00:00")
        for lab, edge in (
            (self.label_time_calm,   "#7bc89a"),
            (self.label_time_stress, "#d4b86a"),
            (self.label_time_ultra,  "#d68a8a"),
        ):
            lab.setFont(QFont("Arial", 13, QFont.Bold))
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet(_CARD_QSS.format(edge=edge, pad="10px 6px"))
            time_row.addWidget(lab, 1)
        sv.addLayout(time_row)

        # ---- Row 2: small info cards (S_t, Score, Height) ----
        info_row = QHBoxLayout()
        info_row.setSpacing(10)
        self.label_s_t_card    = QLabel("S_t\n--")
        self.label_score_card  = QLabel("Score\n--")
        self.label_height_card = QLabel("Height\n-- m")
        for lab, edge in (
            (self.label_s_t_card,    "#a0c4d9"),
            (self.label_score_card,  "#a0c4d9"),
            (self.label_height_card, "#d4b86a"),
        ):
            lab.setFont(QFont("Arial", 12, QFont.Bold))
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet(_CARD_QSS.format(edge=edge, pad="8px 6px"))
            info_row.addWidget(lab, 1)
        sv.addLayout(info_row)

        # ---- Row 3: BIG current-state card, sits right above the charts ----
        # Border + text recolour each tick to match the live state
        # (green = calm, yellow = stressed, red = ultra). Set the initial
        # green styling here; update_dashboard rewrites it on transitions.
        self.label_state = QLabel("CALM")
        self.label_state.setFont(QFont("Arial", 28, QFont.Bold))
        self.label_state.setAlignment(Qt.AlignCenter)
        self.label_state.setStyleSheet(_CARD_QSS.format(edge="#00ff00", pad="16px"))
        sv.addWidget(self.label_state)

        # ---- Row 4: smaller summary lines (kept text, no card) ----
        # These supplement the cards above with the long-form numeric
        # detail that does not need to be readable from across the room.
        self.label_s_t = QLabel("dEDA: -- %   dHR: -- %   dHRV: -- %")
        self.label_thresholds = QLabel("Thresholds: MILD = --, HIGH = --")
        self.label_gate = QLabel("")
        self.label_unity = QLabel("Unity: (no commands sent yet)")
        # `label_deltas` kept as an alias for backwards compat with
        # update_dashboard which still drives the deltas string.
        self.label_deltas = self.label_s_t
        # `label_height` kept as alias so the existing live-height
        # update code keeps working; it just writes to the height card now.
        self.label_height = self.label_height_card
        for lab in (self.label_s_t, self.label_thresholds,
                    self.label_gate, self.label_unity):
            lab.setFont(QFont("Arial", 10))
            lab.setStyleSheet("color: #cccccc;")
            sv.addWidget(lab)
        v.addWidget(strip)

        # ---- Chart grid: row 1 = stress | height (50/50), row 2 = deltas full-width ----
        # Per the operator's sketch: realtime index and balloon height
        # side-by-side at the top, component deltas across the bottom.
        from PyQt5.QtWidgets import QGridLayout
        self.plot_stress = self._create_stress_plot()
        self.plot_height = self._create_height_plot()
        self.plot_deltas = self._create_deltas_plot()
        chart_grid = QGridLayout()
        chart_grid.setSpacing(6)
        chart_grid.addWidget(self.plot_stress, 0, 0)
        chart_grid.addWidget(self.plot_height, 0, 1)
        chart_grid.addWidget(self.plot_deltas, 1, 0, 1, 2)  # span both columns
        chart_grid.setRowStretch(0, 2)
        chart_grid.setRowStretch(1, 1)
        v.addLayout(chart_grid, 1)

        return group

    def _build_qa_strip(self):
        """Bottom-of-screen data-quality counters."""
        layout = QHBoxLayout()
        group = QGroupBox("Data Quality")
        group.setStyleSheet("QGroupBox::title { color: #a0c4d9; }")
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
        plot_widget.showGrid(x=True, y=True, alpha=0.25)
        plot_widget.setMouseEnabled(x=True, y=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        plot_widget.setMenuEnabled(False)
        plot_widget.hideButtons()
        plot_widget.setStyleSheet("border: 1px solid #2c2c2c;")
        self.mild_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen('#d4b86a', style=pg.QtCore.Qt.DashLine, width=2),
            label='MILD = {value:0.2f}',
            labelOpts={'color': '#d4b86a', 'position': 0.92,
                       'movable': False, 'fill': (0, 0, 0, 160)})
        self.high_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen('#d68a8a', style=pg.QtCore.Qt.DashLine, width=2),
            label='HIGH = {value:0.2f}',
            labelOpts={'color': '#d68a8a', 'position': 0.92,
                       'movable': False, 'fill': (0, 0, 0, 160)})
        plot_widget.addItem(self.mild_line)
        plot_widget.addItem(self.high_line)
        self.curve_stress = plot_widget.plot([], [], pen=pg.mkPen('#e6e6e6', width=2))
        self.stress_plot = plot_widget
        return plot_widget

    def _create_deltas_plot(self):
        """Component-deltas chart. Each curve has its own unit but they
        share an axis -- what matters here is direction over time, not
        absolute comparison. phasic EDA is the score-driving signal (PDF
        Cause 1) so it's drawn thicker and gets its own colour band."""
        plot_widget = pg.PlotWidget(
            title="Component Deltas  (phasic EDA in uS, HR/HRV in %)",
            labels={'left': 'Deviation', 'bottom': 'Time (samples)'}
        )
        plot_widget.showGrid(x=True, y=True, alpha=0.25)
        plot_widget.setMouseEnabled(x=True, y=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        plot_widget.setYRange(-30, 80, padding=0)
        plot_widget.setMenuEnabled(False)
        plot_widget.hideButtons()
        plot_widget.setStyleSheet("border: 1px solid #2c2c2c;")
        zero = pg.InfiniteLine(angle=0, pos=0,
                                pen=pg.mkPen('#555555',
                                             style=pg.QtCore.Qt.DashLine, width=1))
        plot_widget.addItem(zero)
        # Calmer palette: cooler hues, phEDA emphasised as the primary curve.
        self.curve_delta_eda = plot_widget.plot(
            [], [], pen=pg.mkPen('#6fd1a8', width=2.0), name='phEDA (uS)')
        self.curve_delta_hr  = plot_widget.plot(
            [], [], pen=pg.mkPen('#d9a86c', width=1.2), name='dHR (%)')
        self.curve_delta_hrv = plot_widget.plot(
            [], [], pen=pg.mkPen('#6fb3d4', width=1.2), name='dHRV (%)')
        plot_widget.addLegend(offset=(10, 10))
        return plot_widget

    def _create_height_plot(self):
        """Live-session chart of the balloon altitude streamed back from
        Unity on UDP 5006. NaN cells (Unity not streaming) are simply
        skipped so the curve does not draw a spurious zero line."""
        plot_widget = pg.PlotWidget(
            title="Balloon Height (m, from Unity)",
            labels={'left': 'Height (m)', 'bottom': 'Time (samples)'}
        )
        plot_widget.showGrid(x=True, y=True, alpha=0.25)
        plot_widget.setMouseEnabled(x=True, y=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        plot_widget.setYRange(*Config.HEIGHT_PLOT_DEFAULT_RANGE, padding=0)
        plot_widget.setMenuEnabled(False)
        plot_widget.hideButtons()
        plot_widget.setStyleSheet("border: 1px solid #2c2c2c;")
        self.curve_height = plot_widget.plot(
            [], [], pen=pg.mkPen('#d4b86a', width=2)
        )
        return plot_widget

    def _create_signal_plot(self, title: str, color: str, y_range=None):
        plot_widget = pg.PlotWidget(
            title=title,
            labels={'left': title, 'bottom': 'Time (samples)'}
        )
        # Soft gridlines (alpha 0.25) so the curve reads clearly without
        # the grid competing for attention. Border tone matches global chrome.
        plot_widget.showGrid(x=True, y=True, alpha=0.25)
        plot_widget.setMouseEnabled(x=True, y=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        plot_widget.setMenuEnabled(False)
        plot_widget.hideButtons()
        if y_range is not None:
            plot_widget.setYRange(*y_range, padding=0)
        plot_widget.setStyleSheet("border: 1px solid #2c2c2c;")
        curve = plot_widget.plot([], [], pen=pg.mkPen(color, width=1.8))
        plot_widget.curve = curve
        plot_widget.title_text = title
        plot_widget.locked_y_range = y_range
        return plot_widget

    # ------------------------------------------------------------------
    # Style
    # ------------------------------------------------------------------

    def _stylesheet(self):
        """Restyled per operator request (2026-06): fewer accent colours,
        gentler grids, calmer chrome. Single cool-blue accent replaces
        the previous primary-colour mix; signal-state still uses
        green / amber / red but desaturated so the room stays readable.
        Typography normalised to one weight family."""
        return """
        QWidget { background-color: #181a1d; color: #e6e6e6;
                  font-family: 'Segoe UI', 'Arial', sans-serif; }
        QGroupBox { font-weight: 600;
                    border: 1px solid #2c2c2c;
                    border-radius: 6px;
                    margin-top: 10px;
                    padding-top: 14px;
                    background-color: #1d2024; }
        QGroupBox::title { left: 12px;
                           padding: 0 6px;
                           color: #a0c4d9; }
        QLabel { color: #e6e6e6; }
        QPushButton {
            background-color: #262a2f; color: #e6e6e6;
            border: 1px solid #3a3f44; border-radius: 5px;
            padding: 7px 14px; font-weight: 600;
        }
        QPushButton:hover:enabled   { background-color: #2f343a;
                                      border-color: #6fb3d4; }
        QPushButton:pressed:enabled { background-color: #6fb3d4;
                                      color: #181a1d; }
        QPushButton:disabled {
            background-color: #1d2024; color: #555a5f;
            border: 1px solid #2c2c2c;
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
        if len(sample) < 25:
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
        balloon_height_m = float(sample[24])  # NaN until Unity streams

        # If main's state changed, refresh the button enable/disable picture.
        if session_state != self._last_state_observed:
            self._apply_button_states(session_state)
            self._last_state_observed = session_state

        # ---- State chip + S_t / deltas readout ----
        # Desaturated stop-light palette matches the rest of the restyled UI.
        if state_enum == 0.0:
            state_label, state_color, bg = "CALM", "#7bc89a", self.color_calm
        elif state_enum == 1.0:
            state_label, state_color, bg = "STRESSED", "#d4b86a", self.color_stressed
        else:
            state_label, state_color, bg = "ULTRA STRESSED", "#d68a8a", self.color_ultra

        # ---- Live numeric readout (top bar). Visible in EVERY phase so the
        # operator can confirm signals are flowing before clicking Start
        # Baseline. Empty / NaN values show as "--" so the operator sees
        # exactly when each signal comes online (HR/HRV warm up over ~60 s). ----
        import math as _math
        def _fmt_v(v, unit, prec=2):
            try:
                return f"{v:.{prec}f} {unit}" if _math.isfinite(v) else f"-- {unit}"
            except Exception:
                return f"-- {unit}"
        self.label_live_eda.setText(f"EDA: {_fmt_v(eda, 'uS', 2)}")
        self.label_live_hr.setText (f"HR: {_fmt_v(hr,  'BPM', 1)}")
        self.label_live_hrv.setText(f"HRV: {_fmt_v(hrv, 'ms', 1)}")
        self.label_live_phasic.setText(f"phEDA: {_fmt_v(delta_eda, 'uS', 3)}")
        # ECG numeric: instantaneous mV from the side stream. Use the last
        # value in the side-stream buffer if present (the chart is the
        # canonical view; this is a quick "is contact good?" check).
        if self.ecg_data['y']:
            self.label_live_ecg.setText(f"ECG: {self.ecg_data['y'][-1]:+.3f} mV")
        else:
            self.label_live_ecg.setText("ECG: -- mV")

        self.label_state.setText(state_label)
        # Outlined-card restyle: transparent fill, border + text in the
        # current state's colour so it pops against the rest of the panel.
        self.label_state.setStyleSheet(
            "QLabel {"
            f" background-color: transparent;"
            f" border: 3px solid {state_color};"
            f" border-radius: 10px;"
            f" padding: 16px;"
            f" color: {state_color};"
            f" font-weight: bold;"
            "}"
        )
        # Small info cards: update S_t and Score every tick. (Height
        # card is updated below in the height-telemetry block.)
        self.label_s_t_card.setText(f"S_t\n{s_t:+.2f}")
        self.label_score_card.setText(f"Score\n{dashboard_score:.0f}/100")
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

        # ---- Captured baseline display + dynamic signal-chart zoom ----
        # Update the card text as soon as any non-zero average is
        # broadcast (channel 9/10/11 carry running averages every
        # ~10 s during BASELINE, then the locked value once at 02:00).
        if avg_eda > 0.0 or avg_hr > 0.0 or avg_hrv > 0.0:
            if avg_eda > 0.0:
                self.label_baseline_eda.setText(f"EDA\n{avg_eda:.2f} uS")
            if avg_hr > 0.0:
                self.label_baseline_hr.setText(f"HR\n{avg_hr:.1f} BPM")
            if avg_hrv > 0.0:
                self.label_baseline_hrv.setText(f"HRV\n{avg_hrv:.1f} ms")

            # Dynamic auto-rescale per chart: keep the curve centred on
            # the live mean and expand the half-range to whichever is
            # bigger between the configured default and the current data
            # span. Avoids the "EDA jumps and goes off the chart" and
            # the opposite "flat resting signal zooms to noise" problems.
            # Updated once per second (every PIPELINE_RATE ticks) so
            # the chart doesn't visibly jitter.
            if self.tick_counter % int(Config.PIPELINE_RATE) == 0:
                self._autoscale_signal(self.plot_eda, self.eda_data['y'],
                                       avg_eda, Config.EDA_PLOT_HALFRANGE,
                                       y_floor=0.0)
                self._autoscale_signal(self.plot_hr,  self.hr_data['y'],
                                       avg_hr,  Config.HR_PLOT_HALFRANGE,
                                       y_floor=0.0)
                self._autoscale_signal(self.plot_hrv, self.hrv_data['y'],
                                       avg_hrv, Config.HRV_PLOT_HALFRANGE,
                                       y_floor=0.0)

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

        # ---- Balloon-height telemetry from Unity ----
        # channel 24 carries the height in meters, NaN when Unity isn't
        # streaming. Skip the chart append when NaN so we don't draw
        # a spurious zero line, but still update the label.
        import math
        # label_height aliases label_height_card; reformat for the card
        # (two lines, big number) rather than the old inline label form.
        if math.isfinite(balloon_height_m):
            self.label_height_card.setText(f"Height\n{balloon_height_m:.1f} m")
            self.height_data['x'].append(self.tick_counter)
            self.height_data['y'].append(balloon_height_m)
            if len(self.height_data['x']) > self.max_history:
                self.height_data['x'].pop(0); self.height_data['y'].pop(0)
            self.curve_height.setData(self.height_data['x'], self.height_data['y'])
        else:
            self.label_height_card.setText("Height\n-- m")
        self.plot_height.setXRange(
            max(0, self.tick_counter - self.view_width), self.tick_counter
        )

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
        # Two-line card format matches the big-card styling applied to
        # these labels in _build_live_panel.
        self.label_time_calm.setText(f"CALM\n{_fmt(self.ticks_calm)}")
        self.label_time_stress.setText(f"STRESSED\n{_fmt(self.ticks_stressed)}")
        self.label_time_ultra.setText(f"ULTRA\n{_fmt(self.ticks_ultra)}")

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

    def _try_attach_ecg(self, initial: bool = False):
        """Best-effort resolve the OpenSignals_ECG side stream. Called once
        at startup and then periodically from the update tick (rate-limited
        by `_ecg_retry_interval_sec`) until it succeeds. The data source
        publishes this outlet a few seconds after the main pipeline comes
        up — without retry, the ECG chart stays empty for the whole run."""
        if self.ecg_inlet is not None:
            return
        import time as _time
        now = _time.time()
        if not initial and (now - self._ecg_last_retry_time) < self._ecg_retry_interval_sec:
            return
        self._ecg_last_retry_time = now
        try:
            ecg_streams = resolve_byprop("name", Config.ECG_STREAM_NAME, timeout=0.2)
            if ecg_streams:
                self.ecg_inlet = StreamInlet(ecg_streams[0])
                print(f"[DASHBOARD] Connected to ECG side stream "
                      f"'{Config.ECG_STREAM_NAME}'."
                      + ("" if initial else " (lazy retry succeeded)"))
            elif initial:
                print(f"[DASHBOARD] ECG side stream not yet present; "
                      f"will retry every {self._ecg_retry_interval_sec:.0f}s "
                      f"in the update loop.")
        except Exception as e:
            if initial:
                print(f"[DASHBOARD] ECG side stream lookup failed ({e}); "
                      f"will retry in the update loop.")

    def _autoscale_signal(self, plot, ydata, center, default_half,
                          y_floor=None, view_n=None):
        """Adaptive Y-range for one signal chart.

        Looks at the last `view_n` samples of `ydata`, centres the chart
        on `center` (the patient's baseline average, when available) or
        on the visible mean otherwise, and sets the half-range to
        whichever is larger between `default_half` and the data span.
        That gives a stable baseline view when nothing is happening AND
        keeps the curve fully visible when EDA or HR spike beyond the
        default range.

        `y_floor`: optional lower clamp (e.g. 0 so EDA does not show
        physically impossible negative axis).
        """
        if not ydata:
            return
        if view_n is None:
            view_n = self.view_width
        vals = ydata[-view_n:]
        if not vals:
            return
        vmin = min(vals)
        vmax = max(vals)
        span = vmax - vmin
        # Centre: prefer the locked / running baseline average; fall
        # back to the visible midpoint while no baseline is available.
        if center is not None and center > 0.0:
            mid = center
        else:
            mid = (vmax + vmin) / 2.0
        # Half-range: enough to cover the visible span plus 20% padding,
        # never tighter than the configured default. Hysteresis is light
        # so the chart can both expand on a spike AND contract back when
        # the signal settles.
        half = max(default_half, span * 0.6 + default_half * 0.3)
        lo = mid - half
        hi = mid + half
        if y_floor is not None:
            lo = max(y_floor, lo)
        plot.setYRange(lo, hi, padding=0)
        plot.locked_y_range = (lo, hi)

    def _update_ecg_chart(self):
        """Drain the ECG side stream and append every sample to the chart
        buffer. Called from the top of update_dashboard so ECG keeps
        flowing even on timer fires where the main inlet returns 0
        samples (which would otherwise early-return out of the whole
        update and starve this chart, leaving it visibly blank during
        LIVE while the rest of the dashboard is fine)."""
        # Lazy attach if the side stream wasn't ready when the dashboard
        # came up — the data source takes a few seconds to publish it.
        if self.ecg_inlet is None:
            self._try_attach_ecg()
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
