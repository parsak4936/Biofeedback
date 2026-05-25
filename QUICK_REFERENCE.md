# QUICK REFERENCE CARD - Biofeedback System

## 🚀 Quick Start (30 seconds)

```powershell
python launcher.py
Enter patient name: [type name]
Enter patient ID: [type ID or leave blank]
```

✅ Dashboard opens automatically  
✅ Data saved to: `data/session_*.csv`  
✅ Press Ctrl+C in launcher to stop

---

## 🔄 Switching Data Source

### Use Mock Data (Testing)
✅ Default - Already set up

### Use Real PLUX Device

**Step 1:** Power on PLUX, open OpenSignals, enable LSL

**Step 2:** Edit `src/config.py` line 8:
```python
DATA_SOURCE = 'real_plux'  # Was: 'mock'
```

**Step 3:** Run `python launcher.py`

✅ System auto-detects device (no MAC changes needed)

---

## 🎨 Color Meanings

| Color | Meaning | S_t Range |
|-------|---------|-----------|
| 🟢 GREEN | Calm | ≤ mild threshold |
| 🟡 YELLOW | Stressed | mild < S_t ≤ high |
| 🔴 RED | Ultra Stressed | > high threshold |

---

## 📊 Dashboard Zones

```
┌─────────────────────────────────────────────────┐
│ Patient: Alice (001) | Phase: BASELINE | 02:15 │
├──────────────────────┬──────────────────────────┤
│                      │   EDA (μS)               │
│   Stress Chart       │   HR (BPM)               │
│   (Scrollable)       │   HRV (ms)               │
│   [COLOR = STATE]    │   (Scrollable)           │
│                      │                          │
├──────────────────────┴──────────────────────────┤
│ Baseline: EDA 5.0 | HR 75 | HRV 40  | Artifacts: 2 │
│ State: CALM | S_t: 1.23 | Score: 45/100        │
└────────────────────────────────────────────────────┘
```

---

## ⚙️ Most Common Tweaks

### Baseline Takes Too Long?
Edit `src/config.py` line 49:
```python
BASELINE_SEC = 60  # Was: 120 (only for testing!)
```

### Signals Too Noisy?
Edit `src/config.py` lines 45-47:
```python
EMA_ALPHA_EDA = 0.10   # Was: 0.05 (higher = less smooth)
EMA_ALPHA_HR = 0.15
EMA_ALPHA_HRV = 0.10
```

### Alarms Too Sensitive?
Edit `src/fusion.py` lines 18-19:
```python
self.thresh_mild = 2.0 * sigma_baseline  # Was: 1.33
self.thresh_high = 3.0 * sigma_baseline  # Was: 2.28
```

### Change Alarm Colors?
Edit `src/dashboard.py` line ~165:
```python
self.color_calm = pg.mkColor(20, 50, 20)      # Edit RGB
self.color_stressed = pg.mkColor(50, 50, 20)
self.color_ultra = pg.mkColor(50, 20, 20)
```

---

## 📁 Important Files

| File | Purpose |
|------|---------|
| `launcher.py` | **START HERE** - User entry point |
| `src/config.py` | ALL tunable parameters |
| `src/main.py` | Pipeline orchestration |
| `src/dashboard.py` | Visualization |
| `README.md` | Full user guide |
| `ARCHITECTURE.md` | Developer reference |

---

## 🆘 Troubleshooting

| Problem | Solution |
|---------|----------|
| Dashboard blank | Check main.py is running (see terminal) |
| "Stream not found" | Ensure streamer.py started (see launcher) |
| "Timeout after 5s" | Device disconnected - check Bluetooth |
| Alarms never change | Baseline still running? (wait 120s) |
| Baselines show "--" | Baseline not complete yet |

---

## 📝 Output File Format

**Filename:** `session_20260525_143022_Alice_001.csv`

**Columns:**
```
timestamp, phase, patient_name, patient_id, eda, hr, hrv,
s_instant, s_t, state, dashboard_score
```

**Example:**
```
2026-05-25 14:30:22.123,BASELINE,Alice,001,5.01,75.2,39.8,0.0,0.0,calm,0
2026-05-25 14:32:22.456,LIVE,Alice,001,5.45,82.1,35.2,15.3,12.8,stressed,67
```

---

## 💡 Pro Tips

1. **Patient ID** can be any identifier (name, number, initials)
2. **Output files** auto-save every 50Hz - never lose data
3. **Scroll charts** - Click & drag to pan, scroll to zoom
4. **Interrupt cleanly** - Press Ctrl+C in launcher terminal
5. **Test first** - Always test with mock data before real device

---

## 📞 Documentation

- **For Users:** Read README.md
- **For Developers:** Read ARCHITECTURE.md  
- **Full Summary:** Read IMPLEMENTATION_SUMMARY.md

---

**Print this card and keep at workstation!** 📌

Last Updated: May 25, 2026
