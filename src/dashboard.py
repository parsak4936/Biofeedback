# src/dashboard.py
"""
Clinical Biofeedback Dashboard

Displays:
- Patient ID and session info
- Live stress index chart (scrollable history)
- Individual signal charts (EDA, HR, HRV)
- Phase indicator and baseline values
- Session statistics and stress state details
"""

import sys
import pyqtgraph as pg
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QGridLayout, QGroupBox)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QFont, QColor
from pylsl import resolve_stream, StreamInlet
from config import Config


class ClinicalDashboard:
    def __init__(self):
        # 1. Connect to the Pipeline's Output Stream
        print("[DASHBOARD] Searching for Biofeedback_State stream...")
        try:
            streams = resolve_stream("name", Config.OUT_STREAM_NAME)
            self.inlet = StreamInlet(streams[0])
            print("[DASHBOARD] Connected to output stream.")
        except Exception as e:
            print(f"[ERROR] Could not find stream '{Config.OUT_STREAM_NAME}'")
            print(f"Make sure main.py is running.")
            raise RuntimeError(f"LSL stream not found: {str(e)}")

        # 1b. Connect to optional ECG side stream (best-effort, non-blocking).
        # If the data source isn't publishing ECG (e.g. real PLUX mode without
        # an upstream ECG publisher), the chart just stays empty.
        self.ecg_inlet = None
        try:
            from pylsl import resolve_byprop
            ecg_streams = resolve_byprop("name", Config.ECG_STREAM_NAME, timeout=2.0)
            if ecg_streams:
                self.ecg_inlet = StreamInlet(ecg_streams[0])
                print(f"[DASHBOARD] Connected to ECG side stream "
                      f"'{Config.ECG_STREAM_NAME}'.")
            else:
                print(f"[DASHBOARD] ECG side stream not present; chart will stay empty.")
        except Exception as e:
            print(f"[DASHBOARD] ECG side stream lookup failed ({e}); skipping.")
        
        # 2. Setup the main PyQt5 application and window
        self.app = QApplication.instance()
        if self.app is None:
            self.app = QApplication(sys.argv)
        
        self.win = QWidget()
        self.win.setWindowTitle("Clinical Biofeedback Dashboard - Patient Acrophobia Therapy")
        self.win.resize(1600, 900)
        self.win.setStyleSheet("background-color: #1a1a1a; color: #ffffff;")
        
        # 3. Setup layout structure
        main_layout = QVBoxLayout()
        
        # --- TOP: Session Info Panel ---
        self.info_layout = self._create_info_panel()
        main_layout.addLayout(self.info_layout)
        
        # --- MIDDLE: Charts (Stress + Signals) ---
        charts_layout = QHBoxLayout()
        
        # Left: Stress Index Chart
        self.plot_stress = self._create_stress_plot()
        charts_layout.addWidget(self.plot_stress, 2)  # 2x wider
        
        # Right: Signal Charts (stacked) — fixed Y bounds prevent the
        # "x0.001" auto-zoom artifact when smoothed signals are nearly constant.
        right_layout = QVBoxLayout()
        self.plot_eda = self._create_signal_plot("EDA (μS)", "#00ff00",
                                                  y_range=Config.EDA_PLOT_DEFAULT_RANGE)
        self.plot_hr = self._create_signal_plot("HR (BPM)", "#ff6600",
                                                 y_range=Config.HR_PLOT_DEFAULT_RANGE)
        self.plot_hrv = self._create_signal_plot("HRV (ms)", "#0099ff",
                                                  y_range=Config.HRV_PLOT_DEFAULT_RANGE)
        # ECG waveform — same chart style, wider X window (drawn at native rate).
        self.plot_ecg = self._create_signal_plot("ECG (mV)", "#ff00ff",
                                                  y_range=(-1.5, 1.5))
        # Track whether we've already recentered the charts around the locked baseline.
        self._signal_ranges_centered = False

        right_layout.addWidget(self.plot_eda)
        right_layout.addWidget(self.plot_hr)
        right_layout.addWidget(self.plot_hrv)
        right_layout.addWidget(self.plot_ecg)
        
        right_widget = QWidget()
        right_widget.setLayout(right_layout)
        charts_layout.addWidget(right_widget, 1)
        
        main_layout.addLayout(charts_layout, 4)  # Charts get 4x height
        
        # --- BOTTOM: Metrics Panel ---
        self.metrics_layout = self._create_metrics_panel()
        main_layout.addLayout(self.metrics_layout, 1)
        
        self.win.setLayout(main_layout)
        
        # 4. Data buffers for charting
        self.stress_data = {'x': [], 'y': []}
        self.eda_data = {'x': [], 'y': []}
        self.hr_data = {'x': [], 'y': []}
        self.hrv_data = {'x': [], 'y': []}
        # ECG buffer: filled at the side-stream's native rate (200 or 1000 Hz),
        # so it scrolls in raw-sample units, not pipeline-ticks.
        self.ecg_buffer = []
        self.ecg_sample_index = 0
        self.tick_counter = 0
        self.max_history = Config.DASHBOARD_MAX_HISTORY
        self.view_width = Config.DASHBOARD_VIEW_WIDTH
        
        # 5. Start the update timer at 50Hz
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_dashboard)
        self.timer.start(20)  # 50Hz
    
    def _create_info_panel(self):
        """Top panel: Session info (patient ID, phase, time, status)."""
        layout = QHBoxLayout()
        
        # Get patient info from environment
        import os
        patient_name = os.environ.get('PATIENT_NAME', 'PATIENT')
        patient_id = os.environ.get('PATIENT_ID', '000')
        
        # Left: Patient ID and Phase
        self.label_patient = QLabel(f"Patient: {patient_name} ({patient_id})")
        self.label_patient.setFont(QFont("Arial", 14, QFont.Bold))
        self.label_patient.setStyleSheet("color: #0099ff;")

        # Mode label — reads Config.SESSION_MODE so dashboard stays in sync with
        # what fusion.py picked. Future Unity handshake would push a new value
        # over a dedicated LSL channel; for now Config is the source of truth.
        self.label_mode = QLabel(f"Session: {Config.SESSION_MODE.upper()}")
        self.label_mode.setFont(QFont("Arial", 12, QFont.Bold))
        self.label_mode.setStyleSheet("color: #ff9900;")

        self.label_phase = QLabel("Phase: BASELINE")
        self.label_phase.setFont(QFont("Arial", 12))
        self.label_phase.setStyleSheet("color: #ffff00;")

        # Center: Duration
        self.label_duration = QLabel("Duration: 00:00")
        self.label_duration.setFont(QFont("Arial", 12))

        # Right: Status indicator with color
        self.label_status = QLabel("● Ready")
        self.label_status.setFont(QFont("Arial", 12))
        self.label_status.setStyleSheet("color: #00ff00;")  # Green = Ready

        layout.addWidget(self.label_patient, 2)
        layout.addWidget(self.label_mode, 1)
        layout.addWidget(self.label_phase, 1)
        layout.addWidget(self.label_duration, 1)
        layout.addWidget(self.label_status, 1)

        return layout
    
    def _create_stress_plot(self):
        """Main stress index chart with threshold indicators."""
        plot_widget = pg.PlotWidget(
            title="Real-Time Stress Index (S_t) - Scrollable",
            labels={'left': 'Stress Level', 'bottom': 'Time (samples)'}
        )
        # S_t can be negative (true relaxation) or sit slightly above thresh_high.
        # Let Y auto-scale to whatever the data shows, but keep the X axis fixed
        # so the auto-scroll feels stable.
        plot_widget.showGrid(x=True, y=True)
        plot_widget.setMouseEnabled(x=True, y=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        plot_widget.setMenuEnabled(False)
        plot_widget.hideButtons()
        plot_widget.setStyleSheet("border: 1px solid #333;")
        
        # Threshold lines — labels embed the numeric value directly on the line
        # so the operator can read "MILD: 15.27" / "HIGH: 26.19" without
        # cross-referencing another panel. Positions itself near the right edge.
        self.mild_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen('#ffff00', style=pg.QtCore.Qt.DashLine, width=2),
            label='MILD = {value:0.2f}',
            labelOpts={'color': '#ffff00', 'position': 0.92,
                       'movable': False, 'fill': (0, 0, 0, 160)},
        )
        self.high_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen('#ff0000', style=pg.QtCore.Qt.DashLine, width=2),
            label='HIGH = {value:0.2f}',
            labelOpts={'color': '#ff0000', 'position': 0.92,
                       'movable': False, 'fill': (0, 0, 0, 160)},
        )
        plot_widget.addItem(self.mild_line)
        plot_widget.addItem(self.high_line)
        
        # Stress curve
        self.curve_stress = plot_widget.plot([], [], pen=pg.mkPen('#ffffff', width=2))
        
        # Color background based on state
        self.stress_plot = plot_widget
        self.color_calm = pg.mkColor(20, 50, 20)
        self.color_stressed = pg.mkColor(50, 50, 20)
        self.color_ultra = pg.mkColor(50, 20, 20)
        
        return plot_widget
    
    def _create_signal_plot(self, title: str, color: str, y_range=None):
        """Create individual signal chart (EDA, HR, HRV) with a fixed Y range
        so flat resting data doesn't get auto-zoomed to floating-point noise."""
        plot_widget = pg.PlotWidget(
            title=title,
            labels={'left': title, 'bottom': 'Time (samples)'}
        )
        plot_widget.showGrid(x=True, y=True)
        plot_widget.setMouseEnabled(x=True, y=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        # Hide the pyqtgraph default context menu + "A" auto-scale button so the
        # operator can't accidentally re-enable autorange mid-session.
        plot_widget.setMenuEnabled(False)
        plot_widget.hideButtons()
        if y_range is not None:
            plot_widget.setYRange(*y_range, padding=0)
        plot_widget.setStyleSheet("border: 1px solid #333;")

        curve = plot_widget.plot([], [], pen=pg.mkPen(color, width=1.5))

        # Store curve reference + its desired Y range for re-locking each tick.
        plot_widget.curve = curve
        plot_widget.title_text = title
        plot_widget.locked_y_range = y_range

        return plot_widget
    
    def _create_metrics_panel(self):
        """Bottom panel: Baseline values, statistics, current stress state."""
        layout = QHBoxLayout()
        
        # Baseline Values (left)
        baseline_group = QGroupBox("Personal Baseline")
        baseline_group.setStyleSheet("QGroupBox { color: #0099ff; font-weight: bold; }")
        baseline_layout = QVBoxLayout()
        
        self.label_baseline_eda = QLabel("EDA: -- μS")
        self.label_baseline_hr = QLabel("HR: -- BPM")
        self.label_baseline_hrv = QLabel("HRV: -- ms")
        
        for label in [self.label_baseline_eda, self.label_baseline_hr, self.label_baseline_hrv]:
            label.setFont(QFont("Arial", 10))
            baseline_layout.addWidget(label)
        
        baseline_group.setLayout(baseline_layout)
        layout.addWidget(baseline_group)
        
        # Current Stress State (center)
        state_group = QGroupBox("Current Stress State")
        state_group.setStyleSheet("QGroupBox { color: #ffff00; font-weight: bold; }")
        state_layout = QVBoxLayout()
        
        self.label_state = QLabel("calm")
        self.label_state.setFont(QFont("Arial", 14, QFont.Bold))
        self.label_state.setAlignment(Qt.AlignCenter)
        
        self.label_s_t = QLabel("S_t: -- ")
        self.label_s_t.setFont(QFont("Arial", 10))

        self.label_thresholds = QLabel("Thresholds: MILD = --, HIGH = --")
        self.label_thresholds.setFont(QFont("Arial", 10))
        self.label_thresholds.setStyleSheet("color: #cccccc;")

        state_layout.addWidget(self.label_state)
        state_layout.addWidget(self.label_s_t)
        state_layout.addWidget(self.label_thresholds)
        state_group.setLayout(state_layout)
        layout.addWidget(state_group)
        
        # Statistics (right)
        stats_group = QGroupBox("Session Statistics")
        stats_group.setStyleSheet("QGroupBox { color: #ff6600; font-weight: bold; }")
        stats_layout = QVBoxLayout()

        self.label_samples = QLabel("Samples: 0")
        self.label_time_calm = QLabel("Time CALM: 00:00")
        self.label_time_stress = QLabel("Time STRESSED: 00:00")
        self.label_time_ultra = QLabel("Time ULTRA: 00:00")

        # Data-quality row — these are diagnostic counters from acquisition.
        # They stay at zero on a clean session; any nonzero value tells the
        # operator something to investigate (electrode contact, noise, etc).
        self.label_qa_header = QLabel("─── Data Quality ───")
        self.label_qa_header.setStyleSheet("color: #888888;")
        self.label_qa_invalid = QLabel("Invalid samples: 0")
        self.label_qa_oor = QLabel("Out-of-range:    0")
        self.label_qa_disc = QLabel("Disconnects:     0")

        for label in [self.label_samples, self.label_time_calm,
                      self.label_time_stress, self.label_time_ultra,
                      self.label_qa_header,
                      self.label_qa_invalid, self.label_qa_oor, self.label_qa_disc]:
            label.setFont(QFont("Arial", 10))
            stats_layout.addWidget(label)

        stats_group.setLayout(stats_layout)
        layout.addWidget(stats_group)

        # Tick counters per state (incremented at 50Hz in update_dashboard).
        self.ticks_calm = 0
        self.ticks_stressed = 0
        self.ticks_ultra = 0
        
        return layout
    
    def update_dashboard(self):
        """Called at 50Hz to fetch and display latest data."""
        sample, timestamp = self.inlet.pull_sample(timeout=0.0)

        if not sample or len(sample) < 18:
            return

        # 18-channel layout: see UnityBridge.CHANNELS in output.py
        s_t = sample[0]
        state_enum = sample[1]
        dashboard_score = sample[2]
        y_t = sample[3]
        eda = sample[4]
        hr = sample[5]
        hrv = sample[6]
        avg_eda = sample[7]
        avg_hr = sample[8]
        avg_hrv = sample[9]
        thresh_mild = sample[10]
        thresh_high = sample[11]
        # baseline_status, elapsed_baseline_sec, mode_enum (12,13,14) — Unity bound.
        _baseline_status = sample[12]
        _elapsed_baseline_sec = sample[13]
        _mode_enum = sample[14]
        # QA counters (15,16,17) — drive the data-quality panel below.
        qa_invalid = int(sample[15])
        qa_out_of_range = int(sample[16])
        qa_disconnects = int(sample[17])

        # ============================================
        # DETERMINE STATE & COLORS
        # ============================================
        if state_enum == 0.0:
            state_label = "CALM"
            state_color = "#00ff00"  # Bright green
            bg_color = self.color_calm
            status_icon = "● "
        elif state_enum == 1.0:
            state_label = "STRESSED"
            state_color = "#ffff00"  # Bright yellow
            bg_color = self.color_stressed
            status_icon = "⚠ "
        else:  # 2.0
            state_label = "ULTRA STRESSED"
            state_color = "#ff0000"  # Bright red
            bg_color = self.color_ultra
            status_icon = "🔴 "

        # ============================================
        # UPDATE STRESS DATA & CHART
        # ============================================
        # Per walkthrough Step 2, "no stress visualization runs yet" during
        # baseline. Once thresholds lock (thresh_mild > 0), start drawing.
        if thresh_mild > 0.0:
            self.stress_data['x'].append(self.tick_counter)
            self.stress_data['y'].append(s_t)

            if len(self.stress_data['x']) > self.max_history:
                self.stress_data['x'].pop(0)
                self.stress_data['y'].pop(0)

            self.curve_stress.setData(self.stress_data['x'], self.stress_data['y'])

        # Auto-scroll to follow data
        self.stress_plot.setXRange(
            max(0, self.tick_counter - self.view_width),
            self.tick_counter
        )

        # Background color by state
        self.stress_plot.getViewBox().setBackgroundColor(bg_color)

        # ============================================
        # UPDATE RAW SIGNAL CHARTS (EDA / HR / HRV)
        # ============================================
        for buf, value, plot in (
            (self.eda_data, eda, self.plot_eda),
            (self.hr_data, hr, self.plot_hr),
            (self.hrv_data, hrv, self.plot_hrv),
        ):
            buf['x'].append(self.tick_counter)
            buf['y'].append(value)
            if len(buf['x']) > self.max_history:
                buf['x'].pop(0)
                buf['y'].pop(0)
            plot.curve.setData(buf['x'], buf['y'])
            plot.setXRange(max(0, self.tick_counter - self.view_width), self.tick_counter)
            # Re-lock the Y range every tick so nothing (mouse zoom, autorange
            # button, internal pyqtgraph state changes) can collapse the axis.
            if plot.locked_y_range is not None:
                plot.setYRange(*plot.locked_y_range, padding=0)

        # ============================================
        # UPDATE ECG WAVEFORM (from side stream)
        # ============================================
        if self.ecg_inlet is not None:
            ecg_samples, _ = self.ecg_inlet.pull_chunk(timeout=0.0)
            if ecg_samples:
                # Each entry is [mv]; flatten and append.
                for s in ecg_samples:
                    self.ecg_buffer.append(float(s[0]))
                    self.ecg_sample_index += 1
                # Trim to history depth
                if len(self.ecg_buffer) > Config.ECG_PLOT_MAX_HISTORY:
                    excess = len(self.ecg_buffer) - Config.ECG_PLOT_MAX_HISTORY
                    self.ecg_buffer = self.ecg_buffer[excess:]
                # X-axis = absolute sample index, so the plot scrolls smoothly.
                start_idx = self.ecg_sample_index - len(self.ecg_buffer)
                xs = list(range(start_idx, self.ecg_sample_index))
                self.plot_ecg.curve.setData(xs, self.ecg_buffer)
                self.plot_ecg.setXRange(
                    max(0, self.ecg_sample_index - Config.ECG_PLOT_MAX_HISTORY),
                    self.ecg_sample_index
                )
                if self.plot_ecg.locked_y_range is not None:
                    self.plot_ecg.setYRange(*self.plot_ecg.locked_y_range, padding=0)

        # ============================================
        # UPDATE BOTTOM PANEL
        # ============================================
        self.label_state.setText(state_label)
        self.label_state.setStyleSheet(f"color: {state_color}; font-weight: bold;")
        self.label_s_t.setText(f"S_t: {s_t:.2f} | Score: {dashboard_score:.0f}/100 | y_t: {y_t:.2f}m")

        # Personal Baseline panel — populated once calibration emits non-zero averages
        if avg_eda > 0.0 and avg_hr > 0.0:
            self.label_baseline_eda.setText(f"EDA: {avg_eda:.2f} μS")
            self.label_baseline_hr.setText(f"HR: {avg_hr:.2f} BPM")
            self.label_baseline_hrv.setText(f"HRV: {avg_hrv:.2f} ms")

            # Recenter the per-signal charts around the locked baseline so the
            # Y-axis covers a clinically useful range, not floating-point noise.
            # We update `locked_y_range` so the per-tick re-lock keeps these new
            # bounds instead of snapping back to the pre-baseline defaults.
            if not self._signal_ranges_centered:
                eda_range = (max(0.0, avg_eda - Config.EDA_PLOT_HALFRANGE),
                             avg_eda + Config.EDA_PLOT_HALFRANGE)
                hr_range = (max(0.0, avg_hr - Config.HR_PLOT_HALFRANGE),
                            avg_hr + Config.HR_PLOT_HALFRANGE)
                hrv_range = (max(0.0, avg_hrv - Config.HRV_PLOT_HALFRANGE),
                             avg_hrv + Config.HRV_PLOT_HALFRANGE)
                self.plot_eda.locked_y_range = eda_range
                self.plot_hr.locked_y_range = hr_range
                self.plot_hrv.locked_y_range = hrv_range
                self.plot_eda.setYRange(*eda_range, padding=0)
                self.plot_hr.setYRange(*hr_range, padding=0)
                self.plot_hrv.setYRange(*hrv_range, padding=0)
                self._signal_ranges_centered = True

        # Threshold lines on the stress chart (drawn once they're set).
        # Per math-pipeline Step 8 these are constants once the baseline locks,
        # so we set them on first arrival and never touch them again.
        if thresh_mild > 0.0 and self.mild_line.value() != thresh_mild:
            self.mild_line.setValue(thresh_mild)
        if thresh_high > 0.0 and self.high_line.value() != thresh_high:
            self.high_line.setValue(thresh_high)
        if thresh_mild > 0.0 and thresh_high > 0.0:
            self.label_thresholds.setText(
                f"Thresholds: MILD = {thresh_mild:.2f}, HIGH = {thresh_high:.2f}"
            )

        # Session Statistics
        # Only accumulate per-state time once thresholds are locked (LIVE phase);
        # before that, every tick reports "calm" with state_enum=0 by default and
        # would inflate the CALM counter during baseline.
        if thresh_mild > 0.0:
            if state_enum == 0.0:
                self.ticks_calm += 1
            elif state_enum == 1.0:
                self.ticks_stressed += 1
            else:
                self.ticks_ultra += 1

        def _fmt(ticks):
            secs = ticks // int(Config.PIPELINE_RATE)
            return f"{secs // 60:02d}:{secs % 60:02d}"

        self.label_samples.setText(f"Samples: {self.tick_counter}")
        self.label_time_calm.setText(f"Time CALM: {_fmt(self.ticks_calm)}")
        self.label_time_stress.setText(f"Time STRESSED: {_fmt(self.ticks_stressed)}")
        self.label_time_ultra.setText(f"Time ULTRA: {_fmt(self.ticks_ultra)}")

        # Data-quality counters — nonzero values are color-flagged so the
        # operator notices mid-session without scanning the terminal.
        def _qa_color(n):
            return "color: #ff6666;" if n > 0 else "color: #888888;"

        self.label_qa_invalid.setText(f"Invalid samples: {qa_invalid}")
        self.label_qa_invalid.setStyleSheet(_qa_color(qa_invalid))
        self.label_qa_oor.setText(f"Out-of-range:    {qa_out_of_range}")
        self.label_qa_oor.setStyleSheet(_qa_color(qa_out_of_range))
        self.label_qa_disc.setText(f"Disconnects:     {qa_disconnects}")
        self.label_qa_disc.setStyleSheet(_qa_color(qa_disconnects))

        # ============================================
        # UPDATE TOP PANEL
        # ============================================
        # Status indicator
        self.label_status.setText(f"{status_icon}{state_label}")
        self.label_status.setStyleSheet(f"color: {state_color};")

        # Update duration (approximate - every 50 ticks is 1 second)
        if self.tick_counter % 50 == 0:
            total_seconds = self.tick_counter // 50
            minutes = total_seconds // 60
            disp_seconds = total_seconds % 60
            self.label_duration.setText(f"Duration: {minutes:02d}:{disp_seconds:02d}")

            # Phase indicator driven by total elapsed seconds, not the wrapped value
            if total_seconds < 120:
                phase_text = f"Phase: BASELINE ({120 - total_seconds}s remaining)"
                phase_color = "#ffff00"  # Yellow during baseline
            else:
                phase_text = "Phase: LIVE"
                phase_color = "#00ff00"  # Green when live

            self.label_phase.setText(phase_text)
            self.label_phase.setStyleSheet(f"color: {phase_color};")

        self.tick_counter += 1
    
    def run(self):
        """Start the dashboard window."""
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