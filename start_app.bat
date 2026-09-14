@echo off
REM Starts the app and opens a public HTTPS tunnel to it.
REM
REM Two things have to run at once: the server (does the watching) and
REM the tunnel (lets your phone reach it). Cloudflare's free tunnels
REM are temporary and expire after several hours, so when the link
REM stops working, close both windows and run this again.
REM
REM The tunnel URL CHANGES every time. That's how free quick tunnels
REM work - a fixed address needs a real deployment (Phase 6).

cd /d "%~dp0"
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
  echo Could not find .venv here. Run this from the project folder.
  pause
  exit /b 1
)

REM cloudflared arrives named cloudflared-windows-amd64.exe; people
REM often rename it. Accept either rather than failing on the name.
set CF=
if exist "%USERPROFILE%\Downloads\cloudflared.exe" set CF=%USERPROFILE%\Downloads\cloudflared.exe
if exist "%USERPROFILE%\Downloads\cloudflared-windows-amd64.exe" set CF=%USERPROFILE%\Downloads\cloudflared-windows-amd64.exe

if "%CF%"=="" (
  echo Could not find cloudflared in your Downloads folder.
  echo Download it from Cloudflare, then run this again.
  pause
  exit /b 1
)

echo Starting the server...
start "Moment - server" .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

REM Give the server a moment, so the tunnel doesn't connect to nothing.
timeout /t 5 /nobreak >nul

echo Starting the tunnel...
start "Moment - tunnel" "%CF%" tunnel --url http://localhost:8000

echo.
echo ================================================================
echo  Two windows have opened. Leave BOTH running.
echo.
echo  Look in the "Moment - tunnel" window for a line like:
echo      https://something-random-here.trycloudflare.com
echo.
echo  That is the link to open on your phone. It is NEW every time.
echo ================================================================
echo.
pause
