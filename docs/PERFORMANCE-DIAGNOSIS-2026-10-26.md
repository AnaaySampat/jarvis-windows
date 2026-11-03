# Jarvis performance diagnosis — 26 October 2026

Measured on this laptop: Intel i5-1135G7, 8 logical processors, 11.8 GiB usable RAM, running on battery during the UI measurements. Both the installed app and the current source checkout were exercised while standing by, with no automation task running.

## Findings

### 1. Idle interface rendering is the largest sustained CPU cost

The installed process tree averaged **25.3% of total laptop CPU** over 35 sampling intervals. Its WebView processes accounted for about **22.7 percentage points**; the Python backend averaged **2.48%**. The busiest WebView process was identified as the GPU process, averaging 15.12% CPU by itself. CPU here is normalized over all eight logical processors, as in Task Manager's overall CPU scale.

The current source app reproduced the problem. Short controlled experiments applied temporary CSS to the running WebViews, measured each state for 12 seconds, and restored the original CSS:

| Source app state | Whole process tree CPU | WebView CPU |
| --- | ---: | ---: |
| Initial normal idle appearance | 31.14% | 27.46% |
| Orb motion and blur disabled | 17.71% | 14.81% |
| All CSS animations and transitions disabled | 5.29% | 3.00% |
| Animations, transitions, filters and backdrop filters disabled | 4.59% | 2.29% |
| Original appearance restored | 25.89% | 22.78% |

This intervention removed about 80–85% of the observed idle CPU cost. It establishes a rendering cause independently of voice transcription or model requests. These are short comparisons, not a guarantee of a fixed percentage under every workload.

The orb has five continuously running CSS animations. Its three blobs combine blur, screen blending, scaling and movement; the halo and surrounding swirl also animate. See `jarvis-studio-gui/src/hud/home.css:225`, particularly lines 235 and 248–255. CSS animation still incurs browser rendering work even though it does not use a JavaScript frame loop.

### 2. A hidden browser panel keeps animating

The browser panel's native window reported **not visible**, while its WebView document reported **visible** and had an active `bp-scan` animation. The control overlay and voice overlay likewise retained live WebViews while their native windows were hidden.

A separate targeted comparison measured:

| State | Whole process tree CPU | WebView CPU |
| --- | ---: | ---: |
| Normal appearance before comparison | 30.32% | 26.67% |
| Only hidden browser scanline animation disabled | 17.02% | 13.88% |
| Original appearance restored | 34.36% | 29.99% |

`BrowserPanel.jsx:83` calculates whether to show the native window; its effect hides that window, but the component still renders the panel and its scanline at `BrowserPanel.jsx:181`. The scanline's `bp-scan` keyframes animate `top` from -140px to 100% indefinitely (`components/browser-panel.css:52–69`). There is no corresponding rendering suspension when the panel is hidden.

Additional isolation showed that animations, filters and live transitions interact: stopping only all animations still left 14.85% total CPU, while stopping only transitions left 32.11%. The comprehensive 4.59% result should not be attributed to one CSS property alone. The exact sustained split between individual effects varies, but the rendering interventions and restored baselines consistently identify the interface as the dominant cause.

### 3. The installed build loads local Whisper even with Groq selected

The user's speech setting was `stt: groq`, with Piper for speech output and continuous listening disabled. Nevertheless, the installed backend log explicitly reported loading the bundled Whisper base model and then `Model ready` during startup.

The installed backend executable was dated **22 October 2026**. Read-only inspection of its frozen Python archive confirmed:

- An eager `faster_whisper` import.
- An unconditional Whisper preload in startup.
- No `_schedule_voice_preloads` guard found in the current source.
- The older GPU poller, which sleeps a fixed 2.5 seconds after each query rather than applying the current failure backoff.

The current source fixes the speech-model behavior: `main.py:5069` only schedules local Whisper when the selected engine is `local`, and `transcription/whisper_transcriber.py:160` rejects local loading in Groq mode. The live source run emitted no Whisper loading message.

An isolated component probe loaded the same local model without microphone use, transcription, or network requests:

| Cumulative loaded components | Resident working set | Private committed memory |
| --- | ---: | ---: |
| Python baseline | 18.0 MiB | 9.9 MiB |
| Import current backend without starting it | 77.0 MiB | 305.8 MiB |
| Add built-in wake-word model | 205.3 MiB | 650.2 MiB |
| Add Piper voice | 284.1 MiB | 731.4 MiB |
| Import faster-whisper | 441.9 MiB | 887.0 MiB |
| Load Whisper base | 568.2 MiB | 1950.5 MiB |

Together, the faster-whisper import and model load added **284.1 MiB resident** and **1219.1 MiB private committed memory**. The locally installed CTranslate2 package also imports optional conversion helpers that import Torch; the component probe confirmed Torch was loaded. The component deltas are specific to this dependency environment and loading order.

### 4. The source app still keeps several substantial components resident

In the live source run, the main backend used about **454 MiB resident / 924 MiB private committed memory**, without local Whisper. Wake-word inference remained active in standby; a separate synthetic-silence test measured **1.39% of total laptop CPU** at its normal 80ms frame cadence.

Four WebViews are created at startup: main HUD, voice overlay, browser panel, and control overlay (`src-tauri/tauri.conf.json:15`). Each component uses its own full `useWebSocket` hook and keeps React state. The four source renderers together held about 266 MiB private committed memory, in addition to the shared WebView browser, GPU and utility processes.

Startup also prewarms Playwright (`main.py:5103`, `actions/browser.py:1011`). Its otherwise idle Node driver held about **70 MiB private / 91–97 MiB resident**. Browser prewarming starts the driver, not a full automation Chromium browser.

`start.py` runs `npm run tauri dev`, retaining npm/Node, Vite, esbuild and Cargo-related processes. In the source snapshot these development processes contributed roughly **0.4 GiB of private committed memory** beyond the app, its backend, WebViews and Playwright driver. They were largely CPU-idle after startup; compilation can add a separate startup spike.

### 5. The HUD gauges report the whole laptop

`telemetry.py:207` reads `psutil.cpu_percent()` and `psutil.virtual_memory()`. The displayed CPU and memory percentages therefore include Windows, other applications and Jarvis together. A displayed 75% memory reading is not 75% used by Jarvis alone.

Before Jarvis started, available physical memory was about 4.4 GiB; during the installed measurement it was about 2.9–3.1 GiB. Jarvis's installed process tree averaged **2660 MiB private committed memory** and **1657 MiB summed resident working sets**. Committed memory is backed by RAM/pagefile and is not the same as currently resident physical RAM. Summing working sets also counts some shared pages more than once.

## Fix order supported by these measurements

1. Stop rendering/animating hidden panels and suspend their WebViews or mount their expensive contents only when needed. Native `hide()` alone did not stop the measured work.
2. Give idle graphics a low-cost/static state, and simplify the orb's moving blur/blending and live transitions. Verify reductions with whole-process-tree measurements, including the GPU process.
3. Rebuild and ship the current Groq-only transcription loading behavior so the installed app stops allocating the unused local Whisper stack.
4. Create auxiliary WebViews and prewarm browser infrastructure on demand; reduce the state each auxiliary view subscribes to.
5. Use a release build for everyday use to remove development-process memory overhead. GPU counter subprocess polling is another optimization target, but it was not the dominant measured CPU consumer here.

No application source or user settings were changed for this diagnosis. Temporary CSS experiments were restored. This investigation establishes high idle resource usage; it does not establish a long-term memory leak or a thermal-throttling diagnosis.

## Local evidence

Diagnostic scripts and JSON are in `.test-tmp/`:

- `jarvis_perf_installed.json`: installed process tree, CPU/RAM samples.
- `jarvis_perf_minimized_confirmed.json`: confirmed minimized-window sample; high graphics CPU persisted.
- `jarvis_webview_probe.json`: comprehensive live source rendering comparison.
- `jarvis_webview_targeted.json`: native/WebView visibility and hidden scanline experiment.
- `jarvis_webview_isolation.json`: animation/filter/transition isolation.
- `jarvis_component_probe.json`: isolated import and model memory costs.
- `jarvis_frozen_inspection.json`: installed frozen-code inspection.

The earlier file named `jarvis_perf_minimized.json` was captured after an unsuccessful minimize action and is not evidence of a minimized state. The confirmed file above supersedes it.
