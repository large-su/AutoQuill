@echo off
setlocal
cd /d "%~dp0"

set "VENV=%~dp0.venv\Scripts"

if not exist "%VENV%\pythonw.exe" (
    echo [ERROR] Virtual environment not found at:
    echo   %VENV%
    echo.
    echo Create it first with:
    echo   python -m venv .venv
    echo   .venv\Scripts\python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem Put the venv first on PATH so the launcher detects the interpreter.
set "PATH=%VENV%;%PATH%"

rem No-console launch: pythonw has no console window, and "start"
rem closes this cmd window immediately. Launcher/Web logs go to
rem logs\ (launcher.log / webui.log).
start "" "%VENV%\pythonw.exe" "%~dp0tools\launcher.py"

endlocal
