@echo off
rem Keep this file ASCII-only: cmd.exe reads .cmd as ANSI and mangles non-ASCII bytes.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8090"

set "PY=python"
where %PY% >nul 2>&1
if errorlevel 1 (
  echo [oc-gov] python not found. Install Python 3.11+ and add it to PATH.
  exit /b 1
)

echo [oc-gov] AI Mayor Workbench  ->  http://127.0.0.1:%PORT%
echo [oc-gov] compass: core\metrics   alerts: core\oversight
"%PY%" -X utf8 app\main.py %PORT%
