@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY where python3 >nul 2>nul && set PY=python3
if not defined PY (
  echo Python was not found. Install Python from python.org, then run this again.
  pause
  exit /b 1
)
echo Starting server... the browser opens in 2 seconds.
start "" cmd /c "timeout /t 2 >nul & start http://localhost:8000/host"
%PY% quiz_server.py
echo.
echo The server stopped. Please read the message above.
pause
