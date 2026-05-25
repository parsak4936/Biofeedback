# Biofeedback System - Architecture Deep Dive

## Overview

This document explains the technical architecture for developers and system integrators.

---

## 🎯 System Goals

1. **User-Friendly** - One command (`python launcher.py`) starts everything
2. **Modular** - Easy to swap data sources (mock → real PLUX)
3. **Real-Time** - 50Hz core processing, 1000Hz data acquisition
4. **Extensible** - Add new features without breaking existing code
5. **Traceable** - Full session logging with patient information

---

## 🔄 Data Flow Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ HARDWARE LAYER (Abstracted)                                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐              ┌──────────────┐                  │
│  │  Mock Data   │              │ Real PLUX    │                  │
│  │   (1000Hz)   │──┐        ┌──│  (1000Hz)    │                  │
│  └──────────────┘  │        │  └──────────────┘                  │
│                    │        │                                    │
│                    ▼        ▼                                    │
│               ┌──────────────┐                                   │
│               │   Streamer   │  (src/streamer.py)              │
│               │   (Factory)  │                                   │
│               └──────┬───────┘                                   │
└────────────────────┼─────────────────────────────────────────────┘
                     │
                     │ LSL Network (1000Hz)
                     ▼ "OpenSignals"
        ┌────────────────────────────┐
        │  ACQUISITION LAYER         │
        │  src/acquisition.py        │
        │  • LSL Pull (timeout)      │
        │  • Zero-Order Hold         │
        │  • CSV Logging             │
        └────────────┬───────────────┘
                     │
                     │ (1 every 20 samples)
                     ▼ Raw EDA, HR, HRV
        ┌────────────────────────────┐
        │  PROCESSING LAYER          │
        │  src/processing.py         │
        │  • EMA Smoothing           │
        │  • 120s Baseline Buffer    │
        │  • 3-Sigma Artifact Clean  │
        │  • Personal Averages       │
        └────────────┬───────────────┘
                     │
                     │ 50Hz Smoothed Vector
                     ▼
        ┌────────────────────────────┐
        │  FUSION LAYER              │
        │  src/fusion.py             │
        │  • % Deviation Calc        │
        │  • Weighted S_instant      │
        │  • 1-sec Rolling Average   │
        │  • State Classification    │
        └────────────┬───────────────┘
                     │
                     │ [S_t, state, score]
         ┌───────────┴────────────┐
         ▼                        ▼
    ┌─────────────┐        ┌─────────────┐
    │  Output     │        │  Session    │
    │  src/       │        │  Manager    │
    │  output.py  │        │  (CSV File) │
    │  (Unity)    │        └─────────────┘
    └─────────────┘
         │
         │ LSL "Biofeedback_State"
         ▼
    ┌──────────────────┐
    │  Dashboard       │
    │  src/dashboard.py│
    │  (PyQt5 GUI)     │
    └──────────────────┘
```

---

## 📦 Module Responsibilities

### **1. LAUNCHER (launcher.py)**

**Responsibility:** User entry point & process management

**Tasks:**
1. Prompt for patient name/ID
2. Pass patient info to subprocesses
3. Start 3 worker processes in sequence
4. Monitor until user closes dashboard
5. Graceful shutdown of all processes

**Key Functions:**
- `launch_system()` - Main orchestrator

**Environment Variables Passed:**
```python
env['PATIENT_NAME'] = "Alice"
env['PATIENT_ID'] = "001"
```

**Changes Needed For:**
- Different startup sequence? Edit startup order
- Different data sources? Add to launcher (but config.py is better)

---

### **2. DATA SOURCES (src/data_sources.py)**

**Responsibility:** Hardware abstraction layer

**Classes:**

#### **DataSource (ABC)**
- Abstract interface all sources must implement
- Methods: `get_next_sample()`, `cleanup()`

#### **MockDataSource**
- Reads from `config.MOCK_DATA_FILE`
- Broadcasts at 1000Hz via LSL
- Cycles through file (loops back at end)
- For development/testing

#### **RealPLUXDataSource**
- Connects to existing LSL stream from OpenSignals
- Pulls samples without buffering (let acquisition handle timing)
- Gracefully handles timeouts
- For production use

#### **DataSourceFactory**
- Static factory method: `create()`
- Reads `Config.DATA_SOURCE` value
- Returns appropriate source instance
- **Key for modularity** - changing this one setting changes everything downstream

**To Add New Hardware Source:**

```python
class MyNewDevice(DataSource):
    def __init__(self):
        # Connect to device, setup streams
        pass
    
    def get_next_sample(self) -> tuple:
        # Read from device
        # Return (eda, hr, hrv) tuple
        return (eda_value, hr_value, hrv_value)
    
    def cleanup(self):
        # Close connections
        pass
```

Then in `DataSourceFactory.create()`:
```python
elif source_type == 'my_new_device':
    return MyNewDevice()
```

And in `config.py`:
```python
DATA_SOURCE = 'my_new_device'
```

---

### **3. STREAMER (src/streamer.py)**

**Responsibility:** Universal data broadcaster

**How It Works:**
1. Create data source via factory
2. Loop forever pulling samples
3. Push samples to LSL at native rate (1000Hz)
4. Handle keyboard interrupt gracefully

**Key Insight:**
- **Same code** works with ANY data source
- No changes needed when switching data sources
- Factory pattern makes this possible

**LSL Stream Details:**
- Stream Name: `Config.STREAM_NAME` (default: "OpenSignals")
- Channel Count: 3 (EDA, HR, HRV)
- Sample Rate: 1000Hz
- Format: float32

---

### **4. ACQUISITION (src/acquisition.py)**

**Responsibility:** Convert 1000Hz LSL stream to 50Hz pipeline

**Features:**

1. **Stale Data Detection**
   - Counts consecutive empty pulls
   - After `STREAM_TIMEOUT_SEC` seconds: raises `ConnectionError`
   - Configurable in `config.py`

2. **Zero-Order Hold Strategy**
   - If no new data: repeat last value
   - Maintains 50Hz regularity even if hardware drops samples
   - Real medical devices often have gaps

3. **CSV Logging**
   - `data/acquisition_log_YYYYMMDD_HHMMSS.csv`
   - Records: timestamp, status (NEW_DATA/HOLD_LAST), eda, hr, hrv
   - Useful for debugging dropout patterns

**Error Handling:**
```python
# If 5 seconds of silence:
if self.stale_tick_counter > max_stale_ticks:
    raise ConnectionError(
        f"Stream Lost: No new data for {STREAM_TIMEOUT_SEC} seconds"
    )
```

This is caught in `main.py` and reported to user.

---

### **5. PROCESSING (src/processing.py)**

**Responsibility:** Signal smoothing & baseline calibration

**Phase 1: Baseline (First 120 seconds)**

1. **EMA Smoothing**
   - Formula: `y_t = α·x_t + (1-α)·y_{t-1}`
   - Separate alpha for each signal (tunable in config)
   - Reduces noise while preserving responsiveness

2. **Buffer Accumulation**
   - Store 6000 smoothed samples (120s @ 50Hz)
   - No processing during this phase

**Phase 2: Live Mode (After 120 seconds)**

1. **3-Sigma Artifact Removal**
   - Calculate μ (mean) and σ (std dev) of baseline data
   - Keep only samples in [μ - 3σ, μ + 3σ]
   - Removes ~0.1% of noise automatically
   - Track count: `self.artifacts_removed` dict

2. **Personal Baseline Calculation**
   - Average of cleaned baseline data
   - Stored in `self.personal_averages` dict
   - Used as reference for stress calculation

3. **EMA State Reset**
   - EMA filter continues from baseline end point
   - Smooth transition to live mode

**Tunable Parameters (in config.py):**
```python
EMA_ALPHA_EDA = 0.05    # Lower = more smoothing, slower response
EMA_ALPHA_HR = 0.10     # Higher = less smoothing, faster response
EMA_ALPHA_HRV = 0.05
BASELINE_SEC = 120      # Could reduce to 60 for testing
```

---

### **6. FUSION (src/fusion.py)**

**Responsibility:** Convert 3 signals into single stress metric

**Phase 1: Percentage Deviation (Per Tick)**

From personal baseline, calculate percentage change:
```
ΔE = ((EDA - baseline_eda) / baseline_eda) × 100%
ΔH = ((HR - baseline_hr) / baseline_hr) × 100%
ΔV = ((baseline_hrv - HRV) / baseline_hrv) × 100%  [INVERTED]
```

Note: HRV is inverted because lower HRV = more stress

**Phase 2: Weighted Fusion**

Combine with physiological weights:
```
S_instant = (0.5 × ΔE) + (0.3 × ΔV) + (0.2 × ΔH)
```

Weights can be tuned based on your study results.

**Phase 3: Rolling Average (1 Second)**

Store last 50 S_instant values, output average:
```
S_t = mean(S_instant_buffer[last 50 samples])
```

This smooths out beat-to-beat variability.

**Phase 4: State Classification**

After baseline, set thresholds:
```
mild_threshold = 1.33 × σ_baseline
high_threshold = 2.28 × σ_baseline

if S_t ≤ mild_threshold:
    state = "calm"
elif S_t ≤ high_threshold:
    state = "stressed"
else:
    state = "ultra_stressed"
```

Thresholds based on statistical distribution (normal distribution cumulative).

**Dashboard Score (0-100 mapping)**
```
if S_t ≤ mild:
    score = 50 × (S_t / mild)
elif S_t ≤ high:
    score = 50 + 50 × ((S_t - mild) / (high - mild))
else:
    score = 100
```

**Tuning Points:**

1. **Change weights** in `compute_s_instant()`:
   ```python
   # More weight on EDA = more responsive to skin conductance
   # More weight on HRV = focus on heart rate variability
   s_instant = (0.6 * delta_eda) + (0.2 * delta_hrv) + (0.2 * delta_hr)
   ```

2. **Change sigma multipliers** in `set_thresholds()`:
   ```python
   # More sensitive (lower numbers = more alarms)
   self.thresh_mild = 1.0 * sigma_baseline
   self.thresh_high = 1.5 * sigma_baseline
   ```

---

### **7. SESSION MANAGER (src/session_manager.py)**

**Responsibility:** Centralized state tracking

**Tracks:**
- Patient info (name, ID)
- Phase transitions
- Signal history (recent buffer)
- Baseline statistics
- Stress metrics over time
- Output file path

**Key Methods:**

```python
__init__(patient_name, patient_id)
    # Initialize, create output file

record_raw_sample(eda, hr, hrv)
    # Add to signal history

record_stress_metric(s_instant, s_t, state, score)
    # Add to stress history

log_sample(eda, hr, hrv, s_inst, s_t, state, score)
    # Write row to CSV file

get_current_state_summary() -> dict
    # Return all current state for dashboard

update_phase_baseline(is_baseline_complete)
    # Handle BASELINE → LIVE transition
```

**Output File Details:**

Format: `session_YYYYMMDD_HHMMSS_PatientName_PatientID.csv`

Example: `session_20260525_143022_Alice_001.csv`

CSV Columns:
```
timestamp          | 2026-05-25 14:30:22.123
phase              | BASELINE or LIVE
patient_name       | Alice
patient_id         | 001
eda                | 5.0234 (microsiemens)
hr                 | 75.23 (BPM)
hrv                | 39.87 (ms)
s_instant          | 2.34 (raw stress %)
s_t                | 1.89 (smoothed stress)
state              | calm or stressed or ultra_stressed
dashboard_score    | 45.67 (0-100 scale)
```

---

### **8. OUTPUT (src/output.py)**

**Responsibility:** Broadcast results to external systems

**Current Implementation:**
- Creates LSL outlet: "Biofeedback_State"
- Broadcasts: [S_t, state_enum, dashboard_score]
- State encoding: 0=calm, 1=stressed, 2=ultra_stressed

**Future Extensions:**
- Save sessions to database
- Send alerts to therapist
- Archive for analysis
- Send to other VR systems

**To Add New Output Format:**

```python
def broadcast_to_database(self, s_t, state, score):
    # Connect to DB
    # Insert record
    pass

def broadcast_to_remote_server(self, s_t, state, score):
    # Send HTTP POST/WebSocket
    pass
```

---

### **9. DASHBOARD (src/dashboard.py)**

**Responsibility:** Real-time clinical visualization

**Components:**

1. **Info Panel (Top)**
   - Patient name & ID (blue)
   - Current phase (yellow→green)
   - Session duration
   - Status indicator with color

2. **Stress Chart (Left)**
   - S_t values over time
   - Scrollable (user can pan/zoom)
   - Threshold lines (yellow=mild, red=high)
   - Background color reflects state:
     - Dark green (calm)
     - Dark yellow (stressed)
     - Dark red (ultra stressed)

3. **Signal Charts (Right)**
   - EDA (green line)
   - HR (orange line)
   - HRV (blue line)
   - Individual scrollable histories

4. **Metrics Panel (Bottom)**
   - Personal Baselines box (blue)
   - Current State box (yellow/red depending on state)
   - Session Statistics box (orange)

**Color Scheme:**

```python
# ALARMING (Bright - to catch attention)
state_color_calm = "#00ff00"     # Bright green
state_color_stressed = "#ffff00" # Bright yellow
state_color_ultra = "#ff0000"    # Bright red

# BACKGROUND (Subtle - not overwhelming)
bg_color_calm = pg.mkColor(20, 50, 20)       # Dark green
bg_color_stressed = pg.mkColor(50, 50, 20)   # Dark yellow
bg_color_ultra = pg.mkColor(50, 20, 20)      # Dark red
```

**To Customize:**

```python
# In dashboard.py, around line 165:
self.color_calm = pg.mkColor(20, 50, 20)      # Edit RGB
self.color_stressed = pg.mkColor(50, 50, 20)
self.color_ultra = pg.mkColor(50, 20, 20)
```

**Update Rate:**
- 50Hz (every 20ms)
- Matches pipeline rate
- Smooth visual updates

---

### **10. MAIN (src/main.py)**

**Responsibility:** Pipeline orchestration

**Startup:**
1. Get patient info from environment
2. Create session manager
3. Initialize all processors
4. Print startup info

**Main Loop (50Hz):**

```python
while True:
    # 1. Pull from LSL (handles timeouts)
    raw_vector = acq.get_synchronized_sample()
    
    # 2. Smooth & buffer
    smoothed, is_ready = proc.process_sample(raw_vector)
    
    # 3. If baseline complete:
    if is_ready:
        # Calculate stress
        s_inst = fusion.compute_s_instant(...)
        s_t, state, score = fusion.evaluate_state(s_inst)
        
        # Broadcast
        out.broadcast_state(s_t, state, score)
    
    # 4. Log to CSV
    session.log_sample(...)
    
    # 5. Sleep to maintain 50Hz
    time.sleep(sleep_time)
```

**Error Handling:**
- `KeyboardInterrupt` - User presses Ctrl+C
- `ConnectionError` - Hardware drops signal for 5+ seconds

Both gracefully shut down and print summary.

---

## 🔄 Configuration Override Points

### **System Configuration (config.py)**

| Parameter | Default | Why Change |
|-----------|---------|-----------|
| `DATA_SOURCE` | `'mock'` | Switch to real hardware |
| `STREAM_TIMEOUT_SEC` | `5.0` | Device connection reliability |
| `BASELINE_SEC` | `120` | Faster/slower calibration |
| `EMA_ALPHA_EDA` | `0.05` | Signal responsiveness |
| `BASELINE_SEC` | `120` | Calibration duration |
| `MOCK_DATA_FILE` | `"data/fake..."` | Use different test file |

### **Runtime Configuration (environment)**

Set by launcher.py:
```python
PATIENT_NAME
PATIENT_ID
```

### **Dashboard Configuration (dashboard.py)**

In source code:
- Line ~165: Color schemes
- Line ~130: Update rate (timer.start(20))

---

## 🔌 Integration Points

### **Adding Real Device**

1. **Install device software** (e.g., OpenSignals for PLUX)
2. **Verify LSL stream** - Check it appears in `resolve_stream()`
3. **Create RealPLUXDataSource class** - Already done!
4. **Update config.py** - Change `DATA_SOURCE = 'real_plux'`
5. **Run launcher.py** - System auto-detects and connects

### **Connecting to Unity**

Currently broadcasts on LSL "Biofeedback_State":
```python
[S_t, state_enum, dashboard_score]
```

Unity C# code:
```csharp
inlet = new StreamInlet(stream);
inlet.pull_sample(sample, timestamp);
// sample[0] = S_t (stress value)
// sample[1] = state (0=calm, 1=stressed, 2=ultra)
// sample[2] = score (0-100)
```

### **Database Integration**

Edit `src/output.py` to add method:
```python
def broadcast_to_database(self, s_t, state, score):
    # Insert into database
    pass
```

Call from `main.py`:
```python
out.broadcast_to_database(s_t, state, score)
```

---

## 📊 Performance Characteristics

### **Timing**

```
1. Data Acquisition: 1000Hz (1ms per sample)
2. Pipeline: 50Hz (20ms per cycle)
3. Dashboard Update: 50Hz (20ms per refresh)
4. CSV Logging: ~1ms per write
```

**Latency:**
- Raw data → S_t: ~120ms typical (depends on EMA smoothing)
- After baseline completion: <2ms (real-time)

### **Memory Usage**

```
Baseline phase: ~2-3 MB
Live phase: ~500 KB
Dashboard: ~50 MB (PyQt5 GUI)
```

### **CPU Usage**

```
Streamer: ~1%
Main pipeline: ~3-5%
Dashboard: ~10-15% (GPU-accelerated plotting)
Total: ~20% (on modern CPU)
```

---

## 🧪 Testing Strategy

### **Unit Testing Each Module**

```bash
# Test data sources
python src/data_sources.py

# Test processing
python src/processing.py

# Test fusion
python src/fusion.py

# Test session manager
python src/session_manager.py
```

### **Integration Testing**

```bash
# Full system with mock data
python launcher.py
```

### **Real Hardware Testing**

1. Connect PLUX device
2. Start OpenSignals (ensure LSL streaming)
3. Change `Config.DATA_SOURCE = 'real_plux'`
4. Run `python launcher.py`
5. Verify data appears in dashboard

---

## 📈 Troubleshooting Guide

### **"LSL Stream Not Found"**

**Cause:** Streamer not running or data source not available

**Solution:**
1. Check `Config.DATA_SOURCE` in config.py
2. If 'mock': Verify data file exists
3. If 'real_plux': Start OpenSignals and verify stream

### **"Stream Lost After 5 Seconds"**

**Cause:** Hardware disconnected or dropped

**Solution:**
1. Increase `STREAM_TIMEOUT_SEC` to 10.0 in config.py (temporary)
2. Check hardware connection
3. Review `acquisition_log_*.csv` for dropout pattern

### **Baseline Takes Too Long**

**Cause:** System design (intentional for accurate calibration)

**Solution:**
1. Reduce `BASELINE_SEC` to 60 in config.py (for testing only)
2. Use real system: 120 seconds is standard in clinical practice

### **Alarms Too Sensitive**

**Cause:** Low sigma multiplier thresholds

**Solution:**
1. In `src/fusion.py`, line ~18:
   ```python
   self.thresh_mild = 2.0 * sigma_baseline  # Increase from 1.33
   self.thresh_high = 3.0 * sigma_baseline  # Increase from 2.28
   ```

---

## 📚 References

### **File Organization**

```
f:\Biofeedback\
├── launcher.py              # Entry point
├── requirements.txt         # Dependencies
├── README.md               # User guide
├── ARCHITECTURE.md         # This file
├── src/
│   ├── __init__.py
│   ├── config.py          # Central configuration
│   ├── data_sources.py    # Hardware abstraction
│   ├── streamer.py        # Data broadcaster
│   ├── acquisition.py     # LSL consumer (50Hz)
│   ├── processing.py      # EMA + baseline
│   ├── fusion.py          # Stress calculation
│   ├── session_manager.py # State tracking
│   ├── output.py          # Result broadcasting
│   ├── dashboard.py       # GUI visualization
│   └── main.py            # Pipeline orchestrator
└── data/
    ├── fake_opensignals_*.txt     # Test data
    ├── session_*.csv              # Output files
    ├── acquisition_log_*.csv      # Debug logs
    └── processing_log_*.csv       # Processing logs
```

### **Key Equations**

**Exponential Moving Average:**
$$y_t = \alpha \cdot x_t + (1-\alpha) \cdot y_{t-1}$$

**Percentage Deviation:**
$$\Delta E = \frac{EDA_t - EDA_{baseline}}{EDA_{baseline}} \times 100\%$$

**Weighted Stress Fusion:**
$$S_{instant} = 0.5 \cdot \Delta E + 0.3 \cdot \Delta V + 0.2 \cdot \Delta H$$

**Smoothed Stress (1-second window):**
$$S_t = \text{mean}(S_{instant}[\text{last 50 samples}])$$

**State Classification (normal distribution):**
- Mild: $S_t > 1.33\sigma$ (top 9%)
- High: $S_t > 2.28\sigma$ (top 1%)

---

**Last Updated:** May 25, 2026
**Version:** 2.0
