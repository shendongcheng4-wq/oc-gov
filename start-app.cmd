@echo off
rem start-app.cmd - double-click button that starts the WORKBENCH ONLY.
rem No local LLM is launched or waited for (that is start-all.cmd's job).
rem ASCII-only: cmd.exe reads .cmd as ANSI and mangles non-ASCII bytes.
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8090"
set "URL=http://127.0.0.1:%PORT%/"

rem ---- already running? just show it, do not start a second copy ----
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [oc-gov] workbench already running on %PORT% - opening browser.
  start "" "%URL%"
  exit /b 0
)

echo ==================================================
echo   oc-gov workbench        model NOT started
echo   %URL%
echo --------------------------------------------------
echo   ask-anything falls back to rule parsing until a
echo   model is configured. Use start-all.cmd instead if
echo   you also want the local model (8099) running.
echo ==================================================
echo.

rem run.cmd re-checks the port, opens the browser, and keeps the console
rem in the foreground so tracebacks stay visible and Ctrl+C stops the server.
call "%~dp0run.cmd" %PORT%

endlocal
