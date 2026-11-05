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
Write-Host "  model (8099) -> workbench (8090)"
Write-Host "=================================================="

# ---- 1. local LLM service ----
if (Test-Port $LLM_PORT) {
    Write-Host "[start] model already running on $LLM_PORT (skip)."
} else {
    Write-Host "[start] starting local model on $LLM_PORT ..."
    if (-not (Test-Path $LLM_CMD)) {
        Write-Host "[start] ERROR: $LLM_CMD not found." -ForegroundColor Red
        Write-Host "Press Enter to exit."
        Read-Host | Out-Null
        exit 1
    }
    Start-Process -FilePath $LLM_CMD -WorkingDirectory $Root -WindowStyle Minimized

    Write-Host "[start] waiting for model to load (about 90s, up to 3min) ..."
    $deadline = (Get-Date).AddSeconds(180)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
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
        Write-Host "[start] continuing anyway; workbench may fail on LLM calls."
    } else {
        Write-Host "[start] model ready on $LLM_URL" -ForegroundColor Green
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
Write-Host "[start] workbench  ->  $APP_URL"
Write-Host "[start] press Ctrl+C to stop the workbench (model keeps running)."
Write-Host ""
& cmd /c "`"$APP_CMD`""

Write-Host ""
Write-Host "[start] workbench exited."
Write-Host "[start] note: the local model on $LLM_PORT is still running."
Write-Host "[start] stop it manually or re-run this script (it will reuse it)."
Write-Host "Press Enter to exit."
Read-Host | Out-Null
