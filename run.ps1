$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$frontendDirectory = Join-Path $projectRoot "frontend"
$node = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
$vite = Join-Path $frontendDirectory "node_modules\vite\bin\vite.js"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python environment not found. Create it and install requirements first."
}
if (-not $node) {
    throw "node.exe was not found. Install Node.js 20+ and restart PowerShell."
}
if (-not (Test-Path -LiteralPath $vite)) {
    throw "Frontend dependencies are missing. Run: npm.cmd --prefix frontend install"
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot ".env"))) {
    throw "Missing .env. Copy .env.example to .env and configure it before starting."
}

& $python -c "import fastapi, langgraph, langchain_ollama, sqlglot, uvicorn"
if ($LASTEXITCODE -ne 0) {
    throw "Backend dependencies are missing. Run: .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}

$backendProcess = $null
$frontendProcess = $null

try {
    $backendArguments = @(
        "-m", "uvicorn",
        "--app-dir", (Join-Path $projectRoot "src"),
        "text_to_sql.app:app",
        "--host", "127.0.0.1",
        "--port", "8000",
        "--no-access-log"
    )
    $backendProcess = Start-Process `
        -FilePath $python `
        -ArgumentList $backendArguments `
        -WorkingDirectory $projectRoot `
        -NoNewWindow `
        -PassThru

    $frontendArguments = @($vite, "--host", "127.0.0.1", "--port", "5173")
    $frontendProcess = Start-Process `
        -FilePath $node `
        -ArgumentList $frontendArguments `
        -WorkingDirectory $frontendDirectory `
        -NoNewWindow `
        -PassThru

    $deadline = (Get-Date).AddSeconds(30)
    $backendReady = $false
    $frontendReady = $false

    while ((Get-Date) -lt $deadline) {
        if ($backendProcess.HasExited) {
            throw "FastAPI exited with code $($backendProcess.ExitCode)."
        }
        if ($frontendProcess.HasExited) {
            throw "Vite exited with code $($frontendProcess.ExitCode)."
        }

        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 2
            $backendReady = $health.status -eq "ok"
        }
        catch {
            $backendReady = $false
        }

        try {
            $page = Invoke-WebRequest -Uri "http://127.0.0.1:5173/" -TimeoutSec 2 -UseBasicParsing
            $frontendReady = $page.StatusCode -eq 200
        }
        catch {
            $frontendReady = $false
        }

        if ($backendReady -and $frontendReady) {
            break
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $backendReady -or -not $frontendReady) {
        throw "Timed out waiting for services. See the backend and frontend output above."
    }

    $config = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/config"
    Write-Host "FastAPI:  http://127.0.0.1:8000/docs (healthy)" -ForegroundColor Green
    Write-Host "Frontend: http://127.0.0.1:5173 (HTTP 200)" -ForegroundColor Green
    Write-Host "Approval required: $($config.approval_required)"
    Write-Host "Service logs will appear in this PowerShell window." -ForegroundColor Cyan
    Write-Host "Press Ctrl+C to stop both services." -ForegroundColor Cyan

    while (-not $backendProcess.HasExited -and -not $frontendProcess.HasExited) {
        Start-Sleep -Seconds 1
    }

    if ($backendProcess.HasExited) {
        throw "FastAPI exited with code $($backendProcess.ExitCode)."
    }
    if ($frontendProcess.HasExited) {
        throw "Vite exited with code $($frontendProcess.ExitCode)."
    }
}
finally {
    foreach ($process in @($backendProcess, $frontendProcess)) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "Text-to-SQL services stopped."
}
