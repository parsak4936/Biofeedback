@echo off
REM ============================================================
REM  Biofeedback launcher (Windows)
REM ============================================================
REM  Double-click this file or run it from CMD / PowerShell:
REM      run.bat
REM
REM  What it does, in order:
REM    1. Changes to the project root (the directory this .bat lives in).
REM    2. Verifies the local virtualenv exists at env\.
REM    3. Activates the virtualenv (CMD activation script -- no PowerShell
REM       execution policy issues; CMD has no policy concept).
REM    4. Runs launcher.py inside the activated env.
REM    5. On error, pauses so the operator can read the message before
REM       the window closes.
REM
REM  If you want this from PowerShell instead and you keep running into
REM  ExecutionPolicy errors, just run `run.bat` -- this .bat works from
REM  PowerShell too, and it sidesteps the policy entirely.
REM ============================================================

setlocal

REM 1. Always run from the project root (this file's location), even if
REM    the user double-clicked from another directory.
cd /d "%~dp0"

REM 2. Sanity check: the venv must exist. If it does not, point the
REM    operator at the requirements.txt + venv creation steps so they
REM    do not have to dig through README.
if not exist "env\Scripts\activate.bat" (
    echo.
    echo [run.bat] ERROR: virtualenv not found at env\Scripts\activate.bat
    echo.
    echo Create one with:
    echo     python -m venv env
    echo     env\Scripts\activate.bat
    echo     pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM 3. Activate the venv. `call` is required so we return to this script
REM    after activate.bat finishes -- without `call`, control transfers
REM    and never comes back.
echo [run.bat] Activating virtualenv...
call "env\Scripts\activate.bat"
if errorlevel 1 (
    echo [run.bat] ERROR: failed to activate virtualenv.
    pause
    exit /b 1
)

REM 4. Run the launcher. The venv's python.exe is now first on PATH,
REM    so this picks up the right interpreter and packages automatically.
echo [run.bat] Starting biofeedback launcher...
echo.
python launcher.py
set RC=%ERRORLEVEL%

REM 5. If anything went wrong, hold the window open so the operator
REM    can read the traceback. On a clean exit (RC=0) the window
REM    closes immediately, which is what you want for normal use.
if not "%RC%"=="0" (
    echo.
    echo [run.bat] launcher.py exited with code %RC%
    pause
)

endlocal
exit /b %RC%
