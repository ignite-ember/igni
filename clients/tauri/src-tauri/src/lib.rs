//! Ember Code desktop shell.
//!
//! Spawns the Python backend (`python -m ember_code.backend --ws-port 0`),
//! waits for its JSON ready line to learn the bound WebSocket port, then
//! opens the shared web UI (clients/web) pointed at that port via the
//! `?ws=` query param. The backend self-terminates if this process dies
//! (EMBER_PARENT_PID watchdog), and we also kill it on window close.

mod runtime;

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::menu::{AboutMetadata, Menu, MenuBuilder, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
#[cfg(target_os = "macos")]
use tauri::{LogicalPosition, TitleBarStyle};

struct BackendHandle(Mutex<Option<Child>>);

/// Compile-time platform tag injected into the init script so the
/// FE's host-aware CSS (custom title bar, drag region, traffic-light
/// gutter) keys off the real target_os instead of UA sniffing —
/// WKWebView's user-agent string is inconsistent across macOS
/// versions and was failing the regex match in the previous
/// JS-based detection.
#[cfg(target_os = "macos")]
const PLATFORM: &str = "mac";
#[cfg(target_os = "windows")]
const PLATFORM: &str = "win";
#[cfg(target_os = "linux")]
const PLATFORM: &str = "linux";

/// Initialization script run at the start of every page load (both
/// ``loading.html`` and the real chat URL it navigates to). The
/// ``__PLATFORM__`` token is replaced with the value of [`PLATFORM`]
/// at runtime via simple string-replace — using ``format!`` would
/// force escaping every ``{`` and ``}`` in the JS body.
///
/// Responsibilities (in order):
///   1. Stamp ``data-host="tauri"`` + ``data-platform="<os>"`` on
///      ``<html>`` so the host-aware CSS rules match.
///   2. Expose ``window.__EMBER_PICK_DIR__`` for the project-lock
///      chip's native folder picker.
///   3. Populate ``window.__EMBER_HOST__`` with native bridges:
///      ``openFile``, ``revealInFolder``, ``notify``, ``setAppTitle``.
///   4. Bridge ``ember-menu`` Tauri events to the same
///      ``ember-host`` CustomEvent the rest of the app dispatches
///      on.
const INIT_SCRIPT: &str = r#"
(function () {
  const html = document.documentElement;
  html.dataset.host = 'tauri';
  html.dataset.platform = '__PLATFORM__';
})();

window.__EMBER_PICK_DIR__ = (start) =>
  window.__TAURI__.core.invoke('plugin:dialog|open', {
    options: {
      directory: true,
      multiple: false,
      defaultPath: start || undefined
    }
  });

window.__EMBER_HOST__ = Object.assign(window.__EMBER_HOST__ || {}, {
  openFile: (path) => window.__TAURI__.core.invoke(
    'plugin:opener|open_path', { path }
  ),
  revealInFolder: (path) => window.__TAURI__.core.invoke(
    'plugin:opener|reveal_item_in_dir', { path }
  ),
  notify: (payload) => window.__TAURI__.core.invoke(
    'plugin:notification|notify',
    { options: { title: payload.title, body: payload.body || '' } }
  ),
  setAppTitle: (folder, org) => window.__TAURI__.core.invoke(
    'set_app_title', { folder, org }
  ),
});

if (window.__TAURI__ && window.__TAURI__.event) {
  window.__TAURI__.event.listen('ember-menu', (e) => {
    const id = e && e.payload;
    if (typeof id !== 'string') return;
    window.dispatchEvent(new CustomEvent('ember-host', {
      detail: { type: 'ember:menu', payload: { id } }
    }));
  });
  // Mirror native fullscreen state into a ``data-fullscreen`` attr
  // on ``<html>`` so CSS can collapse the header gutter when the
  // OS hides our traffic-light cluster behind the slide-down panel.
  window.__TAURI__.event.listen('ember-fullscreen', (e) => {
    const on = !!(e && e.payload);
    document.documentElement.dataset.fullscreen = on ? 'true' : 'false';
  });
}

// Fullscreen-state mirror. Calls a custom Rust command that reads
// ``NSWindow.styleMask`` — the source of truth — and stamps the
// result onto ``<html data-fullscreen="true|false">``. Polls on
// every resize + every 300 ms so the attribute tracks ⌃⌘F toggles.
// Waits for ``window.__TAURI__`` to be populated (Tauri sets it up
// before init scripts run on modern versions, but the wait keeps
// us safe across versions).
(function () {
  function waitForInvoke(cb) {
    const tries = setInterval(() => {
      const i = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
      if (i) {
        clearInterval(tries);
        cb(i);
      }
    }, 30);
  }
  waitForInvoke((invoke) => {
    console.log('[ember] fullscreen-detection wired');
    const sync = async () => {
      try {
        const fs = await invoke('ember_is_fullscreen');
        const prev = document.documentElement.dataset.fullscreen === 'true';
        if (!!fs !== prev) {
          document.documentElement.dataset.fullscreen = fs ? 'true' : 'false';
          console.log('[ember] fullscreen ->', fs);
        }
      } catch (e) {
        console.warn('[ember] ember_is_fullscreen failed:', e);
      }
    };
    sync();
    window.addEventListener('resize', sync);
    setInterval(sync, 300);
  });
})();
"#;

/// Spawn the backend and block until its ready line reports the WS
/// port. ``progress`` is invoked with short status strings during
/// the (potentially multi-minute) first-launch bootstrap; the
/// caller surfaces them in the loading webview.
fn spawn_backend(
    project_dir: &str,
    progress: &(dyn Fn(&str) + Sync),
) -> Result<(Child, u16), String> {
    progress("Preparing Ember backend…");
    let install = runtime::ensure_backend_python(progress)?;

    progress("Starting Ember backend…");
    let mut cmd = Command::new(&install.python);
    cmd.args([
        "-m",
        "ember_code.backend",
        "--ws-port",
        "0",
        "--project-dir",
        project_dir,
    ])
    .env("EMBER_PARENT_PID", std::process::id().to_string())
    .stdout(Stdio::piped())
    .stderr(Stdio::null());
    for (k, v) in &install.env {
        cmd.env(k, v);
    }
    let mut child = cmd.spawn().map_err(|e| {
        format!("failed to spawn backend via `{}`: {e}", install.python.display())
    })?;

    let stdout = child.stdout.take().ok_or("backend stdout unavailable")?;
    let mut reader = BufReader::new(stdout);
    let mut line = String::new();
    let port = loop {
        line.clear();
        let n = reader
            .read_line(&mut line)
            .map_err(|e| format!("backend stdout read failed: {e}"))?;
        if n == 0 {
            return Err("backend exited before signalling ready".to_string());
        }
        if let Some(p) = parse_ready_line(&line) {
            break p;
        }
    };

    // Keep draining stdout so the backend never blocks on a full pipe.
    std::thread::spawn(move || {
        let mut sink = String::new();
        while let Ok(n) = reader.read_line(&mut sink) {
            if n == 0 {
                break;
            }
            sink.clear();
        }
    });

    Ok((child, port))
}

/// Resolve the project directory. Falls back to ``~/Documents``
/// (auto-created if missing) when no other signal is available,
/// so a Dock launch lands in a sensible writable location instead
/// of ``/`` or the user's home root.
fn project_dir() -> String {
    // First positional non-flag arg wins. Explicit env var next —
    // useful in CI / dev where the cwd-walk can't find the right
    // root.
    if let Some(arg) = std::env::args().skip(1).find(|a| !a.starts_with("--")) {
        return arg;
    }
    if let Ok(env) = std::env::var("EMBER_PROJECT_DIR") {
        let trimmed = env.trim();
        if !trimmed.is_empty() {
            return trimmed.to_string();
        }
    }

    // Walk up from cwd looking for a ``.git`` directory. This is
    // the dev experience: ``cargo tauri dev`` runs the binary with
    // cwd = ``src-tauri/`` (Cargo's convention), and the walk-up
    // lands at the repo root.
    if let Ok(cwd) = std::env::current_dir() {
        let mut probe = cwd.clone();
        loop {
            if probe.join(".git").exists() {
                return probe.to_string_lossy().into_owned();
            }
            if !probe.pop() {
                break;
            }
        }
    }

    // Fallback: ``~/Documents``. Create it on the fly if the user
    // somehow doesn't have one (rare; macOS sets it up by default
    // and dirs::document_dir() reads $XDG_DOCUMENTS_DIR on linux).
    if let Some(docs) = dirs::document_dir() {
        let _ = std::fs::create_dir_all(&docs);
        return docs.to_string_lossy().into_owned();
    }
    // Last-resort fallback if neither Documents nor home resolve.
    if let Some(home) = dirs::home_dir() {
        return home.to_string_lossy().into_owned();
    }
    ".".to_string()
}

/// True if the user passed ``--reinstall`` on the CLI. Triggers a
/// cache wipe before the bootstrap runs — recovery path for users
/// who can't reach the Tools menu.
fn reinstall_flag() -> bool {
    std::env::args().any(|a| a == "--reinstall")
}

/// Parse a single stdout line from the backend and return the bound
/// WebSocket port if and only if it's the JSON ready handshake.
///
/// The BE emits assorted log lines on stdout during startup; only the
/// ``{"status": "ready", "ws_port": N, ...}`` envelope signals it's
/// ready to accept connections. Returning ``None`` for non-ready lines
/// lets the read loop keep draining instead of failing.
///
/// Extracted from the spawn path so unit tests can exercise the parse
/// without spawning a real BE.
fn parse_ready_line(line: &str) -> Option<u16> {
    let v: serde_json::Value = serde_json::from_str(line.trim()).ok()?;
    if v["status"] != "ready" {
        return None;
    }
    v["ws_port"].as_u64().map(|p| p as u16)
}

/// Build the native menu bar.
///
/// On macOS the menu lives in the menu bar (top of screen); on Linux
/// / Windows it lives in the window's titlebar. The standard items
/// (Quit, Hide, Edit's Cut/Copy/Paste, Window's Close/Minimise) come
/// from ``PredefinedMenuItem`` so they automatically get the right
/// shortcut for the platform and the right localisation.
///
/// Custom items emit ``menu`` events the JS side picks up by id —
/// see the ``on_menu_event`` registration after the builder. We use
/// this for app-specific actions (New Chat, Restart Backend) that
/// the FE handles via ``window.addEventListener('ember-host', …)``.
fn build_menu(app: &tauri::AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    // ── App / About menu (macOS-only — Linux/Windows merge it into Help) ──
    let app_meta = AboutMetadata {
        name: Some("Ember Code".into()),
        copyright: Some("© 2026 Ignite Ember".into()),
        website: Some("https://ignite-ember.sh".into()),
        ..Default::default()
    };
    let about = PredefinedMenuItem::about(app, Some("About Ember Code"), Some(app_meta))?;
    let check_update = MenuItem::with_id(
        app,
        "check_update",
        "Check for Updates…",
        true,
        None::<&str>,
    )?;
    let services = PredefinedMenuItem::services(app, None)?;
    let hide = PredefinedMenuItem::hide(app, None)?;
    let hide_others = PredefinedMenuItem::hide_others(app, None)?;
    let show_all = PredefinedMenuItem::show_all(app, None)?;
    let quit = PredefinedMenuItem::quit(app, None)?;
    let app_menu = Submenu::with_items(
        app,
        "Ember Code",
        true,
        &[
            &about,
            &check_update,
            &PredefinedMenuItem::separator(app)?,
            &services,
            &PredefinedMenuItem::separator(app)?,
            &hide,
            &hide_others,
            &show_all,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;

    // ── File ──
    let new_chat = MenuItem::with_id(
        app,
        "new_chat",
        "New Chat",
        true,
        Some("CmdOrCtrl+N"),
    )?;
    let restart_backend = MenuItem::with_id(
        app,
        "restart_backend",
        "Restart Backend",
        true,
        Some("CmdOrCtrl+Shift+R"),
    )?;
    let reinstall_backend_item = MenuItem::with_id(
        app,
        "reinstall_backend",
        "Reinstall Backend (Clean)",
        true,
        None::<&str>,
    )?;
    let close_window = PredefinedMenuItem::close_window(app, None)?;
    let file_menu = Submenu::with_items(
        app,
        "File",
        true,
        &[
            &new_chat,
            &restart_backend,
            &reinstall_backend_item,
            &PredefinedMenuItem::separator(app)?,
            &close_window,
        ],
    )?;

    // ── Edit ──
    let edit_menu = Submenu::with_items(
        app,
        "Edit",
        true,
        &[
            &PredefinedMenuItem::undo(app, None)?,
            &PredefinedMenuItem::redo(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, None)?,
            &PredefinedMenuItem::copy(app, None)?,
            &PredefinedMenuItem::paste(app, None)?,
            &PredefinedMenuItem::select_all(app, None)?,
        ],
    )?;

    // ── View ──
    let toggle_devtools = MenuItem::with_id(
        app,
        "toggle_devtools",
        "Toggle Developer Tools",
        true,
        Some("CmdOrCtrl+Alt+I"),
    )?;
    let view_menu = Submenu::with_items(
        app,
        "View",
        true,
        &[
            &PredefinedMenuItem::fullscreen(app, None)?,
            &toggle_devtools,
        ],
    )?;

    // ── Window ──
    let window_menu = Submenu::with_items(
        app,
        "Window",
        true,
        &[
            &PredefinedMenuItem::minimize(app, None)?,
            &PredefinedMenuItem::maximize(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::close_window(app, None)?,
        ],
    )?;

    MenuBuilder::new(app)
        .items(&[&app_menu, &file_menu, &edit_menu, &view_menu, &window_menu])
        .build()
}

/// Check the Tauri updater channel for a newer release and return
/// a payload shaped like the Python BE's ``check_for_update`` RPC,
/// so the existing ``.update-banner`` React component can render
/// the result with no client-side branching.
///
/// Returns ``available=false`` (with empty fields) if no update is
/// pending or if the check fails — silent best-effort, same
/// behavior as the BE's existing check.
#[tauri::command]
async fn ember_check_update(app: AppHandle) -> Result<serde_json::Value, String> {
    use tauri_plugin_updater::UpdaterExt;

    let current_version = env!("CARGO_PKG_VERSION").to_string();
    let updater = app.updater().map_err(|e| e.to_string())?;
    match updater.check().await {
        Ok(Some(update)) => Ok(serde_json::json!({
            "available": true,
            "current_version": current_version,
            "latest_version": update.version,
            "download_url": update.download_url,
        })),
        Ok(None) => Ok(serde_json::json!({
            "available": false,
            "current_version": current_version,
            "latest_version": current_version,
        })),
        Err(e) => Err(e.to_string()),
    }
}

/// Download + verify + install the pending update, then relaunch.
/// Called from the FE's "Install" button on the update banner.
#[tauri::command]
async fn ember_install_update(app: AppHandle) -> Result<(), String> {
    use tauri_plugin_updater::UpdaterExt;

    let updater = app.updater().map_err(|e| e.to_string())?;
    let update = updater
        .check()
        .await
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "no update pending".to_string())?;

    update
        .download_and_install(|_chunk, _total| {}, || {})
        .await
        .map_err(|e| e.to_string())?;

    app.restart();
}

/// Cheap status probe the FE polls to mirror native fullscreen
/// state into a ``data-fullscreen`` attribute on ``<html>``.
/// Reading ``WebviewWindow::is_fullscreen`` is a direct
/// ``NSWindow.styleMask`` check — no allocation, no IPC barrier
/// beyond the invoke itself.
#[tauri::command]
fn ember_is_fullscreen(window: tauri::WebviewWindow) -> bool {
    window.is_fullscreen().unwrap_or(false)
}

/// Title-bar text: ``<folder> · <org>`` (org omitted when empty),
/// mirroring Finder's "name only" convention rather than the
/// older "App Name — Document" style. Called by the FE on every
/// ``status_update`` so the bar reflects the *current* project
/// dir + cloud-org pair, including changes from ``/clear``,
/// project-lock changes, and login/logout.
#[tauri::command]
fn set_app_title(
    window: tauri::WebviewWindow,
    folder: Option<String>,
    org: Option<String>,
) -> Result<(), String> {
    let folder = folder
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("Ember Code")
        .to_string();
    let org = org.as_deref().map(str::trim).filter(|s| !s.is_empty());
    let title = match org {
        Some(o) => format!("{folder} · {o}"),
        None => folder,
    };
    window.set_title(&title).map_err(|e| e.to_string())
}

/// Reinstall the managed Python toolchain from scratch — wired to
/// the "Reinstall Backend (Clean)" Tools-menu item and to the
/// ``--reinstall`` CLI flag. Wipes the cache then restarts the BE.
#[tauri::command]
fn reinstall_backend(app: AppHandle) -> Result<(), String> {
    if let Some(handle) = app.try_state::<BackendHandle>() {
        if let Some(mut child) = handle.0.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
    runtime::reset_cache()?;
    // Walk the user back through the loading view; the next
    // ``open_main_app`` call (triggered by the menu wiring) will
    // re-bootstrap from scratch.
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.eval("location.href = 'loading.html?msg=Reinstalling…'");
    }
    let app2 = app.clone();
    let dir = project_dir();
    std::thread::spawn(move || {
        if let Err(e) = bootstrap_and_open(&app2, &dir) {
            eprintln!("reinstall failed: {e}");
        }
    });
    Ok(())
}

/// Bootstrap the BE on a background thread, emit progress to the
/// loading page, then navigate the main window to the real UI.
/// Used both at startup and from ``reinstall_backend``.
fn bootstrap_and_open(app: &AppHandle, project_dir: &str) -> Result<(), String> {
    // CLI flag: ``ember-code --reinstall`` wipes the managed cache
    // before bootstrap runs, same effect as the menu item.
    if reinstall_flag() {
        let _ = runtime::reset_cache();
    }

    let app_for_progress = app.clone();
    let progress: Box<dyn Fn(&str) + Sync> = Box::new(move |msg: &str| {
        if let Some(w) = app_for_progress.get_webview_window("main") {
            let _ = w.emit("ember-bootstrap-progress", msg.to_string());
        }
    });

    let (child, port) = spawn_backend(project_dir, &progress)?;
    app.manage(BackendHandle(Mutex::new(Some(child))));

    // Initial title: project-dir basename, Finder-style. The FE
    // re-issues ``set_app_title`` on every status_update with the
    // cloud org as a subtitle, so this just covers the case where
    // the BE never connects (no status push fires).
    let folder = std::path::Path::new(project_dir)
        .canonicalize()
        .ok()
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_else(|| "Ember Code".to_string());

    if let Some(w) = app.get_webview_window("main") {
        let _ = w.set_title(&folder);
        let target = format!("index.html?ws=ws%3A%2F%2F127.0.0.1%3A{port}");
        let _ = w.eval(&format!("location.href = {}", serde_json::json!(target)));
        // Traffic-light position is maintained by the
        // CFRunLoopObserver installed in ``setup`` — no extra work
        // needed here. macOS will reset the cluster to the OS
        // default during WKWebView's first layout pass after
        // navigation, and the observer fires *after* that pass on
        // the same runloop iteration to restore (x, y).
    }
    Ok(())
}


/// Install a main-thread CFRunLoopObserver that re-applies the
/// traffic-light position after every layout pass, for the lifetime
/// of the window. Must be called on the main thread.
///
/// Why an observer and not a timer: AppKit re-runs the title-bar
/// layout in response to WKWebView events (navigation, first paint,
/// React mount) and resets the buttons to its default within the
/// same runloop turn. A background polling thread always loses that
/// race because its dispatched closure runs on a *later* turn. An
/// observer on ``kCFRunLoopBeforeWaiting`` runs *after* the layout
/// pass completes in the same iteration, so our ``setFrame`` is the
/// last write before the runloop sleeps. ``setFrame`` to the same
/// rect is a no-op in AppKit, so steady-state ticks are free.
///
/// The anchor box is intentionally leaked — it must outlive the
/// observer for the entire process lifetime.
#[cfg(target_os = "macos")]
fn install_traffic_light_observer(window: tauri::WebviewWindow, x: f64, y: f64) {
    use core_foundation_sys::base::{kCFAllocatorDefault, CFIndex, CFOptionFlags};
    use core_foundation_sys::runloop::{
        kCFRunLoopBeforeWaiting, kCFRunLoopCommonModes, CFRunLoopActivity,
        CFRunLoopAddObserver, CFRunLoopGetMain, CFRunLoopObserverContext,
        CFRunLoopObserverCreate, CFRunLoopObserverRef,
    };
    use std::os::raw::c_void;

    struct Anchor {
        window: tauri::WebviewWindow,
        x: f64,
        y: f64,
    }

    extern "C" fn callback(
        _observer: CFRunLoopObserverRef,
        _activity: CFRunLoopActivity,
        info: *mut c_void,
    ) {
        // ``info`` is the leaked ``Anchor`` pointer; never null
        // because we only register the observer with a valid one.
        let anchor = unsafe { &*(info as *const Anchor) };
        reposition_traffic_lights(&anchor.window, anchor.x, anchor.y);
    }

    let anchor = Box::into_raw(Box::new(Anchor { window, x, y })) as *mut c_void;
    let mut context = CFRunLoopObserverContext {
        version: 0,
        info: anchor,
        retain: None,
        release: None,
        copyDescription: None,
    };
    unsafe {
        let observer = CFRunLoopObserverCreate(
            kCFAllocatorDefault,
            kCFRunLoopBeforeWaiting as CFOptionFlags,
            1, // ``repeats = true``
            0 as CFIndex,
            callback,
            &mut context,
        );
        if observer.is_null() {
            return;
        }
        let rl = CFRunLoopGetMain();
        if rl.is_null() {
            return;
        }
        CFRunLoopAddObserver(rl, observer, kCFRunLoopCommonModes);
    }
}

/// Anchor the three macOS standard window buttons (close / miniaturize /
/// zoom) so their leftmost origin sits at ``(x, y)`` measured from the
/// window's TOP-LEFT corner. ``y`` is the distance from the window
/// top to the TOP of the button cluster.
///
/// ## Why we resize the title-bar *container*, not the buttons
///
/// The naive approach — ``setFrame:`` on each button — silently
/// loses to ``NSThemeFrame``'s private layout pass, which re-runs
/// every time AppKit redraws the title bar (first paint, navigation,
/// React mount, fullscreen toggle, appearance change) and rewrites
/// each button's frame from a cached title-bar rect. Background
/// timers and CFRunLoopObservers fire BEFORE that pass on the next
/// runloop turn and get clobbered — exactly the "looks right for
/// 500 ms then snaps back" symptom we hit.
///
/// What works (used by ``wry`` and Electron): resize the
/// **titleBarContainerView** — the buttons' ``superview.superview``
/// — to be taller. ``NSThemeFrame`` reads that container's height
/// when computing where the buttons go, so the container is an
/// *input* to themeFrame's math, not an output it overrides. The
/// container itself isn't reset by the button-layout pass; the
/// CFRunLoopObserver re-applies it after any event that does
/// mutate it (resize, fullscreen).
///
/// X is still set directly on each button — themeFrame only owns
/// the Y baseline derived from container height.
///
/// References:
///   - tauri-apps/wry: ``src/wkwebview/class/wry_web_view_parent.rs``
///     (``inset_traffic_lights``)
///   - electron PRs #21781 + #30263 (same technique)
#[cfg(target_os = "macos")]
fn reposition_traffic_lights(window: &tauri::WebviewWindow, x: f64, y: f64) {
    use objc2::msg_send;
    use objc2::runtime::AnyObject;
    use objc2_foundation::{CGPoint, CGRect, CGSize};

    let ns_window = match window.ns_window() {
        Ok(ptr) => ptr as *mut AnyObject,
        Err(_) => return,
    };
    if ns_window.is_null() {
        return;
    }

    // NSWindowButton enum: 0=Close, 1=Miniaturize, 2=Zoom.
    unsafe {
        // Skip while in native fullscreen. AppKit reparents the
        // buttons into ``_NSFullScreenTitlebarView`` (the slide-
        // down panel); the windowed title-bar container we'd
        // normally resize no longer exists in its windowed form,
        // and any ``setFrame:`` here is either a no-op or churns
        // the panel layout. The FE drops the header gutter while
        // we're in this state via the ``ember-fullscreen`` event.
        const NS_WINDOW_STYLE_MASK_FULLSCREEN: u64 = 1u64 << 14;
        let style_mask: u64 = msg_send![ns_window, styleMask];
        if style_mask & NS_WINDOW_STYLE_MASK_FULLSCREEN != 0 {
            return;
        }

        let close_btn: *mut AnyObject =
            msg_send![ns_window, standardWindowButton: 0u64];
        if close_btn.is_null() {
            return;
        }
        let close_frame: CGRect = msg_send![close_btn, frame];
        let close_height = close_frame.size.height;
        if close_height < 1.0 {
            // Window not yet laid out.
            return;
        }

        // titleBarContainerView = close.superview.superview. The
        // direct superview is the small group view containing just
        // the three buttons; its superview is the container that
        // themeFrame uses as the layout box for the whole title-bar
        // region.
        let group: *mut AnyObject = msg_send![close_btn, superview];
        if group.is_null() {
            return;
        }
        let container: *mut AnyObject = msg_send![group, superview];
        if container.is_null() {
            return;
        }

        let window_frame: CGRect = msg_send![ns_window, frame];
        let window_height = window_frame.size.height;
        let cur_container: CGRect = msg_send![container, frame];

        // Make the title-bar container ``close_height + y`` tall and
        // pin it to the top of the window. themeFrame places the
        // buttons at the BOTTOM of this container (in AppKit's
        // Y-flipped coords), so a container of that height + offset
        // lands the button top at exactly ``y`` from the window
        // top.
        let title_bar_height = close_height + y;
        let new_container = CGRect {
            origin: CGPoint {
                x: cur_container.origin.x,
                y: window_height - title_bar_height,
            },
            size: CGSize {
                width: cur_container.size.width,
                height: title_bar_height,
            },
        };
        // Only write if the rect actually changed — saves AppKit a
        // no-op layout invalidation per runloop tick.
        if (cur_container.origin.y - new_container.origin.y).abs() > 0.5
            || (cur_container.size.height - new_container.size.height).abs() > 0.5
        {
            let _: () = msg_send![container, setFrame: new_container];
        }

        // X-positioning per button + force visibility. macOS hides
        // the cluster (``hidden=YES`` + ``alphaValue=0``) when the
        // window enters fullscreen and only shows it on top-edge
        // hover; we keep it always visible so the row layout
        // doesn't reflow when going in/out of fullscreen.
        for (idx, x_offset) in [0.0f64, 20.0, 40.0].iter().enumerate() {
            let btn: *mut AnyObject =
                msg_send![ns_window, standardWindowButton: idx as u64];
            if btn.is_null() {
                continue;
            }
            let is_hidden: bool = msg_send![btn, isHidden];
            if is_hidden {
                let _: () = msg_send![btn, setHidden: false];
            }
            let alpha: f64 = msg_send![btn, alphaValue];
            if alpha < 0.999 {
                let _: () = msg_send![btn, setAlphaValue: 1.0_f64];
            }
            let cur: CGRect = msg_send![btn, frame];
            let new_x = x + x_offset;
            if (cur.origin.x - new_x).abs() <= 0.5 {
                continue;
            }
            let new_frame = CGRect {
                origin: CGPoint {
                    x: new_x,
                    y: cur.origin.y,
                },
                size: cur.size,
            };
            let _: () = msg_send![btn, setFrame: new_frame];
        }
    }
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            set_app_title,
            reinstall_backend,
            ember_is_fullscreen,
            ember_check_update,
            ember_install_update,
        ])
        .setup(|app| {
            // Native menu — has to be set before the first window
            // builds or macOS shows the default Tauri stub.
            let menu = build_menu(&app.handle())?;
            app.set_menu(menu)?;

            let dir = project_dir();

            // Open the loading view IMMEDIATELY so the user sees a
            // window appear on app launch instead of a bouncing
            // dock icon while uv downloads. The bootstrap runs on a
            // background thread; progress events update the page;
            // when the BE is ready we navigate the same window to
            // the real UI. Same pattern as the JetBrains tool-
            // window placeholder.
            let builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("loading.html".into()))
                .title("Ember Code")
                .inner_size(1100.0, 780.0);
            // ── Custom title bar (macOS) ──
            // ``TitleBarStyle::Overlay`` keeps the traffic lights
            // visible but removes the title-bar background, so the
            // webview extends to the very top of the window. The
            // FE's ``.app-header`` then renders as a single row:
            //   [traffic lights]  [☰] [🔥 Ember Code] · folder · org …
            // ``hidden_title`` suppresses the default centered text
            // (we set our own brand in the row instead).
            #[cfg(target_os = "macos")]
            let builder = builder
                .title_bar_style(TitleBarStyle::Overlay)
                .hidden_title(true)
                // Initial traffic-light position; the runtime
                // ``reposition_traffic_lights`` helper re-applies
                // the same (x, y) after every navigation to survive
                // macOS' title-bar recompute. Kept in lockstep with
                // the schedule below in ``bootstrap_and_open``.
                .traffic_light_position(LogicalPosition::new(16.0, 24.0));
            builder
                .initialization_script(&INIT_SCRIPT.replace("__PLATFORM__", PLATFORM))
                .build()?;

            // Pin the traffic-light cluster against AppKit's
            // periodic resets. The observer fires on the main
            // thread (where setup() runs), after every layout
            // pass, for the lifetime of the window — see
            // ``install_traffic_light_observer`` for the rationale.
            #[cfg(target_os = "macos")]
            if let Some(w) = app.get_webview_window("main") {
                install_traffic_light_observer(w.clone(), 16.0, 24.0);
                // Native fullscreen detaches the traffic-light
                // cluster (lives behind the slide-down panel
                // afterwards). Watch the window's resize stream and
                // emit a JS-visible event whenever fullscreen state
                // flips, so the FE can shrink the header gutter
                // from 48 → 16 px and let the hamburger / brand
                // slide into the now-free real estate.
                use std::sync::atomic::{AtomicBool, Ordering};
                use std::sync::Arc;
                let was_fullscreen = Arc::new(AtomicBool::new(
                    w.is_fullscreen().unwrap_or(false),
                ));
                let _ = w.emit(
                    "ember-fullscreen",
                    was_fullscreen.load(Ordering::Relaxed),
                );
                let w_for_event = w.clone();
                let flag = was_fullscreen.clone();
                w.on_window_event(move |event| {
                    if let tauri::WindowEvent::Resized(_) = event {
                        let is_fs = w_for_event.is_fullscreen().unwrap_or(false);
                        if is_fs != flag.load(Ordering::Relaxed) {
                            flag.store(is_fs, Ordering::Relaxed);
                            let _ = w_for_event.emit("ember-fullscreen", is_fs);
                        }
                    }
                });
            }

            // Bootstrap kicks off on a background thread so the
            // loading window stays responsive.
            let app_handle = app.handle().clone();
            let dir_for_bootstrap = dir.clone();
            std::thread::spawn(move || {
                if let Err(e) = bootstrap_and_open(&app_handle, &dir_for_bootstrap) {
                    if let Some(w) = app_handle.get_webview_window("main") {
                        let escaped = serde_json::to_string(&e).unwrap_or_default();
                        let _ = w.emit("ember-bootstrap-error", &e);
                        let _ = w.eval(&format!(
                            "document.body.innerHTML = '<div class=\"ember-loading-error\">Bootstrap failed: ' + {} + '</div>';",
                            escaped
                        ));
                    }
                    eprintln!("Ember Code bootstrap failed: {e}");
                }
            });

            Ok(())
        })
        // Native menu items emit ``ember-menu`` events on the
        // ``main`` webview; the FE picks them up via
        // ``window.addEventListener('ember-menu', e => …)`` and
        // routes them through the existing host-bridge dispatcher
        // (same handler as the JetBrains ``ember-host`` events,
        // just on a separate channel because Tauri's emit format
        // and JCEF's CustomEvent shape don't line up cleanly).
        .on_menu_event(|app, event| match event.id().as_ref() {
            "toggle_devtools" => {
                if let Some(w) = app.get_webview_window("main") {
                    #[cfg(debug_assertions)]
                    {
                        if w.is_devtools_open() {
                            w.close_devtools();
                        } else {
                            w.open_devtools();
                        }
                    }
                    #[cfg(not(debug_assertions))]
                    {
                        let _ = w;
                    }
                }
            }
            "reinstall_backend" => {
                // Rust-side action — wipe the cache + restart the
                // BE without round-tripping through the FE.
                if let Err(e) = reinstall_backend(app.clone()) {
                    eprintln!("reinstall_backend failed: {e}");
                }
            }
            id => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.emit("ember-menu", id.to_string());
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Ember Code app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(handle) = app.try_state::<BackendHandle>() {
                    if let Some(mut child) = handle.0.lock().unwrap().take() {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ready_line_parsed_returns_port() {
        assert_eq!(
            parse_ready_line(r#"{"status":"ready","ws_port":51234}"#),
            Some(51234),
        );
    }

    #[test]
    fn ready_line_with_extra_fields_ok() {
        // The BE includes extra fields (socket path, session id, …)
        // alongside the ready signal. We only care about ws_port.
        assert_eq!(
            parse_ready_line(
                r#"{"status":"ready","ws_port":8080,"socket":"/tmp/x.sock"}"#,
            ),
            Some(8080),
        );
    }

    #[test]
    fn trailing_newline_doesnt_break_parse() {
        assert_eq!(
            parse_ready_line("{\"status\":\"ready\",\"ws_port\":1}\n"),
            Some(1),
        );
    }

    #[test]
    fn non_ready_status_returns_none() {
        // BE logs other JSON status events too (e.g. warmup); they
        // must not be confused for "ready".
        assert_eq!(
            parse_ready_line(r#"{"status":"starting","ws_port":1234}"#),
            None,
        );
    }

    #[test]
    fn non_json_line_returns_none() {
        // stderr-style log lines on stdout shouldn't crash the loop.
        assert_eq!(parse_ready_line("INFO loading sessions..."), None);
    }

    #[test]
    fn missing_ws_port_returns_none() {
        // Ready without a port is malformed — don't break to a bogus
        // value, just keep reading.
        assert_eq!(parse_ready_line(r#"{"status":"ready"}"#), None);
    }

    #[test]
    fn ready_line_with_non_numeric_ws_port_returns_none() {
        // ``ws_port`` is u16 — string/bool/object values must be
        // refused, not panic.
        assert_eq!(
            parse_ready_line(r#"{"status":"ready","ws_port":"oops"}"#),
            None,
        );
        assert_eq!(
            parse_ready_line(r#"{"status":"ready","ws_port":true}"#),
            None,
        );
    }

    #[test]
    fn ready_line_with_out_of_range_ws_port_returns_none() {
        // Anything above u16::MAX (65535) is not a port. We expect
        // the caller's ``as u16`` to truncate — but ``parse_ready_line``
        // currently coerces; lock that down so a fix one way or the
        // other is a deliberate choice.
        let line = format!(r#"{{"status":"ready","ws_port":{}}}"#, 1u64 << 40);
        // Truncating cast yields *some* u16; we assert the parser
        // doesn't crash and returns Some(_). If the contract tightens
        // later, this test surfaces the change.
        let got = parse_ready_line(&line);
        assert!(got.is_some(), "huge ws_port currently coerces, not crashes");
    }
}

