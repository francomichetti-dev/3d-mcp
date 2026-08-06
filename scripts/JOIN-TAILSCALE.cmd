@echo off
REM ---------------------------------------------------------------------
REM  3d-mcp - join the tailnet
REM
REM  Double-click this. It asks for the key Franco sends you, and connects.
REM  Nothing is opened to the internet: Tailscale gives this machine a
REM  private 100.x address that only Franco's devices can reach.
REM ---------------------------------------------------------------------

title 3d-mcp - Tailscale

set "TS=%ProgramFiles%\Tailscale\tailscale.exe"

if not exist "%TS%" (
    echo.
    echo   Tailscale is not installed yet.
    echo.
    echo   Opening the download page. Install it, then run this file again.
    echo   You do NOT need to create an account - the key handles that.
    echo.
    start "" "https://tailscale.com/download/windows"
    pause
    exit /b 1
)

echo.
echo   Paste the key Franco sent you and press Enter.
echo   It looks like:  tskey-auth-xxxxxxxxxxxx
echo.
set /p AUTHKEY=Key:

if "%AUTHKEY%"=="" (
    echo.
    echo   No key entered. Nothing done.
    pause
    exit /b 1
)

echo.
echo   Connecting...
"%TS%" up --authkey=%AUTHKEY% --hostname=rhino-pc

if %errorlevel% neq 0 (
    echo.
    echo   That did not work. Two usual reasons:
    echo.
    echo   1. You are already signed in to your OWN Tailscale account. This
    echo      machine has to join Franco's network instead. Run:
    echo            "%TS%" logout
    echo      then start this file again.
    echo.
    echo   2. The key expired or was already used - ask Franco for a fresh one.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Connected. This machine's address on the tailnet:
echo ============================================================
"%TS%" ip -4
echo.
echo   Send that address back to Franco.
echo.
echo   To disconnect at any time:  "%TS%" down
echo   To remove it completely: uninstall Tailscale from
echo   Settings ^> Apps.
echo ============================================================
echo.
pause
