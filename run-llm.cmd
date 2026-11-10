@echo off
rem Keep this file ASCII-only: cmd.exe reads .cmd as ANSI and mangles non-ASCII bytes.
rem Use English messages only; non-ASCII console output risks mojibake.
rem
rem Local LLM service (local-qwen38-27b) for the oc-gov workbench.
rem Start order: run this FIRST (port 8099), THEN run.cmd (port 8090).
chcp 65001 >nul
setlocal EnableDelayedExpansion

rem ---- paths: overridable from the environment -------------------------
rem These defaults point at the machine this project was built on. On any
rem other machine they will not exist -- set the two env vars below (or edit
rem the defaults) before running. run-llm.cmd --check reports which is missing
rem without loading anything.
if not defined OCGOV_LLM_RUNTIME_DIR set "OCGOV_LLM_RUNTIME_DIR=D:\溧阳旅游向导\runtime\llama-b11173"
if not defined OCGOV_LLM_MODEL set "OCGOV_LLM_MODEL=E:\comfyui\ComfyUI_Mie_2026_V9.0_Large\ComfyUI\models\LLM\Q38-27B-ud\Qwen3.8-27B-UD-IQ3_XXS.gguf"
set "RUNTIME_DIR=%OCGOV_LLM_RUNTIME_DIR%"
set "MODEL=%OCGOV_LLM_MODEL%"
set "LLAMA=%RUNTIME_DIR%\llama-server.exe"
set "PORT=8099"
set "MODEL_NAME=local-qwen38-27b"

rem ---- --check: validate only, print nothing, exit 2 when files are missing
rem Used by start-all.ps1 as a fast probe, so it never waits 3 minutes for a
rem model this machine does not have. Keep it before every pause below.
if /i "%~1"=="--check" (
  if not exist "%LLAMA%" exit /b 2
  if not exist "%MODEL%" exit /b 2
  exit /b 0
)

if not exist "%LLAMA%" (
  echo [llm] ERROR: llama-server.exe not found:
  echo [llm]   %LLAMA%
  echo [llm] This default path only exists on the build machine.
  echo [llm] Set OCGOV_LLM_RUNTIME_DIR, or just use run.cmd ^(no model needed^).
  pause
  exit /b 1
)

if not exist "%MODEL%" (
  echo [llm] ERROR: model file not found:
  echo [llm]   %MODEL%
  echo [llm] This default path only exists on the build machine.
  echo [llm] Set OCGOV_LLM_MODEL, or just use run.cmd ^(no model needed^).
  pause
  exit /b 1
)

rem ---- port conflict check ----
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [llm] WARNING: port %PORT% is already in use.
  echo [llm] A model may already be running. Test it:  http://127.0.0.1:%PORT%/v1/models
  echo [llm] If it is not this model, stop the other process and rerun.
  pause
  exit /b 1
)

echo [llm] local-qwen38-27b  -^>  http://127.0.0.1:%PORT%/v1
echo [llm] loading ... full model on GPU, about 90 seconds, first tokens appear below.
echo [llm] press Ctrl+C to stop

cd /d "%RUNTIME_DIR%"

rem 27B / IQ3_XXS / full offload to GPU (~11.7GB VRAM)
rem OpenAI-compatible endpoint: http://127.0.0.1:8099/v1
rem In oc-gov add a provider with base_url above, any non-empty key (e.g. local),
rem model name local-qwen38-27b
"%LLAMA%" ^
  -m "%MODEL%" ^
  -ngl 99 ^
  -c 8192 ^
  --host 127.0.0.1 ^
  --port %PORT% ^
  -a %MODEL_NAME% ^
  --jinja ^
  --no-webui

set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo [llm] server exited with error code %RC%.
  echo [llm] check the output above (VRAM, driver, or port issues), then close this window.
  pause
)
endlocal
