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
        
        # Right: Signal Charts (stacked)
        right_layout = QVBoxLayout()
        self.plot_eda = self._create_signal_plot("EDA (μS)", "#00ff00")
        self.plot_hr = self._create_signal_plot("HR (BPM)", "#ff6600")
        self.plot_hrv = self._create_signal_plot("HRV (ms)", "#0099ff")
        
        right_layout.addWidget(self.plot_eda)
        right_layout.addWidget(self.plot_hr)
        right_layout.addWidget(self.plot_hrv)
        
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
        plot_widget.setStyleSheet("border: 1px solid #333;")
        
        # Threshold lines (will be updated when set)
        self.mild_line = pg.InfiniteLine(angle=0, pen=pg.mkPen('#ffff00', style=pg.QtCore.Qt.DashLine, width=2))
        self.high_line = pg.InfiniteLine(angle=0, pen=pg.mkPen('#ff0000', style=pg.QtCore.Qt.DashLine, width=2))
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
    
    def _create_signal_plot(self, title: str, color: str):
        """Create individual signal chart (EDA, HR, HRV)."""
        plot_widget = pg.PlotWidget(
            title=title,
            labels={'left': title, 'bottom': 'Time (samples)'}
        )
        plot_widget.showGrid(x=True, y=True)
        plot_widget.setMouseEnabled(x=True, y=False)
        plot_widget.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        plot_widget.setStyleSheet("border: 1px solid #333;")
        
        curve = plot_widget.plot([], [], pen=pg.mkPen(color, width=1.5))
        
        # Store curve reference for updating
        plot_widget.curve = curve
        plot_widget.title_text = title
        
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
        
        state_layout.addWidget(self.label_state)
        state_layout.addWidget(self.label_s_t)
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

        for label in [self.label_samples, self.label_time_calm,
                      self.label_time_stress, self.label_time_ultra]:
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

        if not sample or len(sample) < 12:
            return

        # 12-channel layout: see UnityBridge.CHANNELS
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

        # Threshold lines on the stress chart (drawn once they're set)
        if thresh_mild > 0.0:
            self.mild_line.setValue(thresh_mild)
        if thresh_high > 0.0:
            self.high_line.setValue(thresh_high)

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