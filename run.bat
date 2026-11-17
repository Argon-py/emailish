@echo off
REM One-command start on Windows (works from PowerShell and cmd).
REM   .\run.bat              start with SMTP mailbox checks enabled
REM   .\run.bat --no-smtp    start with SMTP mailbox checks turned off
setlocal
cd /d "%~dp0"
if /i "%~1"=="--no-smtp" set EMAILISH_SMTP_ENABLED=false

if exist .venv\Scripts\python.exe goto run
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython
echo Creating virtual environment and installing dependencies (first run only)...
%PY% -m venv .venv || goto nopython
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt || goto failed

:run
if not defined HOST set HOST=127.0.0.1
if not defined PORT set PORT=8000
echo.
echo   emailish is starting: open http://%HOST%:%PORT% in your browser
if /i "%EMAILISH_SMTP_ENABLED%"=="false" echo   SMTP mailbox checks: OFF
echo   Press Ctrl+C to stop.
echo.
.venv\Scripts\python.exe -m uvicorn app.main:app --host %HOST% --port %PORT%
goto :eof

:nopython
echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during installation, then run this again.
exit /b 1

:failed
echo Installing dependencies failed. Delete the .venv folder and try again.
exit /b 1
