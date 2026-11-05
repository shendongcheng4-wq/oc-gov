@echo off
rem Keep this file ASCII-only: cmd.exe reads .cmd as ANSI and mangles non-ASCII bytes.
rem Use English messages only; non-ASCII console output risks mojibake.
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8090"

rem ---- prefer the project venv (.venv with octop-harness plugin engine) ----
rem Validate by actually running the interpreter. Do NOT use `where` on a full
rem path: where parses "E:\..." as its path:pattern syntax and exits 2 with
rem "invalid pattern", which would report python missing even when it is there.
set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" (
  "%PY%" -V >nul 2>&1
  if not errorlevel 1 goto :py_ready
)

rem fall back to system python if the venv was not set up (still works, plugin engine just disabled)
set "PY=python"
python -V >nul 2>&1
if errorlevel 1 (
  echo [oc-gov] ERROR: python not found. Install Python 3.11+ and add it to PATH.
  echo [oc-gov] tried: .venv\Scripts\python.exe, python on PATH
  pause
  exit /b 1
)

:py_ready

rem ---- port conflict check ----
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [oc-gov] ERROR: port %PORT% is already in use.
  echo [oc-gov] If oc-gov is already running, open http://127.0.0.1:%PORT%/ directly.
  echo [oc-gov] Otherwise start with another port:  run.cmd 8091
  pause
  exit /b 1
)

echo [oc-gov] AI Mayor Workbench  -^>  http://127.0.0.1:%PORT%
echo [oc-gov] starting ... press Ctrl+C to stop

rem ---- open browser after a short delay (best-effort, ignore failures) ----
rem Use ping for the delay, not timeout: timeout needs a console stdin and dies
rem with "Input redirection is not supported" whenever output is piped.
start "" /b cmd /c "ping -n 4 127.0.0.1 >nul & start "" http://127.0.0.1:%PORT%/"

rem ---- run server in foreground; on crash keep the window open to show the error ----
"%PY%" -X utf8 app\main.py %PORT%
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo [oc-gov] server exited with error code %RC%.
  echo [oc-gov] check the traceback above, then close this window.
  pause
)
endlocal
