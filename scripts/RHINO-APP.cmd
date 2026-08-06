@echo off
REM ---------------------------------------------------------------------
REM  3d-mcp - open the Rhino window.
REM
REM  Double-click. Nothing to install: it runs on Rhino's own Python.
REM
REM  It needs the broker running (START-BROKER.cmd) and Rhino open with the
REM  poller started. The window says which of those is missing rather than
REM  failing silently, so it is safe to open at any time.
REM ---------------------------------------------------------------------
title 3d-mcp - Rhino

set "PY=%USERPROFILE%\.rhinocode\py39-rh8\pythonw.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.rhinocode\py39-rh8\python.exe"

if not exist "%PY%" (
    echo.
    echo   Rhino's Python was not found at:
    echo     %PY%
    echo.
    echo   Open Rhino once and run the ScriptEditor command - that builds it.
    echo.
    pause
    exit /b 1
)

start "" "%PY%" "%~dp0rhino-app.py"
