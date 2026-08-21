@echo off
setlocal
cd /d "%~dp0"

set "VENV=%~dp0.venv\Scripts"

if not exist "%VENV%\python.exe" (
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

rem Put the venv first on PATH so the launcher detects the interpreter
rem that actually has the runtime dependencies.
set "PATH=%VENV%;%PATH%"

echo Starting AutoQuill launcher...
"%VENV%\python.exe" "%~dp0tools\launcher.py"

endlocal
