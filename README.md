# Biofeedback Virtual Clinic - Complete Documentation

## 📋 Quick Start for Casual Users

```powershell
python launcher.py
```

That's it! You'll be prompted to enter the patient name/ID, then the system starts automatically.

---

## 🏗️ System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        LAUNCHER.PY                              │
│                (Single Entry Point)                             │
│        ├─ Asks for Patient Name/ID                             │
│        └─ Starts all subsystems automatically                   │
└──────────────────┬──────────────────────────────────────────────┘
                   │
        ┌──────────┴──────────┬──────────────┐
        │                     │              │
        ▼                     ▼              ▼
   ┌─────────┐           ┌─────────┐   ┌─────────┐
   │STREAMER │           │  MAIN   │   │DASHBOARD│
   │(1000Hz) │──LSL──→   │(50Hz)   │──LSL─→(GUI)
   └─────────┘           └─────────┘   └─────────┘
        │                     │              
        │                     │              
    ┌───▼────────────────┐    │              
    │ DATA SOURCES       │    │              
    │ • Mock Data        │    │              
    │ • Real PLUX        │    │              
    └────────────────────┘    │
                              │
                    ┌─────────▼──────────┐
                    │  Output Files      │
                    │ (CSV with patient  │
                    │  info + timestamp) │
                    └────────────────────┘
```

---

## 📁 File Structure & What Can Be Changed

### **🚀 ENTRY POINT**

#### `launcher.py` - Main startup script
- **Purpose:** Single entry point for all users
- **What happens:**
  1. Asks patient for name/ID
  2. Starts streamer (data acquisition)
  3. Starts main (processing pipeline)
  4. Starts dashboard (visualization)
- **Changeable settings:**
  - `STREAM_TIMEOUT_SEC` - How long to wait for data before error
  - Process startup order/timing

---

### **⚙️ CORE CONFIGURATION**

#### `src/config.py` - Central configuration hub
- **Purpose:** All tunable parameters in ONE place
- **Key settings to change:**

| Setting | What It Does | Default | When to Change |
|---------|-------------|---------|----------------|
| `DATA_SOURCE` | Mock or Real PLUX | `'mock'` | See "Switching Modes" section |
| `STREAM_NAME` | LSL stream identifier | `"OpenSignals"` | When connecting real device |
| `STREAM_TYPE` | Device MAC or type | `"00:07:80:0F:31:9C"` | When connecting real device |
| `STREAM_TIMEOUT_SEC` | Silence detection (seconds) | `5.0` | If device drops frequently |
| `BASELINE_SEC` | Calibration duration | `120` | Shorter for testing |
| `EMA_ALPHA_*` | Signal smoothing | `0.05-0.10` | Fine-tune responsiveness |
| `PIPELINE_RATE` | Processing frequency | `50.0 Hz` | Don't change (system constraint) |
| `MOCK_DATA_FILE` | Path to test data | `"data/fake_opensignals_..."` | When adding new test files |

---

### **🔌 DATA ACQUISITION LAYER**

#### `src/data_sources.py` - Hardware abstraction (MODULAR)
- **Purpose:** Factory pattern for switching between mock and real hardware
- **Classes:**
  - `MockDataSource` - Reads from test file (for development)
  - `RealPLUXDataSource` - Connects to real device (for production)
  - `DataSourceFactory` - Auto-selects based on `Config.DATA_SOURCE`
- **To add new hardware:**
  1. Create new class inheriting from `DataSource`
  2. Implement `get_next_sample()` method
  3. Add option to `DataSourceFactory.create()`
  4. Update `Config.DATA_SOURCE` to select it

#### `src/streamer.py` - Universal data broadcaster
- **Purpose:** Streams any data source to LSL at 1000Hz
- **How it works:**
  1. Uses `DataSourceFactory` to get current source
  2. Broadcasts on LSL network
  3. Main.py reads from LSL
- **No changes needed** - works with any data source automatically

#### `src/acquisition.py` - LSL consumer (50Hz sync)
- **Purpose:** Reads LSL stream and converts to 50Hz pipeline
- **Features:**
  - Zero-Order Hold strategy (holds last value if no new data)
  - Stale data detection (disconnection alert after 5 seconds)
  - CSV logging of all received samples
- **Changeable:**
  - `STREAM_TIMEOUT_SEC` in config.py

---

### **🧮 SIGNAL PROCESSING**

#### `src/processing.py` - EMA smoothing + baseline calibration
- **Purpose:** 
  1. Smooth raw signals (exponential moving average)
  2. Collect 120-second baseline
  3. Remove 3-sigma artifacts
  4. Calculate personal averages
- **Key formulas (tunable in config.py):**
  - `EMA_ALPHA_EDA = 0.05` (lower = slower response)
  - `EMA_ALPHA_HR = 0.10`
  - `EMA_ALPHA_HRV = 0.05`
- **Output:** Smoothed signals + personal baselines for comparison

#### `src/fusion.py` - Stress index calculation
- **Purpose:** Converts 3 signals into single stress metric (S_t)
- **Formula:** `S_t = (0.5 × EDA%) + (0.3 × HRV%) + (0.2 × HR%)`
- **Thresholds (set after baseline):**
  - Mild stress: S_t > 1.33 × σ_baseline (yellow)
  - High stress: S_t > 2.28 × σ_baseline (red)
  - Calm: S_t ≤ 1.33 × σ_baseline (green)
- **Changeable:**
  - Weights in `compute_s_instant()` - adjust signal importance
  - Sigma multipliers in `set_thresholds()` - adjust sensitivity

#### `src/main.py` - Main pipeline orchestrator
- **Purpose:** Coordinates all 50Hz processing cycles
- **Phases:**
  1. **BASELINE (0-120s):** Collect data, remove artifacts
  2. **LIVE (120s+):** Calculate stress, broadcast results
- **Error handling:** Graceful shutdown if signal drops

---

### **📊 SESSION TRACKING**

#### `src/session_manager.py` - Centralized state tracking
- **Purpose:** Tracks everything about the session:
  - Patient info (name, ID)
  - Phase (BASELINE vs LIVE)
  - Signal history (for charts)
  - Personal baselines & artifact counts
  - Stress metrics over time
  - Session duration
- **Used by:**
  - `main.py` - records all metrics
  - `output.py` - includes patient info in files
  - `dashboard.py` - displays session state

---

### **📤 OUTPUT & VISUALIZATION**

#### `src/output.py` - Unity/Export bridge
- **Purpose:** Broadcasts results to external systems (Unity VR)
- **Output format:** LSL stream with [S_t, state, score]
- **Future:** Can be extended to save sessions, send to database, etc.

#### `src/dashboard.py` - Clinical visualization (PyQt5)
- **Purpose:** Real-time monitoring dashboard
- **Displays:**
  - 🟢🟡🔴 **Color-coded stress level** (green=calm, yellow=stressed, red=ultra)
  - **Patient info panel** - Name, ID, phase, duration
  - **Stress chart** - Scrollable history with threshold lines
  - **Signal charts** - EDA, HR, HRV individual graphs
  - **Metrics panel** - Baselines, statistics, current state
- **Alarms:** Visual only (color changes) - no sound
- **Files to edit:**
  - Color schemes: Line ~50-55 in dashboard.py
  - Chart update frequency: `self.timer.start(20)` = 50Hz

---

## 🔄 Switching Between Mock & Real Signals

### **Option 1: Mock Data (for testing)**

**Current setting:** Already configured in `src/config.py`

```python
DATA_SOURCE = 'mock'  # Uses synthetic test file
```

**To use:**
```powershell
python launcher.py
# System streams fake_opensignals_2026-05-13_15-24-44.txt at 1000Hz
```

---

### **Option 2: Real PLUX Device (production)**

#### Step 1: Install PLUX Software
- Download OpenSignals from PLUX Sensing
- Install on your machine
- Connect PLUX device via Bluetooth
- Launch OpenSignals and start LSL streaming

#### Step 2: Verify Device is Broadcasting
- Open terminal:
```powershell
python -c "from pylsl import resolve_stream; print(resolve_stream('name', 'OpenSignals'))"
```
- Should return device info (not empty)

#### Step 3: Update Configuration

**In `src/config.py`:**

```python
# Change FROM:
DATA_SOURCE = 'mock'

# Change TO:
DATA_SOURCE = 'real_plux'

# If device has different MAC, update:
STREAM_TYPE = "YOUR_DEVICE_MAC_HERE"  # e.g., "00:07:80:0F:31:9C"
```

#### Step 4: Run System
```powershell
python launcher.py
# System automatically detects and uses real PLUX device
```

#### Troubleshooting Real Device

| Issue | Solution |
|-------|----------|
| "Stream not found" | Ensure OpenSignals is running and streaming |
| No data after baseline | Check Bluetooth connection / battery |
| "Timeout after 5 seconds" | Device dropped - reconnect Bluetooth |
| MAC address wrong | Get from OpenSignals software or device settings |

---

## 👤 Patient Input & Output Files

### **Patient Name/ID Entry**

When you run `launcher.py`, you'll be prompted:
```
[LAUNCHER] Enter patient name or ID: Alice_Patient_001
```

This information is:
- ✅ Displayed on dashboard
- ✅ Saved in all output files
- ✅ Used in CSV filenames with timestamp

### **Output Files (Automatic)**

Sessions automatically save to `data/` folder with:

**Naming format:** `session_{DATE}_{TIME}_{PATIENT_ID}.csv`

**Example:** `session_20260525_143022_Alice_Patient_001.csv`

**Contents:**
```csv
timestamp,phase,patient_id,eda,hr,hrv,s_instant,s_t,state,dashboard_score
2026-05-25 14:30:22.123,BASELINE,Alice_Patient_001,5.01,75.2,39.8,0.0,0.0,calm,0
...
2026-05-25 14:32:22.456,LIVE,Alice_Patient_001,5.45,82.1,35.2,15.3,12.8,stressed,67
```

Files can be imported to Excel, Python, or analysis software.

---

## 🚨 Alarm System (Color-Coded)

### **Visual Indicators**

| Color | Meaning | Stress Level | Action |
|-------|---------|-------------|--------|
| 🟢 **GREEN** | Calm | S_t ≤ mild threshold | Patient relaxed |
| 🟡 **YELLOW** | Stressed | mild < S_t ≤ high threshold | Monitor closely |
| 🔴 **RED** | Ultra Stressed | S_t > high threshold | Intervention needed |

### **Where Alarms Appear**

1. **Dashboard Background** - Entire chart area changes color
2. **Stress Chart** - Background of graph shows current state
3. **State Indicator** - Large text showing "CALM" / "STRESSED" / "ULTRA_STRESSED"
4. **Threshold Lines** - Yellow dashed (mild), red dashed (high)

### **Customizing Colors**

In `src/dashboard.py` around line 170:

```python
# Change these RGB values:
self.color_calm = pg.mkColor(20, 50, 20)      # Currently dark green
self.color_stressed = pg.mkColor(50, 50, 20)  # Currently dark yellow
self.color_ultra = pg.mkColor(50, 20, 20)     # Currently dark red
```

---

## 📝 Implementation Checklist for Colleagues

### **For Testing (Mock Mode)**
- [ ] Clone repository
- [ ] `pip install -r requirements.txt`
- [ ] `python launcher.py`
- [ ] Enter patient name when prompted
- [ ] Check dashboard displays data

### **For Real Device Deployment**
- [ ] Install PLUX OpenSignals
- [ ] Connect PLUX device & verify Bluetooth
- [ ] Update `Config.DATA_SOURCE = 'real_plux'` in config.py
- [ ] Verify `STREAM_TYPE` MAC address matches device
- [ ] Test with `python launcher.py`
- [ ] Check data comes from real device (not synthetic)

### **For Customization**
- [ ] Edit smoothing in `config.py` (`EMA_ALPHA_*`)
- [ ] Adjust stress weights in `fusion.py` (`compute_s_instant()`)
- [ ] Change alarm thresholds in `fusion.py` (`set_thresholds()`)
- [ ] Modify dashboard colors in `dashboard.py`

---

## 🔍 Key Files Quick Reference

| Task | File | Line Range |
|------|------|-----------|
| Change data source | `src/config.py` | Line 8 |
| Adjust signal smoothing | `src/config.py` | Lines 45-48 |
| Change baseline duration | `src/config.py` | Line 49 |
| Modify stress formula | `src/fusion.py` | Line 40-43 |
| Adjust stress thresholds | `src/fusion.py` | Line 18-19 |
| Change dashboard colors | `src/dashboard.py` | Lines 165-168 |
| Modify chart update rate | `src/dashboard.py` | Line 130 |
| Add new data source | `src/data_sources.py` | Add class + factory |

---

## 🆘 Common Issues

| Problem | Solution | File |
|---------|----------|------|
| "LSL stream not found" | Check streamer.py is running | Terminal log |
| Dashboard blank | Ensure main.py is running | Check 3 processes |
| Data stops after 5 seconds | Adjust `STREAM_TIMEOUT_SEC` | `src/config.py` |
| Baseline takes too long | Change `BASELINE_SEC` to 60 | `src/config.py` |
| Signals too noisy | Increase `EMA_ALPHA` | `src/config.py` |
| Alarms too sensitive | Adjust sigma multipliers | `src/fusion.py` |

---

## 📚 For Developers

### **Adding a New Data Source**

1. **Create class in `src/data_sources.py`:**
```python
class MyCustomSource(DataSource):
    def get_next_sample(self) -> tuple:
        # Return (eda, hr, hrv)
        pass
    def cleanup(self):
        pass
```

2. **Add to factory in same file:**
```python
elif source_type == 'my_custom':
    return MyCustomSource()
```

3. **Update `src/config.py`:**
```python
DATA_SOURCE = 'my_custom'
```

### **Extending Output**

To save sessions to database or send to external service:
- Edit `src/session_manager.py` - add export methods
- Modify `src/output.py` - add export formats

### **Dashboard Customization**

To add new metrics:
- Extend `src/session_manager.py` to track metric
- Add panel to `src/dashboard.py` to display
- Update `src/main.py` to record metric

---

## 📞 Support

For issues or questions:
1. Check section "Common Issues" above
2. Review `src/config.py` - 90% of problems solved by adjusting settings
3. Check terminal output - descriptive error messages guide troubleshooting
4. Verify all 3 processes running: streamer, main, dashboard

---

**Last Updated:** May 25, 2026
**Version:** 2.0 (Modular, Patient-Aware, Real Device Ready)
