@echo off
REM ---------------------------------------------------------------------
REM  3d-mcp - Tailscale check and fix
REM
REM  Double-click this. It reports the real connection state and offers to
REM  turn on unattended mode, which is what stops Tailscale dropping every
REM  time the screen locks.
REM ---------------------------------------------------------------------

title 3d-mcp - Tailscale check

set "TS=%ProgramFiles%\Tailscale\tailscale.exe"

if not exist "%TS%" (
    echo.
    echo   Tailscale is not installed at:
    echo     %TS%
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   STATUS
echo ============================================================
"%TS%" status
echo.

echo ============================================================
echo   THIS MACHINE'S TAILNET ADDRESS
echo ============================================================
"%TS%" ip -4
echo.

echo ============================================================
echo   IS IT ACTUALLY UP?
echo ============================================================
"%TS%" netcheck 2>nul | findstr /C:"UDP" /C:"IPv4" /C:"DERP"
echo.

REM Unattended mode is the usual reason a Windows machine "looks connected"
REM but keeps dropping: without it Tailscale stops when you lock or sign out.
echo ============================================================
echo   UNATTENDED MODE
echo ============================================================
echo   Without this, Tailscale disconnects whenever you lock the
echo   screen or sign out - which breaks the remote connection.
echo.
set /p FIX=Turn unattended mode ON now? (y/n):

if /i "%FIX%"=="y" (
    "%TS%" set --unattended=true
    if errorlevel 1 (
        echo.
        echo   That needs administrator rights.
        echo   Right-click this file and choose "Run as administrator".
    ) else (
        echo   Unattended mode is ON. Tailscale will stay connected.
    )
)

echo.
echo ============================================================
echo   If status above says "Logged out" or shows no IP, run:
echo       Right-click the Tailscale icon by the clock -^> Connect
echo.
echo   Copy everything above and send it back.
echo ============================================================
echo.
pause
