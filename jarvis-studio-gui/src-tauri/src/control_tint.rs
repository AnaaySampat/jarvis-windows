//! Small, native corner glows during computer control. No WebViews, input
//! interception, focus changes or full-screen capture surface are involved.

use std::sync::{mpsc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

const LEASE: Duration = Duration::from_secs(4);
const WINDOW_TITLE: &str = "JARVIS Control Tint";

enum Update { Renew(bool), Shutdown }

pub struct ControlTint {
    sender: Option<mpsc::Sender<Update>>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl ControlTint {
    pub fn new() -> Self {
        let (sender, receiver) = mpsc::channel();
        match std::thread::Builder::new().name("jarvis-corner-tint".into())
            .spawn(move || native::run(receiver)) {
            Ok(worker) => Self { sender: Some(sender), worker: Mutex::new(Some(worker)) },
            Err(error) => {
                eprintln!("[ControlTint] Could not start native overlay: {error}");
                Self { sender: None, worker: Mutex::new(None) }
            }
        }
    }

    pub fn renew(&self, active: bool) -> Result<(), String> {
        self.sender.as_ref().ok_or("Screen tint is unavailable")?
            .send(Update::Renew(active)).map_err(|_| "Screen tint worker stopped".into())
    }

    pub fn shutdown(&self) {
        if let Some(sender) = &self.sender { let _ = sender.send(Update::Shutdown); }
        if let Ok(mut worker) = self.worker.lock() {
            if let Some(worker) = worker.take() { let _ = worker.join(); }
        }
    }
}

impl Drop for ControlTint { fn drop(&mut self) { self.shutdown(); } }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Bounds { left: i32, top: i32, right: i32, bottom: i32 }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Corner { x: i32, y: i32, size: i32, right: bool, bottom: bool }

fn corners(bounds: Bounds) -> Vec<Corner> {
    let width = bounds.right - bounds.left;
    let height = bounds.bottom - bounds.top;
    if width <= 0 || height <= 0 { return Vec::new(); }
    // Use physical monitor pixels, including negative monitor origins. Scaling
    // to its short side avoids logical/physical DPI conversion mistakes.
    let size = (width.min(height) / 4).clamp(1, 320);
    [false, true].into_iter().flat_map(|bottom| [false, true].into_iter().map(move |right| Corner {
        x: if right { bounds.right - size } else { bounds.left },
        y: if bottom { bounds.bottom - size } else { bounds.top },
        size, right, bottom,
    })).collect()
}

fn pixels(corner: Corner) -> Vec<u32> {
    let size = corner.size.max(1) as usize;
    let mut result = Vec::with_capacity(size * size);
    for y in 0..size {
        for x in 0..size {
            let dx = if corner.right { size - 1 - x } else { x } as f64;
            let dy = if corner.bottom { size - 1 - y } else { y } as f64;
            let distance = (dx * dx + dy * dy).sqrt() / size.saturating_sub(1).max(1) as f64;
            let alpha = ((1.0 - distance).max(0.0).powi(2) * 100.0).round() as u32;
            // UpdateLayeredWindow expects premultiplied BGRA, not straight RGB.
            let red = 46 * alpha / 255;
            let green = 140 * alpha / 255;
            let blue = alpha;
            result.push((alpha << 24) | (red << 16) | (green << 8) | blue);
        }
    }
    result
}

fn lease_alive(last: Option<Instant>, now: Instant) -> bool {
    last.map(|last| now.saturating_duration_since(last) < LEASE).unwrap_or(false)
}

#[cfg(windows)]
mod native {
    use super::*;
    use std::ptr::{null, null_mut};
    use windows_sys::Win32::Foundation::{GetLastError, HWND, LPARAM, LRESULT, POINT, RECT, SIZE, WPARAM};
    use windows_sys::Win32::Graphics::Gdi::*;
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::HiDpi::{SetThreadDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2};
    use windows_sys::Win32::UI::WindowsAndMessaging::*;

    fn wide(value: &str) -> Vec<u16> { value.encode_utf16().chain(Some(0)).collect() }

    unsafe extern "system" fn window_proc(hwnd: HWND, message: u32, w: WPARAM, l: LPARAM) -> LRESULT {
        match message {
            WM_NCHITTEST => HTTRANSPARENT as LRESULT,
            WM_MOUSEACTIVATE => MA_NOACTIVATE as LRESULT,
            WM_ERASEBKGND => 1,
            _ => DefWindowProcW(hwnd, message, w, l),
        }
    }

    unsafe extern "system" fn monitor_proc(_: HMONITOR, _: HDC, rect: *mut RECT, data: LPARAM) -> i32 {
        if !rect.is_null() {
            let monitors = &mut *(data as *mut Vec<Bounds>);
            let rect = &*rect;
            monitors.push(Bounds { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom });
        }
        1
    }

    unsafe fn monitors() -> Vec<Bounds> {
        let mut result = Vec::new();
        EnumDisplayMonitors(null_mut(), null(), Some(monitor_proc), &mut result as *mut _ as LPARAM);
        result
    }

    unsafe fn paint(hwnd: HWND, corner: Corner) -> Result<(), String> {
        let screen = GetDC(null_mut());
        if screen.is_null() { return Err("Could not obtain display context".into()); }
        let dc = CreateCompatibleDC(screen);
        if dc.is_null() { ReleaseDC(null_mut(), screen); return Err("Could not create tint context".into()); }
        let mut info: BITMAPINFO = std::mem::zeroed();
        info.bmiHeader.biSize = std::mem::size_of::<BITMAPINFOHEADER>() as u32;
        info.bmiHeader.biWidth = corner.size;
        info.bmiHeader.biHeight = -corner.size;
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        info.bmiHeader.biCompression = BI_RGB;
        let mut bits = null_mut();
        let bitmap = CreateDIBSection(screen, &info, DIB_RGB_COLORS, &mut bits, null_mut(), 0);
        if bitmap.is_null() || bits.is_null() {
            if !bitmap.is_null() { DeleteObject(bitmap); }
            DeleteDC(dc); ReleaseDC(null_mut(), screen);
            return Err("Could not create tint bitmap".into());
        }
        let old = SelectObject(dc, bitmap);
        let data = pixels(corner);
        std::ptr::copy_nonoverlapping(data.as_ptr(), bits as *mut u32, data.len());
        let position = POINT { x: corner.x, y: corner.y };
        let origin = POINT { x: 0, y: 0 };
        let size = SIZE { cx: corner.size, cy: corner.size };
        let blend = BLENDFUNCTION { BlendOp: AC_SRC_OVER as u8, BlendFlags: 0,
            SourceConstantAlpha: 255, AlphaFormat: AC_SRC_ALPHA as u8 };
        let ok = UpdateLayeredWindow(hwnd, screen, &position, &size, dc, &origin, 0, &blend, ULW_ALPHA);
        let error = GetLastError();
        SelectObject(dc, old); DeleteObject(bitmap); DeleteDC(dc); ReleaseDC(null_mut(), screen);
        if ok == 0 { Err(format!("Could not paint screen tint ({error})")) } else { Ok(()) }
    }

    unsafe fn destroy(windows: &mut Vec<HWND>) {
        for hwnd in windows.drain(..) { DestroyWindow(hwnd); }
    }

    unsafe fn create(class: &[u16], monitors: &[Bounds]) -> Result<Vec<HWND>, String> {
        let mut windows = Vec::new();
        let title = wide(WINDOW_TITLE);
        for corner in monitors.iter().copied().flat_map(corners) {
            let hwnd = CreateWindowExW(WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
                | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, class.as_ptr(), title.as_ptr(), WS_POPUP,
                corner.x, corner.y, corner.size, corner.size, null_mut(), null_mut(),
                GetModuleHandleW(null()), null());
            if hwnd.is_null() {
                let error = GetLastError(); destroy(&mut windows);
                return Err(format!("Could not create screen tint ({error})"));
            }
            windows.push(hwnd);
            // Excluding these four small surfaces avoids blue pixels affecting
            // the operator's vision without making a whole monitor uncapturable.
            if SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE) == 0 {
                destroy(&mut windows);
                return Err("Windows could not exclude the screen tint from capture".into());
            }
            if let Err(error) = paint(hwnd, corner) { destroy(&mut windows); return Err(error); }
        }
        Ok(windows)
    }

    pub fn run(receiver: mpsc::Receiver<Update>) {
        unsafe {
            // Per-monitor physical coordinates on mixed-scale and left-hand displays.
            SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
            let class = wide("JarvisControlTintWindow");
            let mut wc: WNDCLASSW = std::mem::zeroed();
            wc.lpfnWndProc = Some(window_proc);
            wc.hInstance = GetModuleHandleW(null());
            wc.lpszClassName = class.as_ptr();
            if RegisterClassW(&wc) == 0 {
                eprintln!("[ControlTint] Could not register native window ({})", GetLastError());
                return;
            }
            let mut windows = Vec::new();
            let mut topology = Vec::new();
            let mut last_renewal = None;
            let mut visible = false;
            let mut next_layout_check = Instant::now();
            loop {
                match receiver.recv_timeout(Duration::from_millis(50)) {
                    Ok(Update::Renew(active)) => last_renewal = active.then(Instant::now),
                    Ok(Update::Shutdown) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
                    Err(mpsc::RecvTimeoutError::Timeout) => {},
                }
                let now = Instant::now();
                let active = lease_alive(last_renewal, now);
                if active && ((!visible && !windows.is_empty()) || now >= next_layout_check) {
                    let current = monitors();
                    next_layout_check = now + Duration::from_secs(1);
                    let mut rebuilt = false;
                    if current != topology || windows.is_empty() {
                        destroy(&mut windows);
                        topology = current;
                        match create(&class, &topology) {
                            Ok(created) => { windows = created; rebuilt = true; },
                            Err(error) => eprintln!("[ControlTint] {error}"),
                        }
                    }
                    if !visible || rebuilt {
                        for hwnd in &windows { ShowWindow(*hwnd, SW_SHOWNOACTIVATE); }
                    }
                    visible = !windows.is_empty();
                } else if !active && visible {
                    for hwnd in &windows { ShowWindow(*hwnd, SW_HIDE); }
                    visible = false;
                }
                let mut message: MSG = std::mem::zeroed();
                while PeekMessageW(&mut message, null_mut(), 0, 0, PM_REMOVE) != 0 {
                    TranslateMessage(&message); DispatchMessageW(&message);
                }
            }
            destroy(&mut windows);
            UnregisterClassW(class.as_ptr(), GetModuleHandleW(null()));
        }
    }

    #[cfg(test)]
    mod integration_tests {
        use super::*;
        use windows_sys::Win32::System::Threading::GetCurrentProcessId;

        unsafe extern "system" fn own_tint_window(hwnd: HWND, data: LPARAM) -> i32 {
            let mut pid = 0;
            GetWindowThreadProcessId(hwnd, &mut pid);
            if pid == GetCurrentProcessId() {
                let mut title = [0u16; 80];
                let length = GetWindowTextW(hwnd, title.as_mut_ptr(), title.len() as i32);
                if String::from_utf16_lossy(&title[..length.max(0) as usize]) == WINDOW_TITLE {
                    (&mut *(data as *mut Vec<HWND>)).push(hwnd);
                }
            }
            1
        }

        unsafe fn own_windows() -> Vec<HWND> {
            let mut windows = Vec::new();
            EnumWindows(Some(own_tint_window), &mut windows as *mut _ as LPARAM);
            windows
        }

        fn wait_for(mut condition: impl FnMut() -> bool) {
            let deadline = Instant::now() + Duration::from_secs(7);
            while !condition() {
                assert!(Instant::now() < deadline, "native tint did not reach its expected state");
                std::thread::sleep(Duration::from_millis(40));
            }
        }

        // Explicit opt-in because this briefly displays the actual blue corners
        // on an unlocked Windows desktop. It never sends mouse/keyboard input.
        #[test]
        #[ignore = "requires an interactive Windows desktop; briefly shows the tint"]
        fn native_windows_are_click_through_capture_excluded_and_lease_expires() {
            unsafe {
                SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
                let monitors = monitors();
                assert!(!monitors.is_empty(), "no interactive monitor was found");
                let mut expected: Vec<_> = monitors.into_iter().flat_map(corners)
                    .map(|c| (c.x, c.y, c.x + c.size, c.y + c.size)).collect();
                expected.sort_unstable();
                let tint = ControlTint::new();
                tint.renew(true).unwrap();
                wait_for(|| {
                    let windows = own_windows();
                    windows.len() == expected.len() && windows.iter().all(|hwnd| IsWindowVisible(*hwnd) != 0)
                });
                let windows = own_windows();
                let mut actual = Vec::new();
                for hwnd in &windows {
                    let style = GetWindowLongPtrW(*hwnd, GWL_EXSTYLE) as u32;
                    assert_eq!(style & (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW),
                               WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW);
                    assert_ne!(GetForegroundWindow(), *hwnd, "tint must not take keyboard focus");
                    assert_eq!(SendMessageW(*hwnd, WM_NCHITTEST, 0, 0), HTTRANSPARENT as LRESULT);
                    let mut affinity = 0;
                    assert_ne!(GetWindowDisplayAffinity(*hwnd, &mut affinity), 0);
                    assert_eq!(affinity, WDA_EXCLUDEFROMCAPTURE);
                    let mut rect: RECT = std::mem::zeroed();
                    assert_ne!(GetWindowRect(*hwnd, &mut rect), 0);
                    actual.push((rect.left, rect.top, rect.right, rect.bottom));
                }
                actual.sort_unstable();
                assert_eq!(actual, expected);
                // No renewal: a stalled or disconnected UI must fail closed.
                wait_for(|| own_windows().iter().all(|hwnd| IsWindowVisible(*hwnd) == 0));
                tint.renew(true).unwrap();
                wait_for(|| own_windows().iter().any(|hwnd| IsWindowVisible(*hwnd) != 0));
                tint.renew(false).unwrap();
                wait_for(|| own_windows().iter().all(|hwnd| IsWindowVisible(*hwnd) == 0));
                tint.shutdown();
                assert!(own_windows().is_empty(), "shutdown must destroy its own windows");
            }
        }
    }
}

#[cfg(not(windows))]
mod native {
    use super::*;
    pub fn run(receiver: mpsc::Receiver<Update>) {
        while let Ok(update) = receiver.recv() {
            if let Update::Shutdown = update { break; }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn all_four_corners_follow_negative_monitor_origins() {
        let result = corners(Bounds { left: -1920, top: -100, right: 0, bottom: 980 });
        assert_eq!(result.len(), 4);
        assert_eq!((result[0].x, result[0].y), (-1920, -100));
        assert_eq!((result[1].x + result[1].size, result[1].y), (0, -100));
        assert_eq!((result[2].x, result[2].y + result[2].size), (-1920, 980));
        assert_eq!((result[3].x + result[3].size, result[3].y + result[3].size), (0, 980));
    }

    #[test]
    fn small_displays_do_not_overlap_and_invalid_displays_are_ignored() {
        let result = corners(Bounds { left: 0, top: 0, right: 320, bottom: 200 });
        assert!(result.iter().all(|corner| corner.size <= 50));
        assert!(corners(Bounds { left: 10, top: 0, right: 5, bottom: 20 }).is_empty());
    }

    #[test]
    fn glow_is_blue_premultiplied_and_fades_to_transparent() {
        for right in [false, true] { for bottom in [false, true] {
            let corner = Corner { x: 0, y: 0, size: 16, right, bottom };
            let data = pixels(corner);
            let x = if right { 15 } else { 0 };
            let y = if bottom { 15 } else { 0 };
            let tip = data[y * 16 + x];
            assert_eq!(tip >> 24, 100);
            assert_eq!(data[(15 - y) * 16 + (15 - x)], 0);
            for pixel in data {
                let alpha = pixel >> 24;
                assert!(pixel & 255 <= alpha);
                assert!((pixel >> 8) & 255 <= alpha);
                assert!((pixel >> 16) & 255 <= alpha);
            }
        }}
    }

    #[test]
    fn stopped_or_expired_lease_is_hidden() {
        let now = Instant::now();
        assert!(!lease_alive(None, now));
        assert!(lease_alive(Some(now), now + Duration::from_secs(3)));
        assert!(!lease_alive(Some(now), now + LEASE));
    }
}
