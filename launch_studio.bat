@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m autoshorts_studio.gui
    goto :done
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    python -m autoshorts_studio.gui
    goto :done
)

where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 -m autoshorts_studio.gui
    goto :done
)

echo Python 3.10+ was not found on PATH. Please install Python 3 and run:
echo   pip install -r requirements.txt
pause
exit /b 1

:done
if %errorlevel% neq 0 (
    echo AutoShorts Studio exited with code %errorlevel%.
    pause
)
