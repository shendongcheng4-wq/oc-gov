# start-all.ps1
# One-click starter for the oc-gov workbench: local LLM (8099) + workbench (8090).
# Start order matters: model first, then workbench.
#
# Run by double-clicking, or from PowerShell:  powershell -ExecutionPolicy Bypass -File start-all.ps1
#
# ASCII-only to avoid cmd/PS console mojibake.

$ErrorActionPreference = "Continue"
$LLM_PORT  = 8099
$APP_PORT  = 8090
$LLM_URL   = "http://127.0.0.1:$LLM_PORT/v1/models"
$APP_URL   = "http://127.0.0.1:$APP_PORT/"
$Root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$LLM_CMD   = Join-Path $Root "run-llm.cmd"
$APP_CMD   = Join-Path $Root "run.cmd"

function Test-Port([int]$Port) {
    $found = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $found)
}

Write-Host "=================================================="
Write-Host "  oc-gov one-click starter"
Write-Host "  model ($LLM_PORT) -> workbench ($APP_PORT)"
Write-Host "=================================================="

# ---- 1. local LLM service (optional) ----
if (Test-Port $LLM_PORT) {
    Write-Host "[start] model already running on $LLM_PORT (skip)."
} else {
    # Probe first: run-llm.cmd --check exits 2 when llama-server.exe or the .gguf
    # is absent. Without this probe the script launched it anyway and then polled
    # for up to 180s -- on any machine but the build machine that meant staring at
    # a dead window for three minutes before the workbench even started.
    $probe = 2
    if (Test-Path $LLM_CMD) {
        # `call` is required: with the bare quoted form cmd mis-parses the
        # nested quotes, swallows --check, and always reports 0.
        & cmd /c "call `"$LLM_CMD`" --check" | Out-Null
        $probe = $LASTEXITCODE
    }
    if ($probe -ne 0) {
        Write-Host "[start] local model not available on this machine" -ForegroundColor Yellow
        Write-Host "[start]   (llama-server.exe or the .gguf is missing; run-llm.cmd has the build machine's paths)"
        Write-Host "[start] skipping it -- the workbench does not need a model to start."
        Write-Host "[start] ask-anything will fall back to rule parsing until a model is configured."
        Write-Host "[start] to enable it: set OCGOV_LLM_RUNTIME_DIR and OCGOV_LLM_MODEL, then rerun."
    } else {
        Write-Host "[start] starting local model on $LLM_PORT ..."
        $llmProc = Start-Process -FilePath $LLM_CMD -WorkingDirectory $Root -WindowStyle Minimized -PassThru

        Write-Host "[start] waiting for model to load (about 90s, up to 3min) ..."
        $deadline = (Get-Date).AddSeconds(180)
        $ready = $false
        while ((Get-Date) -lt $deadline) {
            # A crashed launch must not burn the whole budget either.
            if ($llmProc -and $llmProc.HasExited) {
                Write-Host "[start] WARNING: model process exited (code $($llmProc.ExitCode)). Continuing without it." -ForegroundColor Yellow
                break
            }
            Start-Sleep -Seconds 5
            try {
                $r = Invoke-WebRequest -Uri $LLM_URL -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
                if ($r.StatusCode -eq 200) { $ready = $true; break }
            } catch {
                # still loading
            }
        }
        if (-not $ready) {
            Write-Host "[start] WARNING: model did not report ready within 3min." -ForegroundColor Yellow
            Write-Host "[start] continuing anyway; ask-anything falls back to rule parsing until it is up."
        } else {
            Write-Host "[start] model ready on $LLM_URL" -ForegroundColor Green
        }
    }
}

# ---- 2. workbench ----
if (Test-Port $APP_PORT) {
    Write-Host "[start] workbench already running on $APP_PORT."
    Write-Host "[start] open  $APP_URL"
    Start-Process $APP_URL
    Write-Host "Press Enter to exit."
    Read-Host | Out-Null
    exit 0
}

Write-Host "[start] starting workbench on $APP_PORT ..."
if (-not (Test-Path $APP_CMD)) {
    Write-Host "[start] ERROR: $APP_CMD not found." -ForegroundColor Red
    Write-Host "Press Enter to exit."
    Read-Host | Out-Null
    exit 1
}

# Run workbench in foreground so its console output (and any error) stays visible.
# Pass $APP_PORT: without it run.cmd falls back to 8090, so a non-default port
# silently started a second copy on the wrong one. `call` keeps cmd from
# mangling the nested quotes and swallowing the argument.
Write-Host "[start] workbench  ->  $APP_URL"
Write-Host "[start] press Ctrl+C to stop the workbench."
Write-Host ""
& cmd /c "call `"$APP_CMD`" $APP_PORT"

Write-Host ""
Write-Host "[start] workbench exited."
if (Test-Port $LLM_PORT) {
    Write-Host "[start] note: the local model on $LLM_PORT is still running."
    Write-Host "[start] stop it manually or re-run this script (it will reuse it)."
}
Write-Host "Press Enter to exit."
Read-Host | Out-Null
