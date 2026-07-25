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


def _format_scene_value(v):
    """Render a telemetry value for the scene panel. Floats get 2 decimals
    unless they're clearly integers; ints stay bare; anything else is str()."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v.is_integer():
            return f"{int(v)}"
        return f"{v:.2f}"
    return str(v)

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

        # Optional telemetry side stream. Same lazy-attach pattern as ECG:
        # main.py opens the outlet at startup, we retry until it appears.
        # Feeds the scene panel (scenario label, key/value grid, primary-
        # field chart). Absent stream = scene panel stays in its "waiting
        # for Unity" empty state, physiology charts work unchanged.
        self.telemetry_inlet = None
        self._telemetry_last_retry_time = 0.0
        self._telemetry_retry_interval_sec = 2.0
        # Latest telemetry snapshot rendered on the scene panel.
        self.latest_scenario = ""
        self.latest_scene_data = {}
        # Rolling history of the primary field for the scene panel chart.
        self.scene_chart_data = {'x': [], 'y': []}
        self._try_attach_telemetry(initial=True)

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

        # Per-panel live duration. Compact inline label.
        self.label_live_duration = QLabel("Live duration: 00:00")
        self.label_live_duration.setFont(QFont("Arial", 10, QFont.Bold))
        self.label_live_duration.setStyleSheet("color: #7bc89a;")
        self.label_live_duration.setMaximumHeight(20)
        v.addWidget(self.label_live_duration)

        # ====================================================================
        # COMPACT INFO STRIP (one row, six cards). Operator request 2026-06:
        # readouts are for monitoring only -- charts are what matter. So
        # everything that used to be a giant stacked card now lives in one
        # compact horizontal strip above the charts:
        #   CALM 00:00 | STRESSED 00:00 | ULTRA 00:00 | S_t -- | Score -- | Height -- m
        # The big standalone CALM/STRESSED/ULTRA state pill is gone --
        # current state is now shown as a TextItem overlay ON the stress
        # chart (top-left corner), so the operator reads it where they're
        # already looking. See update_dashboard / _state_overlay below.
        # ====================================================================
        _CARD_QSS = (
            "QLabel {{"
            " background-color: transparent;"
            " border: 1px solid {edge};"
            " border-radius: 4px;"
            " padding: 4px 6px;"
            " color: {edge};"
            " font-weight: 600;"
            "}}"
        )

        info_row = QHBoxLayout()
        info_row.setSpacing(6)
        self.label_time_calm   = QLabel("CALM 00:00")
        self.label_time_stress = QLabel("STRESSED 00:00")
        self.label_time_ultra  = QLabel("ULTRA 00:00")
        self.label_s_t_card    = QLabel("S_t --")
        self.label_score_card  = QLabel("Score --")
        # Scenario-agnostic card. Shows the primary field of whichever
        # scenario Unity is currently emitting (height for acrophobia,
        # count for arachnophobia, looking for public_speaking, per
        # Config.SCENARIO_PRIMARY_FIELD). "Scene --" when no telemetry.
        self.label_scene_card = QLabel("Scene --")
        for lab, edge in (
            (self.label_time_calm,   "#7bc89a"),
            (self.label_time_stress, "#d4b86a"),
            (self.label_time_ultra,  "#d68a8a"),
            (self.label_s_t_card,    "#a0c4d9"),
            (self.label_score_card,  "#a0c4d9"),
            (self.label_scene_card,  "#d4b86a"),
        ):
            lab.setFont(QFont("Arial", 9, QFont.Bold))
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet(_CARD_QSS.format(edge=edge))
            lab.setMaximumHeight(26)
            info_row.addWidget(lab, 1)
        v.addLayout(info_row)

        # Tiny inline diagnostic line: deltas + Unity command, single-line.
        # Was four separate labels; merged because they're operator
        # reference only -- they shouldn't compete with the charts.
        self.label_s_t = QLabel("phEDA: --   dHR: --   dHRV: --   |   "
                                 "Thresholds: MILD --  HIGH --   |   "
                                 "Unity: (idle)")
        self.label_s_t.setFont(QFont("Arial", 9))
        self.label_s_t.setStyleSheet("color: #888888;")
        self.label_s_t.setMaximumHeight(18)
        v.addWidget(self.label_s_t)

        # Backwards-compat aliases. update_dashboard still writes to these
        # variable names; we forward them to the merged label or to no-ops.
        self.label_deltas = self.label_s_t
        self.label_height = self.label_scene_card  # old name → scene card
        self.label_thresholds = self.label_s_t   # rolled into deltas line
        self.label_gate = self.label_s_t          # rolled into deltas line
        self.label_unity = self.label_s_t          # rolled into deltas line

        # The big standalone state pill is gone. We keep `self.label_state`
        # as a hidden label so the rest of update_dashboard doesn't crash
        # when it sets its text. The visible state indicator is the
        # TextItem on the stress chart, refreshed in update_dashboard.
        self.label_state = QLabel("")
        self.label_state.setVisible(False)

        # ====================================================================
        # CHARTS -- one row, two columns. Side by side. Both maximised.
        # ====================================================================
        from PyQt5.QtWidgets import QGridLayout
        self.plot_stress = self._create_stress_plot()
        # Scene panel replaces the old Balloon Height chart. Adapts to
        # whatever scenario Unity is emitting (see _create_scene_panel).
        self.scene_widget = self._create_scene_panel()
        # plot_deltas is built but not displayed. update_dashboard still
        # references its curves; they buffer invisibly. Keeps the change
        # contained to layout only.
        self.plot_deltas = self._create_deltas_plot()

        # State-overlay TextItem on the stress chart -- shows the current
        # state ("CALM" / "STRESSED" / "ULTRA STRESSED") in the chart's
        # top-left corner. Updated in update_dashboard. Anchor 0,0 = top-left.
        self._state_overlay = pg.TextItem(
            text="CALM", color='#7bc89a', anchor=(0, 0),
        )
        font = QFont("Arial", 18, QFont.Bold)
        self._state_overlay.setFont(font)
        self.plot_stress.addItem(self._state_overlay)
        # Position the overlay in chart coordinates. We re-position it
        # each tick in update_dashboard because the X range slides.
        self._state_overlay.setPos(0, 0)

        chart_grid = QGridLayout()
        chart_grid.setSpacing(6)
        chart_grid.addWidget(self.plot_stress, 0, 0)
        chart_grid.addWidget(self.scene_widget, 0, 1)
        chart_grid.setColumnStretch(0, 1)
        chart_grid.setColumnStretch(1, 1)
        v.addLayout(chart_grid, 1)   # all remaining space goes to the charts

        return group

    def _build_qa_strip(self):
        """Bottom-of-screen data-quality counters + Unity link indicator."""
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

        # Unity telemetry link status pill. Painted red/gray until the
        # first packet arrives, then green while packets keep flowing,
        # amber when the receiver goes stale (>2 s without a packet).
        # Text updates every tick in _refresh_unity_link_pill().
        self.label_unity_link = QLabel("● UNITY  waiting…")
        self.label_unity_link.setFont(QFont("Arial", 10, QFont.Bold))
        self.label_unity_link.setStyleSheet(
            "color: #888888; padding: 3px 10px; "
            "background-color: #1c1c1c; border: 1px solid #333333; "
            "border-radius: 3px; margin-left: 10px;"
        )
        # Track packet flow so the pill can show a rolling packet-rate.
        self._unity_last_packet_wall = 0.0
        self._unity_packet_count = 0
        self._unity_recent_stamps = []  # sliding 2-second window
        gv.addWidget(self.label_unity_link)

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
        # Y-axis is THRESHOLD-LOCKED (not auto-ranged). Per operator request
        # 2026-06: the chart must clearly show the MILD/HIGH band for the
        # person, not auto-zoom out when a single outlier sample comes
        # through (which was making the curve invisible at scale 1e+27).
        # Default range below is a placeholder used only before the
        # baseline locks; once thresholds arrive, _lock_stress_y_to_thresholds
        # in update_dashboard sets the actual range as:
        #     y_min = -0.5 * (HIGH - MILD)         (a bit below the MILD band)
        #     y_max = HIGH + 0.5 * (HIGH - MILD)   (a bit above the ULTRA line)
        # Stress values above y_max are clipped visually but still counted
        # numerically by the state-classifier, so an off-scale ULTRA event
        # still triggers the correct UI state.
        plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        plot_widget.setYRange(-5.0, 8.0, padding=0)
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

    def _create_scene_panel(self):
        """
        Scenario-agnostic panel that replaces the old Balloon Height chart.

        Layout (top to bottom):
          1. Scenario label — "Scene: ACROPHOBIA" or "waiting for Unity"
          2. Key/value grid — every field of the current data payload
          3. Primary-field chart — the field named in
             Config.SCENARIO_PRIMARY_FIELD for the active scenario. When
             the scenario is unknown or has no primary field configured,
             the chart region shows a "no primary field" placeholder.

        All three elements are populated in update_dashboard from the
        Biofeedback_Telemetry LSL stream. The panel gracefully handles
        every state: no Unity connected, Unity connected but stale,
        unknown scenario, known scenario with new fields.
        """
        from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QGridLayout,
                                      QLabel, QSizePolicy)

        container = QWidget()
        container.setStyleSheet("background-color: #0a0a0a;")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        # --- Scenario label at top ---
        self.label_scenario = QLabel("Scene: (waiting for Unity)")
        self.label_scenario.setFont(QFont("Arial", 13, QFont.Bold))
        self.label_scenario.setStyleSheet("color: #d4b86a; padding: 2px;")
        self.label_scenario.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.label_scenario)

        # --- Key/value grid ---
        # QGridLayout inside its own widget so we can clear it wholesale
        # each tick without disturbing the outer layout.
        self.scene_kv_container = QWidget()
        self.scene_kv_container.setStyleSheet(
            "color: #dddddd; font-family: Consolas, monospace;"
        )
        self.scene_kv_container.setSizePolicy(QSizePolicy.Expanding,
                                                QSizePolicy.Minimum)
        self.scene_kv_layout = QGridLayout(self.scene_kv_container)
        self.scene_kv_layout.setContentsMargins(4, 2, 4, 2)
        self.scene_kv_layout.setSpacing(3)
        outer.addWidget(self.scene_kv_container)

        # --- Primary-field chart at the bottom ---
        self.plot_scene = pg.PlotWidget(
            title="",
            labels={'left': 'value', 'bottom': 'Time (samples)'}
        )
        self.plot_scene.showGrid(x=True, y=True, alpha=0.25)
        self.plot_scene.setMouseEnabled(x=True, y=False)
        self.plot_scene.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        self.plot_scene.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        self.plot_scene.setMenuEnabled(False)
        self.plot_scene.hideButtons()
        self.plot_scene.setStyleSheet("border: 1px solid #2c2c2c;")
        self.curve_scene = self.plot_scene.plot(
            [], [], pen=pg.mkPen('#d4b86a', width=2)
        )
        outer.addWidget(self.plot_scene, 1)  # chart takes remaining space

        return container

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
        # sample[24] is still `height_m` on the LSL stream for backward
        # compatibility with any external consumer of the primary stream,
        # but the dashboard no longer draws it here — the Scene panel
        # subscribes to the Biofeedback_Telemetry string stream and
        # renders whatever scenario Unity is emitting (see below).
        _ = float(sample[24])  # read to keep the tuple index shape stable

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
        # 4-6 decimal places on the live readouts so the operator can
        # spot small EDA values (1e-3..1e-4 uS) at a glance instead of
        # seeing them rounded to "0.00 uS" and assuming the sensor is dead.
        self.label_live_eda.setText(f"EDA: {_fmt_v(eda, 'uS', 4)}")
        self.label_live_hr.setText (f"HR: {_fmt_v(hr,  'BPM', 1)}")
        self.label_live_hrv.setText(f"HRV: {_fmt_v(hrv, 'ms', 1)}")
        self.label_live_phasic.setText(f"phEDA: {_fmt_v(delta_eda, 'uS', 4)}")
        # ECG numeric: instantaneous mV from the side stream. Use the last
        # value in the side-stream buffer if present (the chart is the
        # canonical view; this is a quick "is contact good?" check).
        if self.ecg_data['y']:
            self.label_live_ecg.setText(f"ECG: {self.ecg_data['y'][-1]:+.5f} mV")
        else:
            self.label_live_ecg.setText("ECG: -- mV")

        self.label_state.setText(state_label)
        # The big standalone state pill is gone. State is shown as a
        # TextItem overlay on the stress chart -- update the overlay's
        # text and colour each tick, and re-anchor it to the chart's
        # current top-left so it sticks to the visible viewport as the
        # X range slides.
        try:
            self._state_overlay.setText(state_label, color=state_color)
            # Re-anchor: top-left of the *visible* X range, top of Y range.
            vb = self.plot_stress.getViewBox()
            (x_min_view, x_max_view), (y_min_view, y_max_view) = vb.viewRange()
            self._state_overlay.setPos(x_min_view, y_max_view)
        except Exception:
            pass
        # Compact info cards (inline, single-line). Height card is
        # written below by the Unity telemetry block.
        self.label_s_t_card.setText(f"S_t {s_t:+.2f}")
        self.label_score_card.setText(f"Score {dashboard_score:.0f}/100")
        # Merged diagnostic line -- deltas + thresholds + Unity all in one
        # compact strip below the cards. Thresholds and Unity text are
        # filled in further below; we rebuild the whole line each tick.
        # Stored partial values are recombined further down.
        self._diag_deltas = (
            f"phEDA: {delta_eda:+.3f}uS   "
            f"dHR: {delta_hr:+.1f}%   dHRV: {delta_hrv:+.1f}%"
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

        # ---- EDA chart: full 0..25 µS PLUX sensor span, ALWAYS ----
        # Operator request 2026-06: keep the EDA Y axis pinned to the
        # full sensor range so saturation is visible and the scale is
        # the same across every participant. The auto-rescale that used
        # to narrow this around the visible mean is intentionally OFF
        # for EDA -- it only applies to HR / HRV below.
        if self.tick_counter % int(Config.PIPELINE_RATE) == 0:
            self.plot_eda.setYRange(*Config.EDA_PLOT_DEFAULT_RANGE, padding=0)
            self.plot_eda.locked_y_range = Config.EDA_PLOT_DEFAULT_RANGE

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

            # Dynamic auto-rescale for HR / HRV only (EDA stays at the
            # full 0..25 µS sensor span pinned earlier in this tick).
            # Keeps the HR / HRV curves centred on the live mean and
            # widens the half-range when the signal deviates. Updated
            # once per second so the chart doesn't visibly jitter.
            if self.tick_counter % int(Config.PIPELINE_RATE) == 0:
                self._autoscale_signal(self.plot_hr,  self.hr_data['y'],
                                       avg_hr,  Config.HR_PLOT_HALFRANGE,
                                       y_floor=0.0)
                self._autoscale_signal(self.plot_hrv, self.hrv_data['y'],
                                       avg_hrv, Config.HRV_PLOT_HALFRANGE,
                                       y_floor=0.0)

        # Threshold lines + baseline-panel labels still update normally;
        # the live-panel string is now a single merged line further down.
        if thresh_mild > 0.0 and self.mild_line.value() != thresh_mild:
            self.mild_line.setValue(thresh_mild)
        if thresh_high > 0.0 and self.high_line.value() != thresh_high:
            self.high_line.setValue(thresh_high)
        if thresh_mild > 0.0 and thresh_high > 0.0:
            t = f"Thresholds: MILD = {thresh_mild:.2f}, HIGH = {thresh_high:.2f}"
            self.label_baseline_sigma.setText(
                f"sigma_baseline: {thresh_mild / Config.THRESH_MILD_K:.3f}"
            )
            self.label_baseline_thresh.setText(t)
            thresh_str = f"MILD {thresh_mild:.2f} HIGH {thresh_high:.2f}"

            # ---- THRESHOLD-LOCKED Y-AXIS on the stress chart ----
            # Anchor the Y range to the person's own MILD/HIGH so the
            # bands are visible, but give GENEROUS headroom in both
            # directions so deep-negative S_t (very calm) and far-above-
            # HIGH S_t (very stressed) are still visible. Operator
            # request 2026-06: previous tight band hid both tails.
            band = max(1e-3, thresh_high - thresh_mild)
            y_min = -3.0 * band                          # well below CALM zero
            y_max = thresh_high + 3.0 * band             # well above HIGH
            cache = getattr(self, '_stress_y_locked_at', None)
            if cache != (thresh_mild, thresh_high):
                self.stress_plot.setYRange(y_min, y_max, padding=0)
                self._stress_y_locked_at = (thresh_mild, thresh_high)
        else:
            thresh_str = "MILD -- HIGH --"

        # UDP gate status as a compact tag.
        if session_state == SessionState.LIVE and not udp_gate_open:
            gate_str = "GATE:WAIT"
        elif session_state == SessionState.LIVE and udp_gate_open:
            gate_str = "GATE:OPEN"
        else:
            gate_str = ""

        # Unity last-command compact tag.
        _CODE_TO_NAME = {0: "idle", 1: "UP", 2: "DOWN", 3: "START", 4: "STOP"}
        cmd_name = _CODE_TO_NAME.get(unity_cmd_code, f"?{unity_cmd_code}")
        if session_state in (SessionState.LIVE, SessionState.STOPPED):
            unity_str = f"Unity:{cmd_name} ({unity_cmd_total} sent)"
        else:
            unity_str = ""

        # Merged single-line diagnostic strip (replaces 4 separate labels).
        # Uses the deltas computed earlier (self._diag_deltas) plus the
        # compact threshold / gate / Unity tags built just above.
        diag_bits = [getattr(self, '_diag_deltas', '')]
        if thresh_str:
            diag_bits.append(thresh_str)
        if gate_str:
            diag_bits.append(gate_str)
        if unity_str:
            diag_bits.append(unity_str)
        self.label_s_t.setText("   |   ".join(b for b in diag_bits if b))

        # ---- Scene panel telemetry (from Biofeedback_Telemetry) ----
        # Pull the newest packet, parse JSON, refresh the scenario label,
        # the key/value grid, and the primary-field chart. All work is
        # scenario-agnostic: whatever fields Unity sends are rendered
        # verbatim. Lazy-attach the inlet the first few seconds until
        # main.py's outlet resolves.
        self._try_attach_telemetry(initial=False)
        if self.telemetry_inlet is not None:
            self._pump_telemetry_and_render()
        else:
            # Inlet not up yet: still refresh the pill so the operator
            # sees "waiting…" instead of a frozen state.
            self._refresh_unity_link_pill()

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
        # Single-line compact card format.
        self.label_time_calm.setText(f"CALM {_fmt(self.ticks_calm)}")
        self.label_time_stress.setText(f"STRESSED {_fmt(self.ticks_stressed)}")
        self.label_time_ultra.setText(f"ULTRA {_fmt(self.ticks_ultra)}")

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

    def _pump_telemetry_and_render(self):
        """
        Pull every buffered sample from the telemetry inlet, keep the
        most recent JSON envelope, update the scene panel widgets, and
        append the primary field's value to the scene chart.

        Called from update_dashboard once the inlet is attached. Cheap
        — one pull_chunk per timer fire plus a handful of Qt widget
        updates. String stream so we get list-of-strings back.
        """
        import json
        import time as _time
        try:
            chunk, _ = self.telemetry_inlet.pull_chunk(timeout=0.0)
        except Exception:
            return
        # Refresh the Unity link pill on EVERY tick so "stale" can be
        # detected even when no chunk arrives this fire.
        self._refresh_unity_link_pill()
        if not chunk:
            # No new telemetry this tick. Do NOT clear the panel — hold
            # the last-known scenario/values so brief network jitter
            # doesn't blink the UI.
            return

        # Take the newest packet only; older ones in the chunk are stale.
        # Also count every non-empty payload seen so the Unity link pill
        # can display a rolling packet-rate.
        newest = None
        now = _time.time()
        for row in chunk:
            payload = (row[0] if isinstance(row, (list, tuple)) and row
                       else row)
            if payload:
                newest = payload
                self._unity_packet_count += 1
                self._unity_recent_stamps.append(now)
                self._unity_last_packet_wall = now
        # Prune the 2-second sliding window used for the pill's rate readout.
        cutoff = now - 2.0
        while (self._unity_recent_stamps
               and self._unity_recent_stamps[0] < cutoff):
            self._unity_recent_stamps.pop(0)

        if newest is None:
            # Every payload in the chunk was empty ("no telemetry" from
            # main.py). Reset the panel to its waiting state.
            if self.latest_scenario:
                self.latest_scenario = ""
                self.latest_scene_data = {}
                self._refresh_scene_panel()
            return

        try:
            envelope = json.loads(newest)
        except (ValueError, TypeError):
            return
        scenario = envelope.get(Config.TELEMETRY_SCENARIO_KEY, "")
        data = envelope.get(Config.TELEMETRY_DATA_KEY, {})
        if not isinstance(scenario, str) or not isinstance(data, dict):
            return

        self.latest_scenario = scenario
        self.latest_scene_data = data
        self._refresh_scene_panel()

        # Append the primary field's value to the chart if configured.
        primary_key = Config.SCENARIO_PRIMARY_FIELD.get(scenario.lower())
        if primary_key and primary_key in data:
            try:
                value = float(data[primary_key])
            except (TypeError, ValueError):
                value = None
            if value is not None:
                self.scene_chart_data['x'].append(self.tick_counter)
                self.scene_chart_data['y'].append(value)
                if len(self.scene_chart_data['x']) > self.max_history:
                    self.scene_chart_data['x'].pop(0)
                    self.scene_chart_data['y'].pop(0)
                self.curve_scene.setData(self.scene_chart_data['x'],
                                          self.scene_chart_data['y'])

        self.plot_scene.setXRange(
            max(0, self.tick_counter - self.view_width),
            self.tick_counter,
        )

    def _refresh_unity_link_pill(self):
        """
        Repaint the Unity link pill so the operator always knows whether
        biofeedback is receiving telemetry from Unity. Three states:

          gray   — no packet has ever arrived (waiting).
          green  — packets are flowing (last <2 s + rate > 0).
          amber  — a packet HAS arrived at some point, but the link has
                   gone silent for >2 s (Unity paused, VR cable pulled,
                   scene not in Play mode).

        Format:
          "● UNITY  waiting…"                             (never seen)
          "● UNITY  acrophobia • 10.2 Hz • 128 pkts"      (live)
          "● UNITY  acrophobia • STALE 4.2s • 128 pkts"   (dropped)
        """
        import time as _time
        now = _time.time()

        if self._unity_last_packet_wall == 0.0:
            color = "#888888"       # gray dot, gray text
            text = "● UNITY  waiting…"
        else:
            age = now - self._unity_last_packet_wall
            rate = len(self._unity_recent_stamps) / 2.0
            scen = (self.latest_scenario or "unknown").upper()
            total = self._unity_packet_count
            if age <= 2.0:
                color = "#7bc89a"    # green: healthy link
                text = (f"● UNITY  {scen} • {rate:.1f} Hz "
                        f"• {total} pkts")
            else:
                color = "#d4b86a"    # amber: went silent
                text = (f"● UNITY  {scen} • STALE {age:.1f}s "
                        f"• {total} pkts")

        self.label_unity_link.setText(text)
        self.label_unity_link.setStyleSheet(
            f"color: {color}; padding: 3px 10px; "
            f"background-color: #1c1c1c; border: 1px solid {color}; "
            f"border-radius: 3px; margin-left: 10px;"
        )

    def _refresh_scene_panel(self):
        """
        Redraw the scenario label, key/value grid, chart title, and top-
        bar card from `self.latest_scenario` + `self.latest_scene_data`.
        Idempotent — safe to call repeatedly.
        """
        # ---- Scenario label ----
        if self.latest_scenario:
            self.label_scenario.setText(
                f"Scene: {self.latest_scenario.upper()}"
            )
        else:
            self.label_scenario.setText("Scene: (waiting for Unity)")

        # ---- Key/value grid ----
        # Clear existing rows first — QGridLayout has no clear() so we
        # remove children by hand. Cheap because rows are ~1-5 items.
        while self.scene_kv_layout.count():
            item = self.scene_kv_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for row, (key, val) in enumerate(self.latest_scene_data.items()):
            k_lab = QLabel(f"{key}:")
            k_lab.setStyleSheet("color: #a0c4d9;")
            v_lab = QLabel(_format_scene_value(val))
            v_lab.setStyleSheet("color: #eeeeee; font-weight: bold;")
            self.scene_kv_layout.addWidget(k_lab, row, 0)
            self.scene_kv_layout.addWidget(v_lab, row, 1)

        # ---- Chart title (matches the primary field for the scenario) ----
        primary_key = Config.SCENARIO_PRIMARY_FIELD.get(
            self.latest_scenario.lower()
        )
        if primary_key:
            unit = Config.SCENARIO_PRIMARY_UNIT.get(
                self.latest_scenario.lower(), ""
            )
            title_bits = [primary_key]
            if unit:
                title_bits.append(f"({unit})")
            self.plot_scene.setTitle(" ".join(title_bits),
                                       color='#dddddd', size='10pt')
        else:
            self.plot_scene.setTitle("no primary field configured",
                                       color='#888888', size='9pt')

        # ---- Top-bar Scene card ----
        # Show "<primary_field> <value> <unit>" for known scenarios,
        # otherwise a generic scenario label.
        if not self.latest_scenario:
            self.label_scene_card.setText("Scene --")
        elif primary_key and primary_key in self.latest_scene_data:
            unit = Config.SCENARIO_PRIMARY_UNIT.get(
                self.latest_scenario.lower(), ""
            )
            val = _format_scene_value(self.latest_scene_data[primary_key])
            self.label_scene_card.setText(
                f"{primary_key} {val} {unit}".strip()
            )
        else:
            self.label_scene_card.setText(
                f"Scene {self.latest_scenario}"
            )

    def _try_attach_telemetry(self, initial: bool = False):
        """Best-effort resolve the Biofeedback_Telemetry side stream.
        Same lazy-attach pattern as ECG — main.py publishes it at startup
        but resolve_byprop can miss it during the ~1 s race between
        outlet open and the dashboard's first tick."""
        if self.telemetry_inlet is not None:
            return
        import time as _time
        now = _time.time()
        if (not initial and
                (now - self._telemetry_last_retry_time) < self._telemetry_retry_interval_sec):
            return
        self._telemetry_last_retry_time = now
        try:
            streams = resolve_byprop("name",
                                      Config.TELEMETRY_LSL_STREAM_NAME,
                                      timeout=0.2)
            if streams:
                self.telemetry_inlet = StreamInlet(streams[0])
                print(f"[DASHBOARD] Connected to telemetry stream "
                      f"'{Config.TELEMETRY_LSL_STREAM_NAME}'."
                      + ("" if initial else " (lazy retry succeeded)"))
            elif initial:
                print(f"[DASHBOARD] Telemetry stream not yet present; "
                      f"will retry every "
                      f"{self._telemetry_retry_interval_sec:.0f}s "
                      f"in the update loop.")
        except Exception as e:
            if initial:
                print(f"[DASHBOARD] Telemetry stream lookup failed ({e}); "
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
