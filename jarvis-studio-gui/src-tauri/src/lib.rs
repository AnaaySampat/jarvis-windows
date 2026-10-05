use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager, State,
};

struct BackendState {
    token: String,
    child: Mutex<Option<Child>>,
    // Set when the user picks tray → Quit, so CloseRequested lets the windows close
    // (instead of hiding to the tray) and the process can actually exit.
    quitting: AtomicBool,
}

fn tray_icon() -> tauri::image::Image<'static> {
    static RGBA: &[u8] = include_bytes!("../icons/tray_icon.rgba");
    tauri::image::Image::new(RGBA, 32, 32)
}

fn random_token() -> String {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    use std::time::{SystemTime, UNIX_EPOCH};

    let mut hasher = DefaultHasher::new();
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .hash(&mut hasher);
    std::process::id().hash(&mut hasher);
    format!("{:016x}", hasher.finish())
}

fn bundled_backend_dir(app: &tauri::App) -> Option<PathBuf> {
    if let Ok(resource_dir) = app.path().resource_dir() {
        let dir = resource_dir.join("jarvis-backend");
        if dir.join("jarvis-backend.exe").is_file() {
            return Some(dir);
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            for dir in [
                parent.join("jarvis-backend"),
                parent.join("resources").join("jarvis-backend"),
            ] {
                if dir.join("jarvis-backend.exe").is_file() {
                    return Some(dir);
                }
            }
        }
    }

    None
}

fn dev_python_backend_dir() -> Option<PathBuf> {
    if let Ok(dir) = std::env::var("JARVIS_BACKEND_DIR") {
        let path = PathBuf::from(dir);
        if path.join("main.py").is_file() {
            return Some(path);
        }
    }

    if let Ok(manifest) = std::env::var("CARGO_MANIFEST_DIR") {
        let dev = PathBuf::from(&manifest)
            .parent()?
            .parent()?
            .join("jarvis-studio-backend");
        if dev.join("main.py").is_file() {
            return Some(dev);
        }
    }

    None
}

fn apply_windows_no_window(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
}

fn spawn_process(cmd: &mut Command, label: &str) -> Option<Child> {
    match cmd.spawn() {
        Ok(child) => {
            eprintln!("[JARVIS] Started {label}");
            Some(child)
        }
        Err(error) => {
            eprintln!("[JARVIS] Couldn't start backend ({label}): {error}");
            None
        }
    }
}

fn spawn_bundled_backend(dir: &Path, token: &str) -> Option<Child> {
    let exe = dir.join("jarvis-backend.exe");
    let preload_assets = dir.join("preload-assets");
    let playwright_browsers = preload_assets.join("ms-playwright");
    let whisper_model = preload_assets.join("whisper-base");
    let mut cmd = Command::new(&exe);
    cmd.current_dir(dir).env("JARVIS_WS_TOKEN", token);
    // Capture the frozen backend's stdout+stderr to a log file. Without this a
    // startup crash (e.g. a root module missing from the PyInstaller bundle) is
    // silent and the GUI just "can't connect" with no way to see why. The dir is
    // the per-user resource folder, so it's writable for a currentUser install.
    match std::fs::File::create(dir.join("backend.log")) {
        Ok(file) => {
            match file.try_clone() {
                Ok(err) => { cmd.stdout(Stdio::from(file)).stderr(Stdio::from(err)); }
                Err(_) => { cmd.stdout(Stdio::from(file)).stderr(Stdio::null()); }
            }
        }
        Err(_) => { cmd.stdout(Stdio::null()).stderr(Stdio::null()); }
    }
    if preload_assets.is_dir() {
        cmd.env("JARVIS_PRELOAD_ASSETS_DIR", &preload_assets);
    }
    if playwright_browsers.is_dir() {
        cmd.env("PLAYWRIGHT_BROWSERS_PATH", &playwright_browsers);
    }
    if whisper_model.is_dir() {
        cmd.env("JARVIS_BUNDLED_WHISPER_MODEL", &whisper_model);
    }
    apply_windows_no_window(&mut cmd);
    spawn_process(&mut cmd, &format!("frozen backend at {}", dir.display()))
}

fn spawn_python_backend(dir: &Path, token: &str) -> Option<Child> {
    let python = if cfg!(windows) { "python" } else { "python3" };
    let mut cmd = Command::new(python);
    cmd.arg("main.py")
        .current_dir(dir)
        .env("JARVIS_WS_TOKEN", token)
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    apply_windows_no_window(&mut cmd);
    spawn_process(&mut cmd, &format!("Python backend at {}", dir.display()))
}

/// Tie the spawned backend's lifetime to THIS GUI process via a Windows Job Object
/// with KILL_ON_JOB_CLOSE. The job handle is intentionally kept open for the GUI's
/// whole life (never closed); when the GUI exits — cleanly, by crash, or by Task
/// Manager — Windows closes the handle, the job closes, and the backend (plus
/// anything IT spawned, e.g. Chromium) is terminated with it. This is what stops the
/// orphaned `jarvis-backend.exe` that locked the installer's files on reinstall.
#[cfg(windows)]
fn confine_to_job(child: &Child) {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            eprintln!("[JARVIS] CreateJobObject failed; backend may orphan on crash");
            return;
        }
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const std::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        ) == 0
        {
            eprintln!("[JARVIS] SetInformationJobObject failed; backend may orphan");
            return;
        }
        if AssignProcessToJobObject(job, child.as_raw_handle() as _) == 0 {
            eprintln!("[JARVIS] AssignProcessToJobObject failed; backend may orphan");
        }
        // `job` is intentionally NOT closed: its handle must outlive this function and
        // stay open for the GUI's lifetime for KILL_ON_JOB_CLOSE to fire on exit.
    }
}

#[cfg(not(windows))]
fn confine_to_job(_child: &Child) {}

fn spawn_backend(app: &tauri::App, token: &str) -> Option<Child> {
    let child = if let Some(dir) = bundled_backend_dir(app) {
        spawn_bundled_backend(&dir, token)
    } else if let Some(dir) = dev_python_backend_dir() {
        spawn_python_backend(&dir, token)
    } else {
        eprintln!("[JARVIS] No backend found (bundle or dev source tree).");
        None
    };
    if let Some(ref c) = child {
        confine_to_job(c);
    }
    child
}

#[tauri::command]
fn get_ws_token(state: State<'_, BackendState>) -> String {
    state.token.clone()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let token = std::env::var("JARVIS_WS_TOKEN")
                .unwrap_or_else(|_| random_token());

            let skip_spawn = std::env::var("JARVIS_SKIP_BACKEND_SPAWN").is_ok();
            let child = if skip_spawn {
                None
            } else {
                spawn_backend(app, &token)
            };

            app.manage(BackendState {
                token,
                child: Mutex::new(child),
                quitting: AtomicBool::new(false),
            });

            #[cfg(desktop)]
            {
                use tauri_plugin_global_shortcut::{
                    Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState,
                };
                let listen_shortcut = Shortcut::new(Some(Modifiers::CONTROL), Code::Space);
                let shortcut_plugin = app.handle().plugin(
                    tauri_plugin_global_shortcut::Builder::new()
                        .with_handler(move |app, shortcut, event| {
                            if shortcut == &listen_shortcut {
                                // Push-to-talk: hold to listen, release to send.
                                match event.state() {
                                    ShortcutState::Pressed => {
                                        let _ = app.emit("ptt-start", ());
                                    }
                                    ShortcutState::Released => {
                                        let _ = app.emit("ptt-stop", ());
                                    }
                                }
                            }
                        })
                        .build(),
                );

                if let Err(error) = shortcut_plugin {
                    eprintln!("warning: global shortcut plugin failed to initialize: {error}");
                } else if let Err(error) = app.global_shortcut().register(listen_shortcut) {
                    eprintln!("warning: Ctrl+Space global shortcut is unavailable: {error}");
                }
            }

            let quit =
                MenuItem::with_id(app, "quit", "Quit JARVIS", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&quit])?;

            let _tray = TrayIconBuilder::with_id("main")
                .tooltip("JARVIS")
                .icon(tray_icon())
                .menu(&menu)
                .on_menu_event(|app, event| {
                    if event.id.as_ref() == "quit" {
                        // Tear the backend down explicitly BEFORE exiting (don't rely
                        // only on RunEvent::Exit / the job object), and flag the quit so
                        // CloseRequested lets the windows close. Guarantees an explicit
                        // Quit never leaves a lingering jarvis-backend.
                        if let Some(state) = app.try_state::<BackendState>() {
                            state.quitting.store(true, Ordering::SeqCst);
                            if let Ok(mut guard) = state.child.lock() {
                                if let Some(mut child) = guard.take() {
                                    let _ = child.kill();
                                }
                            }
                        }
                        app.exit(0);
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(win) = app.get_webview_window("main") {
                            let _ = win.show();
                            let _ = win.unminimize();
                            let _ = win.set_focus();
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .on_page_load(|webview, payload| {
            // Launched from IDLE, a console or start.py, the HUD opened behind
            // whatever was in front — Windows denies focus to a process that isn't
            // foreground — so "JARVIS doesn't open". A topmost flip raises it
            // without needing focus; set_focus takes focus when Windows allows.
            // Done on the first finished load: the same raise in setup() didn't hold.
            static RAISED: AtomicBool = AtomicBool::new(false);
            if webview.label() == "main"
                && payload.event() == tauri::webview::PageLoadEvent::Finished
                && !RAISED.swap(true, Ordering::SeqCst)
            {
                let win = webview.window();
                let _ = win.set_always_on_top(true);
                let _ = win.set_always_on_top(false);
                let _ = win.set_focus();
            }
        })
        .invoke_handler(tauri::generate_handler![get_ws_token])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                // While quitting (tray → Quit), let windows close so the process exits.
                // Otherwise the X button just hides the HUD/panel to the tray.
                let quitting = window
                    .app_handle()
                    .try_state::<BackendState>()
                    .map(|s| s.quitting.load(Ordering::SeqCst))
                    .unwrap_or(false);
                if !quitting && (window.label() == "main" || window.label() == "browser-panel") {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while running JARVIS")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app.try_state::<BackendState>() {
                    if let Ok(mut guard) = state.child.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        });
}
