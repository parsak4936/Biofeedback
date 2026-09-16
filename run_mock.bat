@echo off
REM ============================================================
REM  Biofeedback launcher - REPLAY MODE (Windows)
REM ============================================================
REM  Same pipeline as run.bat, but replaying a saved OpenSignals
REM  recording instead of reading a live PLUX device. Use this to
REM  test the dashboard, the Unity telemetry contract, or a code
REM  change without the hardware present.
REM
REM  This sets environment variables for THIS WINDOW ONLY. It does
REM  not edit src/config.py, so run.bat continues to use the real
REM  device and a forgotten replay run cannot leave the lab machine
REM  pointing at recorded data during a real session.
REM
REM  To replay a different recording, pass its path (relative to the
REM  project root) as the first argument:
REM      run_mock.bat data\opensignalDATA\some_other_capture.txt
REM
REM  Sampling rate and ECG/EDA channel order are read from the file's
REM  own header, so recordings made at different rates work as-is.
REM ============================================================

setlocal

cd /d "%~dp0"

if not exist "env\Scripts\activate.bat" (
    echo.
    echo [run_mock.bat] ERROR: virtualenv not found at env\Scripts\activate.bat
    echo.
    echo Create one with:
    echo     python -m venv env
    echo     env\Scripts\activate.bat
    echo     pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM Replay mode for this window only.
set "BIOFEEDBACK_DATA_SOURCE=mock"

REM Optional: a recording path given as the first argument overrides the
REM default in config.py.
if not "%~1"=="" (
    set "BIOFEEDBACK_MOCK_FILE=%~1"
    echo [run_mock.bat] Recording: %~1
)

echo [run_mock.bat] Activating virtualenv...
call "env\Scripts\activate.bat"
if errorlevel 1 (
    echo [run_mock.bat] ERROR: failed to activate virtualenv.
    pause
    exit /b 1
)

echo [run_mock.bat] Starting launcher in REPLAY mode...
echo [run_mock.bat] Real-device mode is unaffected; use run.bat for a session.
echo.
python launcher.py
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
    echo.
    echo [run_mock.bat] launcher.py exited with code %RC%
    pause
)

endlocal
exit /b %RC%
