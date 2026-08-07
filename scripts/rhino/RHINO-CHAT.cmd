@echo off
REM ---------------------------------------------------------------------
REM  Chat with Claude about your Rhino model. Double-click.
REM
REM  Runs on Rhino's own Python. If the packages are missing it says so
REM  and tells you the one command to run.
REM ---------------------------------------------------------------------
title Rhino - Claude

set "PY=%USERPROFILE%\.rhinocode\py39-rh8\pythonw.exe"
set "PYC=%USERPROFILE%\.rhinocode\py39-rh8\python.exe"

if not exist "%PYC%" (
    echo.
    echo   Rhino's Python was not found.
    echo   Open Rhino once and run the ScriptEditor command - that builds it.
    echo.
    pause
    exit /b 1
)

"%PYC%" -c "import webview" 2>nul
if errorlevel 1 (
    echo.
    echo   First run - installing the one package this needs.
    echo   This happens once and takes about a minute.
    echo.
    "%PYC%" -m pip install pywebview
    if errorlevel 1 (
        echo.
        echo   Install failed. Check your internet connection and try again.
        echo.
        pause
        exit /b 1
    )
)

if not exist "%PY%" set "PY=%PYC%"
start "" "%PY%" "%~dp0rhino-chat.py"
