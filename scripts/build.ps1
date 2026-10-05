# Build the JARVIS Windows installer (NSIS) with bundled Python backend.
# Prerequisites: Python 3.11+, Node.js 20+, Rust, WebView2 runtime.
# Usage: .\scripts\build.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Gui  = Join-Path $Root "jarvis-studio-gui"

Write-Host "JARVIS release build" -ForegroundColor Cyan

& (Join-Path $Root "scripts\package-backend.ps1")

Push-Location $Gui
try {
    if (-not (Test-Path "node_modules")) {
        npm ci
    }
    npm run tauri:build
} finally {
    Pop-Location
}

$Bundle = Join-Path $Gui "src-tauri\target\release\bundle\nsis"
if (Test-Path $Bundle) {
    Write-Host "`nInstaller output:" -ForegroundColor Green
    Get-ChildItem $Bundle -Filter "*.exe" | ForEach-Object { Write-Host "  $($_.FullName)" }
} else {
    Write-Warning "Bundle folder not found. Check the cargo/tauri build log above."
}

Write-Host "`nThe NSIS installer includes JARVIS.exe and the frozen jarvis-backend folder."
Write-Host "Users only need API keys at first launch - no Python install required."
