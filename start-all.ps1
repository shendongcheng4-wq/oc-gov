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
$LLM_BASE  = "http://127.0.0.1:$LLM_PORT/v1"
$LLM_URL   = "$LLM_BASE/models"
$APP_URL   = "http://127.0.0.1:$APP_PORT/"
$Root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$LLM_CMD   = Join-Path $Root "run-llm.cmd"
$APP_CMD   = Join-Path $Root "run.cmd"

function Test-Port([int]$Port) {
    $found = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $found)
}

function Test-LLMReady {
    # A 200 from GET /v1/models only proves "the port is open". llama-server binds the
    # port before the weights finish loading, so /v1/models can answer while
    # chat/completions still refuses -- and a workbench started in that window talks to
    # a socket that is not ready. This is the same reason core/modelcfg.py:probe() sends
    # a real completion instead of trusting GET /models. Probe inference for real.
    try {
        $models = Invoke-RestMethod -Uri $LLM_URL -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        $mid = $null
        if ($models.data -and @($models.data).Count -gt 0) { $mid = @($models.data)[0].id }
        if (-not $mid) { return $false }

        # Hand-built JSON on purpose: ConvertTo-Json on PowerShell 5.1 collapses the
        # single-element messages array into an object, which llama-server rejects (400).
        $body = '{"model":"' + [string]$mid + '","messages":[{"role":"user","content":"ping"}],"max_tokens":1,"stream":false}'
        $resp = Invoke-RestMethod -Uri "$LLM_BASE/chat/completions" -Method Post -Body $body `
            -ContentType 'application/json' -TimeoutSec 20 -ErrorAction Stop
        return ($null -ne $resp.choices -and @($resp.choices).Count -gt 0)
    } catch {
        return $false
    }
}

Write-Host "=================================================="
Write-Host "  oc-gov one-click starter"
Write-Host "  model ($LLM_PORT) -> workbench ($APP_PORT)"
Write-Host "=================================================="

# ---- 1. local LLM service (optional) ----
if (Test-Port $LLM_PORT) {
    Write-Host "[start] model already running on $LLM_PORT (skip launching)."
    # "Port is open" is not the same as "can infer": llama-server binds the port first and
    # loads weights afterwards. A workbench started inside that gap answers every question
    # with "preferred model unavailable, fell back to ..." -- the user reads that as a
    # broken model, when it is only an unfinished load. Warn-then-continue was not enough;
    # wait here instead, so the burden is not pushed onto the first few questions.
    if (Test-LLMReady) {
        Write-Host "[start] model answers inference." -ForegroundColor Green
    } else {
        Write-Host "[start] port is open but inference is not ready yet; waiting (up to 3min) ..." -ForegroundColor Yellow
        $deadline = (Get-Date).AddSeconds(180)
        $ready = $false
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 5
            if (Test-LLMReady) { $ready = $true; break }
        }
        if ($ready) {
            Write-Host "[start] model ready." -ForegroundColor Green
        } else {
            Write-Host "[start] WARNING: still not answering inference after 3min." -ForegroundColor Yellow
            Write-Host "[start]   that process may be wedged: close the model window and rerun this script."
            Write-Host "[start]   workbench starts anyway; questions fall back to the cloud model."
        }
    }
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
            if (Test-LLMReady) { $ready = $true; break }
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
