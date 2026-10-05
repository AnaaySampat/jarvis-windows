# PyInstaller spec — one-folder bundle (dist/jarvis-backend/).
# Build: pyinstaller jarvis-backend.spec --noconfirm --clean
#
# "Balanced" build: bundles the full local voice stack (openWakeWord + its ONNX
# models, faster-whisper offline STT fallback, Piper neural TTS) AND browser
# control (Playwright), but EXCLUDES the multi-GB ML extras for camera-OCR
# (torch / easyocr / opencv). The camera-OCR feature degrades gracefully when
# those are absent (its import is guarded), and screen-vision uses the cloud.
#
# First-run browser/voice/STT assets are staged by scripts/package-backend.ps1
# under dist/jarvis-backend/preload-assets/. OCR libraries stay excluded.

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
    collect_dynamic_libs,
)

block_cipher = None
root = Path(SPECPATH)

# ── Python modules to force-include (hidden / dynamically imported) ──────────
hiddenimports = [
    *collect_submodules("actions"),
    *collect_submodules("llm"),
    *collect_submodules("server"),
    # Remote-control crypto identity. device_identity is imported at module top in
    # server/websocket_server.py, but main.py imports it lazily inside a function,
    # so force the package in. `cryptography`'s compiled Rust backend is exactly
    # the kind of binary-only import PyInstaller's static scan drops (C6) — collect
    # its submodules explicitly (PyInstaller's own cryptography hook adds the .pyd).
    *collect_submodules("autonomy"),
    *collect_submodules("cryptography"),
    *collect_submodules("wake_word"),
    *collect_submodules("transcription"),
    *collect_submodules("tts"),
    # Playwright drives browser control, but it's imported lazily INSIDE functions
    # (browser.py), so PyInstaller's static scan can miss the package entirely —
    # force the whole thing in. _impl._driver is what download-on-first-use calls
    # to fetch Chromium via the bundled Node driver. The driver binaries (node.exe
    # + package/cli.js) ride along via collect_data_files("playwright") below.
    *collect_submodules("playwright"),
    # NB: the local "ocr" package (easyocr/opencv camera feature) is intentionally
    # NOT collected — it's dropped in the balanced build and guarded at the call
    # site (main.py), so it fails with a friendly message instead of crashing.
    "jarvis_paths",
    "app_secrets",
    "storage",
    "provisioning",     # first-run asset download — root module, easily missed by the scan
    "telemetry",
    "weather",
    "places",
    "news",
    "memory_store",
    "reminders",
    "routines",
    "playbooks",
    "websockets",
    "websockets.legacy",
    "websockets.legacy.server",
    "httpx",
    "httpx._transports",
    "httpx._transports.default",
    "anyio",
    "sniffio",
    "certifi",
    # ── local voice stack ──
    "openwakeword",
    "openwakeword.model",
    "onnxruntime",
    "pyaudio",
    "numpy",
    # offline STT fallback (faster-whisper → ctranslate2 + av)
    "faster_whisper",
    "ctranslate2",
    "av",
    "tokenizers",
    "huggingface_hub",
    # TTS
    "piper",
    "edge_tts",
    "pygame",
    "pyttsx3",
    "pyttsx3.drivers",
    "pyttsx3.drivers.sapi5",
    # misc features kept in balanced
    "psutil",
    "comtypes",
    "comtypes.client",
    "pycaw",
    "qrcode",
    "mss",
    "pypdf",
    "fpdf",
    "fpdf.fpdf",
    "send2trash",
    "spotipy",
    "pygetwindow",
    "pyperclip",
    "PIL",
    "PIL.Image",
    "google.auth",
    "google.auth.transport.requests",
    "uiautomation",
    "pyautogui",
    "cv2",
    "pynvml",
]

# ── Data files & native libs the above packages load at runtime ─────────────
datas = []
binaries = []

# openWakeWord ships its melspectrogram / embedding / "hey_jarvis" ONNX models as
# package data — without these the wake word silently never fires. (Pre-fetch with
# `python -m openwakeword.utils.download_models` before building; package-backend
# does this automatically.)
datas += collect_data_files("openwakeword")

# Piper needs its bundled assets (espeak-ng phoneme data, etc.). Playwright ships
# its node-based driver as package data — without it the frozen browser can't
# launch. (The Chromium binary itself stays in the user's `playwright install`
# cache, %LOCALAPPDATA%\ms-playwright, which the frozen app reads at runtime.)
for pkg in ("piper", "piper_phonemize", "espeakng_loader", "playwright"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

# Native runtime libraries (DLLs) for the ONNX / ctranslate2 / PyAV backends.
for pkg in ("onnxruntime", "ctranslate2", "av", "piper", "cv2"):
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass

# ── Exclude the multi-GB extras (balanced build) ────────────────────────────
excludes = [
    "torch", "torchvision", "torchaudio",
    "easyocr",
    "ocr",          # local camera-OCR package (guarded; feature dropped)
    "matplotlib",
    "pandas",
    "tkinter",
    "PyQt5", "PySide2", "PySide6", "PyQt6",
    "IPython", "notebook",
]
# NB: scipy + scikit-learn are intentionally KEPT — openWakeWord's __init__
# imports its training module (custom_verifier_model), which needs both just to
# `import openwakeword`. Excluding them would break wake-word detection.

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jarvis-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="jarvis-backend",
)
