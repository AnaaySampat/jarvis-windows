# Freeze jarvis-studio-backend into dist/jarvis-backend/ (PyInstaller --onedir)
# and stage the result for Tauri under src-tauri/resources/jarvis-backend/.
#
# BUNDLED installer (default): the heavy runtime assets (Chromium ~170MB, the
# Whisper base model ~150MB, the Piper neural voice ~60MB) are PREFETCHED at build
# time into preload-assets\ and shipped INSIDE the installer, so a fresh install
# works fully OFFLINE with zero first-run downloads. The backend's provisioning.py
# auto-download stays in place as a SELF-REPAIR fallback for any missing/corrupt
# bundled asset. Pass -Lean to skip bundling and ship the small download-on-first-run
# installer instead.
#
# Usage: .\scripts\package-backend.ps1            # bundled (default)
#        .\scripts\package-backend.ps1 -Lean      # download-on-first-run
#        .\scripts\package-backend.ps1 -SkipAssets browser,speech,voice

param(
    [switch]$Lean,
    [string]$SkipAssets = ""
)

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "jarvis-studio-backend"
$GuiRes  = Join-Path $Root "jarvis-studio-gui\src-tauri\resources\jarvis-backend"
$Venv    = Join-Path $Backend ".venv-build"
$Dist    = Join-Path $Backend "dist\jarvis-backend"

Write-Host "Packaging JARVIS Python backend (one-folder)..." -ForegroundColor Cyan

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3.11+ is required on PATH."
}

Push-Location $Backend
try {
    if (-not (Test-Path $Venv)) {
        Write-Host "Creating build venv..." -ForegroundColor DarkGray
        python -m venv $Venv
    }

    $python = Join-Path $Venv "Scripts\python.exe"
    $pyi = Join-Path $Venv "Scripts\pyinstaller.exe"

    Write-Host "Installing release backend deps + PyInstaller..." -ForegroundColor DarkGray
    $req = "requirements-release.txt"
    if (-not (Test-Path $req)) {
        throw "$req is required for the non-OCR installer bundle."
    }
    Write-Host "Using $req (non-OCR installer bundle)" -ForegroundColor DarkGray
    & $python -m pip install -q -r $req pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)." }

    # Pre-fetch openWakeWord's ONNX feature models so PyInstaller can bundle them.
    # Without this the frozen app's wake word silently never fires.
    Write-Host "Fetching wake-word models..." -ForegroundColor DarkGray
    & $python -c "import openwakeword.utils as u; u.download_models()"
    if ($LASTEXITCODE -ne 0) { throw "Failed to download openWakeWord models (exit $LASTEXITCODE)." }

    Write-Host "Running PyInstaller..." -ForegroundColor DarkGray
    & $pyi jarvis-backend.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)." }

    if (-not (Test-Path (Join-Path $Dist "jarvis-backend.exe"))) {
        throw "Expected $Dist\jarvis-backend.exe was not produced."
    }

    # ── Bundle the heavy runtime assets (Chromium / Whisper / Piper) ─────────────
    # Prefetch them into preload-assets\ next to the frozen exe so a fresh install
    # works OFFLINE with no first-run download. The backend reads this folder via
    # <exe dir>\preload-assets\ (browser.py / whisper_transcriber.py / tts_engine.py).
    # provisioning.py still self-repairs anything missing on first launch.
    #
    # We fetch into a PERSISTENT cache (.preload-cache, gitignored) and copy into the
    # freshly-built dist — so a --clean rebuild reuses the ~380MB download instead of
    # re-fetching it every time.
    $Preload = Join-Path $Dist "preload-assets"
    if ($Lean) {
        Write-Host "Lean build (-Lean): assets will download on first launch, not bundled." -ForegroundColor Yellow
        if (Test-Path $Preload) { Remove-Item -Recurse -Force $Preload }
    } else {
        $Cache = Join-Path $Backend ".preload-cache"
        Write-Host "Prefetching bundled assets (Chromium + Whisper + Piper)..." -ForegroundColor DarkGray
        $prefetch = Join-Path $Root "scripts\prefetch_preload_assets.py"
        # Build the arg list so an EMPTY -SkipAssets isn't passed as a bare "--skip"
        # (PowerShell drops empty-string args to native exes → argparse "expected one argument").
        $prefetchArgs = @($Cache)
        if ($SkipAssets) { $prefetchArgs += @("--skip", $SkipAssets) }
        & $python $prefetch @prefetchArgs
        if ($LASTEXITCODE -ne 0) { throw "Asset prefetch failed (exit $LASTEXITCODE). Re-run, or build -Lean to ship download-on-first-run." }
        Write-Host "Copying bundled assets into dist..." -ForegroundColor DarkGray
        if (Test-Path $Preload) { Remove-Item -Recurse -Force $Preload }
        New-Item -ItemType Directory -Force -Path $Preload | Out-Null
        Copy-Item -Path (Join-Path $Cache "*") -Destination $Preload -Recurse -Force
        $pMb = [math]::Round((Get-ChildItem $Preload -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
        Write-Host "Bundled assets staged at $Preload ($pMb MB)" -ForegroundColor DarkGray
    }

    Write-Host "Staging backend for Tauri resources..." -ForegroundColor DarkGray
    if (Test-Path $GuiRes) {
        Remove-Item -Recurse -Force $GuiRes
    }
    New-Item -ItemType Directory -Force -Path $GuiRes | Out-Null
    Copy-Item -Path (Join-Path $Dist "*") -Destination $GuiRes -Recurse -Force

    $sizeMb = [math]::Round((Get-ChildItem $GuiRes -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
    Write-Host "Done. Staged $GuiRes ($sizeMb MB)" -ForegroundColor Green
} finally {
    Pop-Location
}
