<#
.SYNOPSIS
    Onboarding smoke test for Windows — exact PowerShell counterpart
    of ``scripts/onboarding-smoke.sh``.

.DESCRIPTION
    Exercises the path a new Windows user takes the first time they
    install the JetBrains plugin / VSCode extension / Tauri app:

        1. Download uv-x86_64-pc-windows-msvc.zip from GitHub releases
        2. ``uv python install 3.12``
        3. ``uv venv``
        4. ``uv pip install ignite-ember`` (or -e <repo> for --local)
        5. Prefetch sentence-transformer model
        6. Launch ``python -m ember_code.backend --ws-port 0`` and
           confirm the JSON ready line lands on stdout

    Mirrors the bash script's step-by-step output + per-step timings.

.PARAMETER Local
    Install ignite-ember from the working tree (``uv pip install -e .``)
    instead of from PyPI. Use this to smoke-test BEFORE publishing a
    new version.

.PARAMETER Version
    Specific ignite-ember version to install. Defaults to whatever's
    in pyproject.toml.

.PARAMETER Keep
    Leave the temp cache dir on disk for inspection.

.EXAMPLE
    pwsh scripts/onboarding-smoke.ps1

.EXAMPLE
    pwsh scripts/onboarding-smoke.ps1 -Local

.EXAMPLE
    pwsh scripts/onboarding-smoke.ps1 -Version 0.6.0
#>

param(
    [switch]$Local,
    [switch]$Keep,
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

# ── Repo root + version resolution ─────────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path

if (-not $Version) {
    $line = Select-String -Path (Join-Path $RepoRoot "pyproject.toml") -Pattern '^version\s*=' | Select-Object -First 1
    if ($line.Line -match 'version\s*=\s*"([^"]+)"') {
        $Version = $Matches[1]
    } else {
        Write-Error "could not parse version from pyproject.toml"
        exit 1
    }
}
Write-Host "-> Targeting ignite-ember version: $Version (local source: $Local)"

# ── Log dir ─────────────────────────────────────────────────────────
# Same arrangement as the bash script: backend logs live OUTSIDE the
# temp cache, at a fixed workspace-relative path, and survive a failing
# run. Cleanup below wipes $CacheDir on every exit including failures,
# so logs kept in there were gone before CI's failure-only upload step
# ran; and ``[System.IO.Path]::GetTempPath()`` resolves to $env:TEMP,
# which is NOT ``runner.temp`` on a GitHub Windows runner — so the
# workflow's old glob could not have matched even if the files had
# survived. Reporting the path (below) removes the guesswork.
$LogDir = if ($env:SMOKE_LOG_DIR) { $env:SMOKE_LOG_DIR } else { Join-Path $RepoRoot ".smoke-logs" }
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
# A stale be-stdout.log from an earlier run still holding a ready line
# would satisfy step 6's poll instantly, so clear it first.
Get-ChildItem -Path $LogDir -Filter "be-*.log" -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
# Publish the real path for the CI job. Written before anything that
# can fail, so the failure-only upload step always has a value.
if ($env:GITHUB_OUTPUT) {
    Add-Content -Path $env:GITHUB_OUTPUT -Value "log-dir=$LogDir"
}
Write-Host "-> Backend logs: $LogDir"

# ── Temp cache ──────────────────────────────────────────────────────
$CacheDir = Join-Path ([System.IO.Path]::GetTempPath()) "ember-onboarding-$(Get-Random)"
New-Item -ItemType Directory -Path $CacheDir | Out-Null
Write-Host "-> Using temp cache: $CacheDir"

# Declared before the try block so Cleanup can see it whether or not
# step 6 was ever reached.
$ProjectDir = $null
# Flipped to $true only on the very last line of the happy path, so
# Cleanup can tell a pass from a failure. ``$LASTEXITCODE`` is no use
# here — every failure in this script goes out as a thrown exception.
$script:Succeeded = $false

# Cleanup hooks: trap Ctrl-C + normal exit so the cache is always
# removed unless ``-Keep`` was passed. ``trap`` in PowerShell fires
# on unhandled exceptions; ``finally`` is the normal-flow hook. Both
# call this one function, and it is idempotent, because on a thrown
# exception both hooks fire.
$keepFlag = $Keep
function Cleanup {
    if ($keepFlag) {
        Write-Host " Keeping cache at $CacheDir (-Keep)"
        if ($ProjectDir) { Write-Host " Keeping project dir at $ProjectDir (-Keep)" }
    } else {
        Remove-Item -Recurse -Force $CacheDir -ErrorAction SilentlyContinue
        if ($ProjectDir) { Remove-Item -Recurse -Force $ProjectDir -ErrorAction SilentlyContinue }
    }

    # Logs outlive a failure — the only run anyone wants them from.
    if (-not $script:Succeeded) {
        Write-Host " Backend logs kept for diagnosis: $LogDir"
    } elseif ($keepFlag) {
        Write-Host " Keeping backend logs at $LogDir (-Keep)"
    } else {
        Remove-Item -Recurse -Force $LogDir -ErrorAction SilentlyContinue
    }
}
trap { Cleanup; break }

try {

# ── Step 1: uv ──────────────────────────────────────────────────────
$UvVersion = "0.5.7"
$Triple = "x86_64-pc-windows-msvc"
$StepStart = Get-Date
Write-Host "-> Step 1: downloading uv $UvVersion for $Triple"
$UvZip = Join-Path $CacheDir "uv.zip"
$UvUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-$Triple.zip"
Invoke-WebRequest -Uri $UvUrl -OutFile $UvZip -UseBasicParsing
Expand-Archive -Path $UvZip -DestinationPath $CacheDir -Force
$UvBin = Get-ChildItem -Path $CacheDir -Filter "uv.exe" -Recurse | Select-Object -First 1 -ExpandProperty FullName
if (-not $UvBin) { throw "uv.exe not found in extracted archive" }
$Elapsed = [int]((Get-Date) - $StepStart).TotalSeconds
Write-Host "  uv ready at $UvBin (${Elapsed}s)"

# ── Step 2: Python ──────────────────────────────────────────────────
$StepStart = Get-Date
Write-Host "-> Step 2: installing Python 3.12 via uv"
& $UvBin python install 3.12
if ($LASTEXITCODE -ne 0) { throw "uv python install failed" }
$Elapsed = [int]((Get-Date) - $StepStart).TotalSeconds
Write-Host "  Python installed (${Elapsed}s)"

# ── Step 3: venv ────────────────────────────────────────────────────
$StepStart = Get-Date
Write-Host "-> Step 3: creating venv"
$VenvDir = Join-Path $CacheDir "venv"
& $UvBin venv --python 3.12 $VenvDir
if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) { throw "venv python not found at $VenvPython" }
$Elapsed = [int]((Get-Date) - $StepStart).TotalSeconds
Write-Host "  venv ready at $VenvDir (${Elapsed}s)"

# ── Step 4: ignite-ember ────────────────────────────────────────────
$StepStart = Get-Date
if ($Local) {
    Write-Host "-> Step 4: installing ignite-ember from local source ($RepoRoot)"
    & $UvBin pip install --python $VenvPython -e $RepoRoot
} else {
    Write-Host "-> Step 4: installing ignite-ember==$Version from PyPI"
    & $UvBin pip install --python $VenvPython "ignite-ember==$Version"
}
if ($LASTEXITCODE -ne 0) { throw "uv pip install failed" }
$Elapsed = [int]((Get-Date) - $StepStart).TotalSeconds
Write-Host "  ignite-ember installed (${Elapsed}s)"

# Sanity: import works.
& $VenvPython -c "import ember_code; print(f'  ember_code v{ember_code.__version__}')"
if ($LASTEXITCODE -ne 0) { throw "ember_code import failed" }

# ── Step 5: prefetch model ──────────────────────────────────────────
$StepStart = Get-Date
Write-Host "-> Step 5: prefetching embedding model"
$env:HF_HOME = Join-Path $CacheDir "hf"
& $VenvPython -m ember_code.prefetch_models
if ($LASTEXITCODE -ne 0) { throw "prefetch_models failed" }
$Elapsed = [int]((Get-Date) - $StepStart).TotalSeconds
Write-Host "  model warmed (${Elapsed}s)"

# ── Step 6: launch BE, await ready ──────────────────────────────────
$StepStart = Get-Date
Write-Host "-> Step 6: launching backend and waiting for ready line"
$ProjectDir = Join-Path ([System.IO.Path]::GetTempPath()) "ember-be-test-$(Get-Random)"
New-Item -ItemType Directory -Path $ProjectDir | Out-Null

# In $LogDir, not $CacheDir, so Cleanup can drop the cache on a failing
# run while still handing these to CI.
$BeStdout = Join-Path $LogDir "be-stdout.log"
$BeStderr = Join-Path $LogDir "be-stderr.log"
$BeProc = Start-Process -FilePath $VenvPython `
    -ArgumentList @("-m", "ember_code.backend", "--ws-port", "0", "--project-dir", $ProjectDir) `
    -RedirectStandardOutput $BeStdout `
    -RedirectStandardError $BeStderr `
    -NoNewWindow `
    -PassThru

# Poll for the ready line for up to 60s.
$Ready = $false
$WsPort = $null
for ($i = 0; $i -lt 60; $i++) {
    if (Test-Path $BeStdout) {
        $content = Get-Content $BeStdout -Raw -ErrorAction SilentlyContinue
        if ($content -match '"status":\s*"ready"') {
            if ($content -match '"ws_port":\s*(\d+)') {
                $WsPort = $Matches[1]
            }
            $Ready = $true
            break
        }
    }
    if ($BeProc.HasExited) {
        Write-Host "x backend exited before signalling ready" -ForegroundColor Red
        if (Test-Path $BeStdout) { Write-Host "--- stdout ---"; Get-Content $BeStdout }
        if (Test-Path $BeStderr) { Write-Host "--- stderr ---"; Get-Content $BeStderr }
        throw "backend exited early"
    }
    Start-Sleep -Seconds 1
}

if (-not $Ready) {
    if (-not $BeProc.HasExited) { Stop-Process -Id $BeProc.Id -Force }
    throw "backend never reported ready within 60s"
}

$Elapsed = [int]((Get-Date) - $StepStart).TotalSeconds
Write-Host "  backend ready on ws://127.0.0.1:$WsPort (${Elapsed}s)"

# Clean shutdown — Windows doesn't have a clean SIGTERM analogue
# for ``Start-Process``-spawned processes, so we go straight to
# ``Stop-Process``. The BE's atexit handlers run via Python's
# normal shutdown path.
Stop-Process -Id $BeProc.Id -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "OK Onboarding smoke passed. A fresh user reaches a running backend through this exact path." -ForegroundColor Green
$script:Succeeded = $true

} finally {
    Cleanup
}
