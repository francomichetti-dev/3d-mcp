@echo off
REM ---------------------------------------------------------------------
REM  3d-mcp - Rhino 8 host setup launcher
REM
REM  Just double-click this file. Say YES to the Windows permission prompt.
REM
REM  Exists so nobody has to type a PowerShell command: chat apps rewrite
REM  "-ExecutionPolicy" into an em-dash, and a copied command carries a path
REM  that only works from one folder. This runs the script sitting next to
REM  it, wherever that happens to be.
REM ---------------------------------------------------------------------

title 3d-mcp - Rhino host setup

REM Re-launch elevated if we are not already Administrator. %~f0 is this
REM file's own full path, so it survives being run from any folder.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Asking Windows for administrator permission...
    echo If a prompt appears, choose YES.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

REM %~dp0 is the folder this file lives in, WITH a trailing backslash.
cd /d "%~dp0"

if not exist "%~dp0windows-rhino-setup.ps1" (
    echo.
    echo   ERROR: windows-rhino-setup.ps1 is not next to this file.
    echo.
    echo   The most common cause is running this from INSIDE the .zip.
    echo   Right-click the .zip, choose "Extract All...", then open the
    echo   extracted folder and double-click this file again.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows-rhino-setup.ps1"

echo.
echo ============================================================
echo  Scroll up, copy the block starting "----- 3d-mcp host report"
echo  and send it back.
echo ============================================================
echo.
pause
