`scripts/package-backend.ps1` stages the frozen Python backend here for the
installer. This file keeps the folder in git, because Tauri's build fails when a
configured resource path is missing.
