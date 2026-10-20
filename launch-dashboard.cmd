@echo off
setlocal
"%~dp0Telemetry.exe" %*
if errorlevel 1 (
    echo.
    echo Dashboard launch failed. See the error above.
    pause
    exit /b 1
)
