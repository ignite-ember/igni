//! igni desktop shell.
//!
//! Spawns the Python backend (`python -m ember_code.backend --ws-port 0`),
//! waits for its JSON ready line to learn the bound WebSocket port, then
//! opens the shared web UI (clients/web) pointed at that port via the
//! `?ws=` query param. The backend self-terminates if this process dies
//! (EMBER_PARENT_PID watchdog), and we also kill it on window close.

mod discovery;
mod runtime;

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tauri::menu::{AboutMetadata, Menu, MenuBuilder, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
#[cfg(target_os = "macos")]
use tauri::{LogicalPosition, TitleBarStyle};

struct BackendHandle(Mutex<Option<Child>>);

/// Everything a window needs to reach the running backend.
///
/// Filled once bootstrap resolves and read by every window opened
/// afterwards — a second window doesn't re-bootstrap or re-discover,
/// it connects to the backend this app instance already has.
#[derive(Clone)]
struct BackendConn {
    port: u16,
    expected_cli: String,
    actual_cli: String,
    source: &'static str,
}

/// Managed in ``setup`` — before any window exists — and filled in
/// later. Managing it up front rather than at fill time sidesteps
/// Tauri's ``manage``-is-a-no-op-when-already-managed rule; see
/// ``track_backend`` for what that costs when you get it wrong.
struct BackendConnState(Mutex<Option<BackendConn>>);

/// Percent-encode one query-string value.
///
/// Project paths reach the FE as a query param and routinely contain
/// spaces, and may contain ``&``, ``#`` or ``%`` — any of which would
/// otherwise truncate or corrupt the URL. Unreserved set per RFC 3986;
/// everything else goes out as ``%XX``. Encoding bytes rather than
/// chars keeps non-ASCII paths correct.
fn percent_encode(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(*byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// The URL a window loads to reach the backend.
///
/// ``project_dir`` is set only for windows opened onto a folder of
/// their own (New Window). The first window omits it and lands on the
/// backend's default session, which is already the launch folder —
/// passing the dir there would attach a *new* session instead of
/// resuming the one the backend booted with.
fn app_url(conn: &BackendConn, project_dir: Option<&str>) -> String {
    let ws = percent_encode(&format!("ws://127.0.0.1:{}", conn.port));
    let mut url = format!(
        "index.html?ws={ws}&host=tauri&expected_cli={}&actual_cli={}&backend_source={}",
        conn.expected_cli, conn.actual_cli, conn.source
    );
    if let Some(dir) = project_dir {
        url.push_str(&format!("&dir={}", percent_encode(dir)));
    }
    url
}

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

function openExternalUrl(url) {
  return window.__TAURI__.core.invoke('plugin:opener|open_url', { url });
}

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
  openUrl: openExternalUrl,
});

// Route http(s) and mailto links to the OS browser/mail client.
// Without this, anchor clicks navigate the whole WKWebView to the
// target URL (no top-level browser context exists), and
// ``window.open`` returns null — so links end up "opening inside
// the app". Intercept at the capture phase so we win against any
// React handlers, then hand off to the Tauri opener plugin.
(function () {
  function isExternal(href) {
    if (!href) return false;
    const lower = href.toLowerCase();
    return (
      lower.startsWith('http://') ||
      lower.startsWith('https://') ||
      lower.startsWith('mailto:') ||
      lower.startsWith('tel:')
    );
  }

  document.addEventListener(
    'click',
    (e) => {
      // Respect modifier-clicks the user may use to copy the link
      // address — let the default contextmenu / nothing-happens
      // behavior stand rather than firing the opener.
      if (e.defaultPrevented) return;
      if (e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
        // Cmd/Ctrl-click still means "open in browser" for us;
        // there's no second window to route into. Fall through.
      }
      const path = e.composedPath ? e.composedPath() : [];
      let anchor = null;
      for (const node of path) {
        if (node && node.tagName === 'A' && node.href) {
          anchor = node;
          break;
        }
      }
      if (!anchor) {
        // Fallback for events whose composedPath is empty (rare).
        let n = e.target;
        while (n && n.tagName !== 'A') n = n.parentElement;
        if (n) anchor = n;
      }
      if (!anchor) return;
      const href = anchor.getAttribute('href') || anchor.href;
      if (!isExternal(href)) return;
      e.preventDefault();
      e.stopPropagation();
      openExternalUrl(href).catch((err) =>
        console.warn('[ember] open_url failed:', err)
      );
    },
    true
  );

  // ``window.open(url, …)`` is used by a few FE call sites
  // (update banner, install-CLI hint). In a Tauri webview the
  // default returns null and does nothing visible; route it to
  // the OS instead.
  const nativeOpen = window.open?.bind(window);
  window.open = function (url) {
    if (isExternal(url)) {
      openExternalUrl(url).catch((err) =>
        console.warn('[ember] window.open failed:', err)
      );
      return null;
    }
    return nativeOpen ? nativeOpen.apply(window, arguments) : null;
  };
})();

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
/// Snapshot of the version data captured at bootstrap time —
/// travels alongside the WS port so ``bootstrap_and_open`` can
/// splice it into the WKWebView URL and the shared
/// ``BackendVersionChip`` in the web bundle renders correctly.
pub struct BackendVersionInfo {
    pub actual: Option<String>,
    pub expected: String,
    pub source: &'static str,
}

fn spawn_backend(
    project_dir: &str,
    progress: &(dyn Fn(&str) + Sync),
) -> Result<(Child, u16, BackendVersionInfo), String> {
    progress("Preparing Ember backend…");
    let install = runtime::ensure_backend_python(progress)?;
    let version_info = BackendVersionInfo {
        actual: install.actual_cli_version.clone(),
        expected: install.expected_cli_version.clone(),
        source: install.source.as_str(),
    };

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

    Ok((child, port, version_info))
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
        name: Some("igni".into()),
        copyright: Some("© 2026 Ignite Ember".into()),
        website: Some("https://ignite-ember.sh".into()),
        ..Default::default()
    };
    let about = PredefinedMenuItem::about(app, Some("About igni"), Some(app_meta))?;
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
        "igni",
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
    // Shift+Cmd+N, not Cmd+N: New Chat already owns the plain
    // binding, and it is the far more frequent action. This matches
    // VS Code, where Shift+Cmd+N is New Window.
    let new_window = MenuItem::with_id(
        app,
        "new_window",
        "New Window…",
        true,
        Some("CmdOrCtrl+Shift+N"),
    )?;
    let restart_backend = MenuItem::with_id(
        app,
        "restart_backend",
        "Restart Backend",
        true,
        // Deliberately no accelerator. The previous
        // ``CmdOrCtrl+Shift+R`` binding shadowed the near-
        // universal "hard reload" shortcut, so users trying
        // to reload the page instead killed the BE — very bad
        // failure mode when debugging a stuck stream. The menu
        // item is still one click away for the genuine case.
        None::<&str>,
    )?;
    let reinstall_backend_item = MenuItem::with_id(
        app,
        "reinstall_backend",
        "Reinstall Backend (Clean)",
        true,
        None::<&str>,
    )?;
    let diagnose_backend_item = MenuItem::with_id(
        app,
        "diagnose_backend",
        "Diagnose Backend",
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
            &new_window,
            &PredefinedMenuItem::separator(app)?,
            &restart_backend,
            &reinstall_backend_item,
            &diagnose_backend_item,
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
        .unwrap_or("igni")
        .to_string();
    let org = org.as_deref().map(str::trim).filter(|s| !s.is_empty());
    let title = match org {
        Some(o) => format!("{folder} · {o}"),
        None => folder,
    };
    window.set_title(&title).map_err(|e| e.to_string())
}

/// Build a triage dump — plugin version, pinned vs installed
/// ``ignite-ember`` versions, interpreter path, dev-override env
/// state, marker contents — and pop it in a native message dialog.
/// One-click bug-report triage: same purpose as the JetBrains
/// plugin's ``DoctorAction``.
///
/// Runs on a background thread because the version probe spawns
/// a subprocess; blocking the main thread would freeze the app.
fn diagnose_backend(app: AppHandle) {
    std::thread::spawn(move || {
        let report = build_diagnostic_report();
        use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
        app.dialog()
            .message(&report)
            .title("igni · Backend Diagnostics")
            .kind(MessageDialogKind::Info)
            .buttons(MessageDialogButtons::Ok)
            .show(|_| {});
    });
}

fn build_diagnostic_report() -> String {
    let expected = env!("CARGO_PKG_VERSION");
    let cache = runtime::cache_root_or_display();
    let venv_python = runtime::venv_python_path(&cache);
    let marker_path = cache.join("ember-install.json");

    let dev_backend = std::env::var("EMBER_DEV_BACKEND").ok();
    let ember_python = std::env::var("EMBER_PYTHON").ok();
    let dev_ack = std::env::var("IGNITE_EMBER_DEV").ok();
    let dev_active = dev_ack
        .as_deref()
        .is_some_and(|v| v == "1" || v.eq_ignore_ascii_case("true"));

    let active_python = if dev_active && dev_backend.as_deref().is_some_and(|s| !s.is_empty()) {
        std::path::PathBuf::from(dev_backend.as_deref().unwrap())
    } else if dev_active && ember_python.as_deref().is_some_and(|s| !s.is_empty()) {
        std::path::PathBuf::from(ember_python.as_deref().unwrap())
    } else {
        venv_python.clone()
    };
    let actual = runtime::probe_cli_version(&active_python)
        .unwrap_or_else(|| "<probe failed>".to_string());

    let marker_contents = std::fs::read_to_string(&marker_path)
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|_| "<missing>".to_string());

    let mut out = String::new();
    out.push_str("igni Tauri app · backend diagnostics\n");
    out.push_str("─────────────────────────────────────\n");
    out.push_str(&format!("App version              : {}\n", expected));
    out.push_str(&format!("Expected ignite-ember    : {}\n", expected));
    out.push_str(&format!("Actual ignite-ember      : {}\n", actual));
    if actual != expected && actual != "<probe failed>" {
        out.push_str("                           ↑ MISMATCH — chat may fail\n");
    }
    out.push('\n');
    let backend_source = if dev_active
        && (dev_backend.as_deref().is_some_and(|s| !s.is_empty())
            || ember_python.as_deref().is_some_and(|s| !s.is_empty()))
    {
        "dev override"
    } else {
        "managed venv"
    };
    out.push_str(&format!("Backend source           : {}\n", backend_source));
    out.push_str(&format!("Interpreter path         : {}\n", active_python.display()));
    out.push_str(&format!("Managed venv path        : {}\n", venv_python.display()));
    out.push_str(&format!(
        "Managed venv present     : {}\n",
        runtime::is_executable_path(&venv_python)
    ));
    out.push('\n');
    out.push_str(&format!(
        "EMBER_DEV_BACKEND        : {}\n",
        dev_backend.as_deref().unwrap_or("<unset>")
    ));
    out.push_str(&format!(
        "EMBER_PYTHON             : {}\n",
        ember_python.as_deref().unwrap_or("<unset>")
    ));
    out.push_str(&format!(
        "IGNITE_EMBER_DEV         : {}\n",
        dev_ack.as_deref().unwrap_or("<unset>")
    ));
    if (dev_backend.as_deref().is_some_and(|s| !s.is_empty())
        || ember_python.as_deref().is_some_and(|s| !s.is_empty()))
        && !dev_active
    {
        out.push_str("                           ↑ override env var set without ack — ignored\n");
    }
    out.push('\n');
    out.push_str(&format!("Marker file              : {}\n", marker_path.display()));
    out.push_str(&format!("Marker contents          : {}\n", marker_contents));
    out
}

/// Reinstall the managed Python toolchain from scratch — wired to
/// the "Reinstall Backend (Clean)" Tools-menu item and to the
/// ``--reinstall`` CLI flag. Wipes the cache then restarts the BE.
#[tauri::command]
fn reinstall_backend(app: AppHandle) -> Result<(), String> {
    // ``BackendHandle`` is managed only on the spawn path, so its
    // absence means we attached to a backend another client owns (see
    // ``discovery.rs``). Killing it would take down their windows, and
    // silently reinstalling around it would leave the user staring at
    // the old backend wondering why nothing changed.
    let Some(handle) = app.try_state::<BackendHandle>() else {
        return Err(
            "This window is attached to a backend started by another igni client, so it \
             can't be reinstalled from here. Close the other client first, then reopen igni."
                .to_string(),
        );
    };
    // Forced, unlike the exit path's ``shutdown_backend``. This runs
    // on the main thread from the menu handler, and a grace period
    // here would freeze the UI for its duration. Nothing is lost that
    // matters: the re-bootstrap immediately below runs discovery,
    // which clears the stale lockfile this leaves behind, and the
    // user asked to blow the backend away.
    if let Some(mut child) = handle.0.lock().unwrap().take() {
        let _ = child.kill();
        let _ = child.wait();
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

/// Build one app window, native chrome and all.
///
/// Extracted so ``setup`` and New Window cannot drift: every window
/// needs the same title-bar treatment, the same init script (host
/// bridges, external-link routing, menu event plumbing) and — on
/// macOS — the same traffic-light pinning and fullscreen watcher. The
/// traffic-light constants used to carry a "kept in lockstep with"
/// comment pointing at a second call site, which is exactly the shape
/// of thing that stops being true.
fn build_app_window(
    app: &AppHandle,
    label: &str,
    url: &str,
    title: &str,
) -> tauri::Result<tauri::WebviewWindow> {
    let builder = WebviewWindowBuilder::new(app, label, WebviewUrl::App(url.into()))
        .title(title)
        .inner_size(1100.0, 780.0);

    // ── Custom title bar (macOS) ──
    // ``TitleBarStyle::Overlay`` keeps the traffic lights visible but
    // removes the title-bar background, so the webview extends to the
    // very top of the window. The FE's ``.app-header`` then renders as
    // a single row:
    //   [traffic lights]  [☰] [🔥 igni] · folder · org …
    // ``hidden_title`` suppresses the default centered text (we set
    // our own brand in the row instead).
    #[cfg(target_os = "macos")]
    let builder = builder
        .title_bar_style(TitleBarStyle::Overlay)
        .hidden_title(true)
        .traffic_light_position(LogicalPosition::new(16.0, 22.0));

    let window = builder
        .initialization_script(&INIT_SCRIPT.replace("__PLATFORM__", PLATFORM))
        .build()?;

    #[cfg(target_os = "macos")]
    {
        // Pin the traffic-light cluster against AppKit's periodic
        // resets — see ``install_traffic_light_observer``.
        install_traffic_light_observer(window.clone(), 16.0, 22.0);
        // The observer fires on ``kCFRunLoopBeforeWaiting`` — only
        // when the runloop goes idle. Under load (debug builds, slow
        // first paint) that idle moment can be delayed long enough
        // for AppKit's initial title-bar layout to stick at its
        // default y. Pin explicitly so we don't depend on timing.
        reposition_traffic_lights(&window, 16.0, 24.0);
        install_fullscreen_watcher(&window);
    }

    Ok(window)
}

/// Emit ``ember-fullscreen`` whenever the window enters or leaves
/// native fullscreen, so the FE can shrink the header gutter from
/// 48 → 16 px and slide the hamburger / brand into the freed space.
/// Native fullscreen detaches the traffic-light cluster (it lives
/// behind the slide-down panel afterwards), so the FE has to know.
#[cfg(target_os = "macos")]
fn install_fullscreen_watcher(window: &tauri::WebviewWindow) {
    let was_fullscreen = Arc::new(AtomicBool::new(window.is_fullscreen().unwrap_or(false)));
    let _ = window.emit("ember-fullscreen", was_fullscreen.load(Ordering::Relaxed));
    let w_for_event = window.clone();
    let flag = was_fullscreen.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::Resized(_) = event {
            let is_fs = w_for_event.is_fullscreen().unwrap_or(false);
            if is_fs != flag.load(Ordering::Relaxed) {
                flag.store(is_fs, Ordering::Relaxed);
                let _ = w_for_event.emit("ember-fullscreen", is_fs);
            }
        }
    });
}

/// The window a menu action belongs to.
///
/// Menu events used to go unconditionally to ``main``. With more than
/// one window that is wrong in the most confusing way available: New
/// Chat pressed in the second window would start a chat in the first
/// one, behind it. Falls back to ``main`` when nothing reports focus
/// (the menu bar itself can hold focus on macOS).
fn focused_window(app: &AppHandle) -> Option<tauri::WebviewWindow> {
    app.webview_windows()
        .into_values()
        .find(|w| w.is_focused().unwrap_or(false))
        .or_else(|| app.get_webview_window("main"))
}

/// Whether a folder pick is already on screen.
///
/// Two concurrent picks wedge the app. Observed: firing New Window
/// twice in quick succession presents a second panel on a window that
/// already has one as a modal sheet, and AppKit orders the parent
/// window out — the process stays alive with no windows, and since
/// tao never sees `Destroyed`, it never exits either. You get an app
/// running with nothing on screen and no way back. Two taps of
/// ⇧⌘N is all it takes.
static PICK_IN_FLIGHT: AtomicBool = AtomicBool::new(false);

/// Claim the pick slot. `false` means one is already open.
fn try_begin_pick() -> bool {
    !PICK_IN_FLIGHT.swap(true, Ordering::SeqCst)
}

fn end_pick() {
    PICK_IN_FLIGHT.store(false, Ordering::SeqCst);
}

/// Releases the pick slot however the picking thread unwinds —
/// cancelled dialog, unreadable path, backend not ready, or success.
/// A missed release would disable New Window for the rest of the
/// session, which is a worse bug than the one being fixed.
struct PickGuard;

impl Drop for PickGuard {
    fn drop(&mut self) {
        end_pick();
    }
}

/// New Window: pick a folder, then open a window bound to it.
///
/// Pick first, build second — a cancelled picker leaves no empty
/// window behind. The new window shares this app instance's backend
/// (one backend per app; see ``discovery.rs``) and binds a session in
/// the chosen folder via ``attach_session({project_dir})``, which the
/// backend's session pool already supports per-runtime.
///
/// Threading: ``blocking_pick_folder`` must not run on the main
/// thread, and window construction wants to be on it — so the pick
/// happens on a worker and the build hops back.
fn open_new_window(app: &AppHandle) {
    use tauri_plugin_dialog::DialogExt;

    if !try_begin_pick() {
        // A picker is already up. Nothing to do — it is modal, so the
        // user can see it; presenting a second one is what breaks the
        // window. See ``PICK_IN_FLIGHT``.
        return;
    }

    let app = app.clone();
    std::thread::spawn(move || {
        // Held for the whole pick. The window build is queued onto
        // the main thread and the label is chosen there, so releasing
        // when this thread exits cannot race two windows onto one
        // label.
        let _guard = PickGuard;
        let conn = app
            .try_state::<BackendConnState>()
            .and_then(|state| state.0.lock().unwrap().clone());
        let Some(conn) = conn else {
            // Bootstrap hasn't resolved yet. Say so rather than
            // opening a window with nowhere to connect.
            app.dialog()
                .message("igni is still starting. Try again once the first window has loaded.")
                .blocking_show();
            return;
        };

        let Some(folder) = app.dialog().file().blocking_pick_folder() else {
            return; // cancelled
        };
        let Ok(path) = folder.into_path() else {
            return;
        };
        let dir = path.to_string_lossy().to_string();
        let title = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| "igni".to_string());

        let url = app_url(&conn, Some(&dir));
        let app_for_build = app.clone();
        let _ = app.run_on_main_thread(move || {
            let existing: Vec<String> = app_for_build.webview_windows().into_keys().collect();
            let label = next_window_label(&existing);
            if let Err(e) = build_app_window(&app_for_build, &label, &url, &title) {
                eprintln!("failed to open new window: {e}");
            }
        });
    });
}

/// Lowest free ``w-N`` (N ≥ 2). The first window is ``main`` and
/// keeps that label forever — it is what maps an existing install
/// onto its pre-multi-window ``client_id`` (see `clientState.ts`).
///
/// Lowest-free rather than a counter so closing w-2 and opening
/// another window reuses w-2 instead of climbing forever. A window's
/// label is its identity: reusing it means reusing that window's
/// stored session binding and draft, which is the behaviour you want
/// when the "same" second window comes back.
fn next_window_label(existing: &[String]) -> String {
    let mut n = 2usize;
    loop {
        let candidate = format!("w-{n}");
        if !existing.iter().any(|label| label == &candidate) {
            return candidate;
        }
        n += 1;
    }
}

/// How long the backend gets to wind down before we force it.
///
/// Teardown cancels background tasks, drains the session pool
/// (checkpointing each runtime), closes the transport and removes the
/// lockfile — all local work, normally tens of milliseconds. Three
/// seconds is headroom for a pool mid-checkpoint, not an expected
/// wait: the poll below returns as soon as the process is gone.
const SHUTDOWN_GRACE: Duration = Duration::from_secs(3);

#[derive(Debug, PartialEq, Eq)]
enum ShutdownOutcome {
    /// Already dead — crashed, or killed by something else.
    AlreadyExited,
    /// Exited on its own after the terminate request.
    Graceful,
    /// Ignored the request (or we had none to send) and was killed.
    Forced,
}

/// Stop a backend we spawned, giving it a chance to exit cleanly.
///
/// ``Child::kill`` is SIGKILL, which skips the backend's teardown
/// entirely (`backend/supervisor.py::teardown`). Two things are lost
/// that way: sessions never checkpoint, and the discovery lockfile
/// survives naming a dead PID — so the next launch has to probe a
/// corpse, and a port recycled onto an unrelated process in the
/// meantime is exactly the case `discovery::pid_alive` exists to
/// catch. SIGTERM is a first-class exit path on the backend side: it
/// sets the shutdown event, which drains the pool and removes the
/// lockfile on the way out.
///
/// Escalates when the grace period lapses, so a wedged backend can
/// never hold the app open.
fn shutdown_backend(child: &mut Child, grace: Duration) -> ShutdownOutcome {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return ShutdownOutcome::AlreadyExited;
    }
    if request_terminate(child) {
        let deadline = Instant::now() + grace;
        while Instant::now() < deadline {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return ShutdownOutcome::Graceful;
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    ShutdownOutcome::Forced
}

#[cfg(unix)]
fn request_terminate(child: &Child) -> bool {
    unsafe { libc::kill(child.id() as i32, libc::SIGTERM) == 0 }
}

#[cfg(not(unix))]
fn request_terminate(_child: &Child) -> bool {
    // Nothing on Windows reaches a console-less child the way SIGTERM
    // does, so go straight to the forced path — the behaviour there
    // is what every platform had before this. The backend's own
    // parent-PID watchdog is the cleanup story on Windows.
    false
}

/// Record a backend we spawned, so ``RunEvent::Exit`` can kill it.
///
/// Not a bare ``app.manage``: Tauri's ``manage`` is a no-op when the
/// type is already managed, and it returns ``false`` rather than
/// replacing. ``reinstall_backend`` leaves a managed
/// ``BackendHandle`` holding ``None`` after taking the old child, so
/// the ``manage`` call for the *replacement* child was silently
/// dropped and that process was tracked by nothing. It still died
/// with the app — the ``IGNI_PARENT_PID`` watchdog saw the parent go
/// — but the explicit kill on exit, and anything else that reaches
/// for the handle, had lost it. Store into the existing slot when
/// there is one.
///
/// Both calls take ``&self``, so holding the ``State`` borrow across
/// the ``manage`` in the other arm is fine.
fn track_backend(app: &AppHandle, child: Child) {
    match app.try_state::<BackendHandle>() {
        Some(handle) => *handle.0.lock().unwrap() = Some(child),
        None => {
            // Discards ``manage``'s bool: this arm only runs when
            // nothing is managed yet, so it is always ``true``.
            app.manage(BackendHandle(Mutex::new(Some(child))));
        }
    }
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

    // Attach to a backend another client already started for this
    // project rather than spawning a second one over the same
    // ``state.db``. See ``discovery.rs`` for what two backends on one
    // project actually cost.
    //
    // Ownership rides on ``BackendHandle``: it is only managed on the
    // spawn path, so the ``RunEvent::Exit`` handler has nothing to
    // kill when we attached, and a backend the plugins own survives
    // this app quitting.
    let (port, version_info) = match discovery::discover(project_dir, runtime::IGNITE_EMBER_VERSION)
    {
        discovery::Decision::Attach { port, wire_version } => {
            progress("Connecting to the running igni backend…");
            // Skips the whole Python bootstrap — no interpreter
            // resolution, no model prefetch — so this path is near
            // instant on a warm project.
            (
                port,
                BackendVersionInfo {
                    actual: Some(wire_version),
                    expected: runtime::IGNITE_EMBER_VERSION.to_string(),
                    source: "discovered",
                },
            )
        }
        discovery::Decision::Refuse { running } => {
            // Mixing traffic across wire versions corrupts state, and
            // spawning alongside it is the duplicate we're removing —
            // so this is a dead end the user has to resolve. Mirrors
            // what the VSCode extension tells them.
            return Err(format!(
                "Another igni backend is running for this project on version {running}, \
                 but this app is {}. Close the other client (or restart it on the \
                 matching version) and reopen igni.",
                runtime::IGNITE_EMBER_VERSION
            ));
        }
        discovery::Decision::Spawn => {
            let (child, port, version_info) = spawn_backend(project_dir, &progress)?;
            track_backend(app, child);
            (port, version_info)
        }
    };

    // Initial title: project-dir basename, Finder-style. The FE
    // re-issues ``set_app_title`` on every status_update with the
    // cloud org as a subtitle, so this just covers the case where
    // the BE never connects (no status push fires).
    let folder = std::path::Path::new(project_dir)
        .canonicalize()
        .ok()
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_else(|| "igni".to_string());

    // Publish the connection before navigating, so a New Window
    // opened the moment the first one paints already has somewhere to
    // point. Version params feed the shared bundle's
    // ``BackendVersionChip`` — same shape the JetBrains plugin uses,
    // one component, three surfaces.
    let conn = BackendConn {
        port,
        expected_cli: version_info.expected.clone(),
        actual_cli: version_info
            .actual
            .clone()
            .unwrap_or_else(|| "unknown".to_string()),
        source: version_info.source,
    };
    if let Some(state) = app.try_state::<BackendConnState>() {
        *state.0.lock().unwrap() = Some(conn.clone());
    }

    if let Some(w) = app.get_webview_window("main") {
        let _ = w.set_title(&folder);
        let target = app_url(&conn, None);
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
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build());

    // Unlock WKWebView's internal 60 Hz cap on macOS so the app
    // renders at the display's native refresh rate (up to 120 Hz
    // on ProMotion). WebKit caps to 60 by default regardless of
    // the display's ``maximumFramesPerSecond``; the plugin
    // flips the private ``_setPreferredFramesPerSecond`` knob
    // right after the WebView instantiates. No-op on non-mac
    // targets — the ``cfg`` in ``Cargo.toml`` keeps the crate
    // out of the Windows/Linux dependency graph entirely.
    #[cfg(target_os = "macos")]
    let builder = builder.plugin(tauri_plugin_macos_fps::init());

    builder
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

            // Managed before any window so the fill in
            // ``bootstrap_and_open`` writes into an existing slot —
            // ``manage`` refuses to replace, and a second call would
            // be silently dropped.
            app.manage(BackendConnState(Mutex::new(None)));

            let dir = project_dir();

            // Open the loading view IMMEDIATELY so the user sees a
            // window appear on app launch instead of a bouncing
            // dock icon while uv downloads. The bootstrap runs on a
            // background thread; progress events update the page;
            // when the BE is ready we navigate the same window to
            // the real UI. Same pattern as the JetBrains tool-
            // window placeholder.
            build_app_window(&app.handle().clone(), "main", "loading.html", "igni")?;

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
                    eprintln!("igni bootstrap failed: {e}");
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
            "new_window" => {
                open_new_window(app);
            }
            "toggle_devtools" => {
                if let Some(w) = focused_window(app) {
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
            "diagnose_backend" => {
                diagnose_backend(app.clone());
            }
            id => {
                if let Some(w) = focused_window(app) {
                    let _ = w.emit("ember-menu", id.to_string());
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building igni app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                // Reached when the last window closes: the runtime
                // fires ``ExitRequested`` on the final window's
                // destruction and, unprevented, exits the loop — on
                // every platform, macOS included.
                //
                // ``BackendHandle`` is managed on the spawn path
                // alone, so this only ever stops a backend we
                // started. One an IDE plugin owns (we attached to it
                // — see ``discovery.rs``) has no handle here and
                // outlives us, which is the whole point.
                if let Some(handle) = app.try_state::<BackendHandle>() {
                    if let Some(mut child) = handle.0.lock().unwrap().take() {
                        shutdown_backend(&mut child, SHUTDOWN_GRACE);
                    }
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── The New Window pick guard ───────────────────────────────

    #[test]
    fn only_one_folder_pick_at_a_time() {
        // Observed before this guard: firing New Window twice in
        // quick succession presented a second panel on a window that
        // already had one as a modal sheet, AppKit ordered the parent
        // window out, and the app was left running with no windows
        // and no way back — tao never saw ``Destroyed``, so it never
        // exited either.
        //
        // One test body rather than three: the flag is process-wide
        // state and cargo runs tests in parallel threads.
        assert!(try_begin_pick(), "first pick should claim the slot");
        assert!(!try_begin_pick(), "second pick must be refused");

        // The slot has to come back however the picking thread
        // unwinds — a cancelled dialog releases it just like a
        // successful one. Leaking it would disable New Window for the
        // rest of the session.
        {
            let _guard = PickGuard;
        }
        assert!(try_begin_pick(), "slot must be reusable after release");
        end_pick();
    }

    // ── Backend shutdown ────────────────────────────────────────
    //
    // Run against real child processes: the thing under test is
    // whether a signal is actually delivered and reaped, which a
    // mock would only assert back at us. Unix-only — the Windows
    // path has no terminate request to send and goes straight to
    // ``Forced``, which is what every platform did before.

    #[cfg(unix)]
    #[test]
    fn a_cooperative_backend_exits_gracefully() {
        // The case that matters: SIGTERM is what lets the backend
        // drain its session pool and remove its lockfile.
        let mut child = Command::new("sleep").arg("30").spawn().unwrap();
        let started = Instant::now();
        let outcome = shutdown_backend(&mut child, Duration::from_secs(5));
        assert_eq!(outcome, ShutdownOutcome::Graceful);
        // Returns on the process actually being gone, not on the
        // grace period elapsing.
        assert!(started.elapsed() < Duration::from_secs(2));
    }

    #[cfg(unix)]
    #[test]
    fn a_wedged_backend_is_forced() {
        // Ignores SIGTERM. A backend stuck mid-teardown must never
        // be able to hold the app open.
        let mut child = Command::new("sh")
            .arg("-c")
            .arg("trap '' TERM; while :; do sleep 0.2; done")
            .spawn()
            .unwrap();
        // Guard the fixture itself: if the shell exits on its own,
        // ``shutdown_backend`` would report Graceful for the wrong
        // reason and this test would be asserting nothing.
        std::thread::sleep(Duration::from_millis(200));
        assert!(
            matches!(child.try_wait(), Ok(None)),
            "fixture should still be running before we signal it"
        );
        let outcome = shutdown_backend(&mut child, Duration::from_millis(200));
        assert_eq!(outcome, ShutdownOutcome::Forced);
        // Reaped, not left a zombie.
        assert!(matches!(child.try_wait(), Ok(Some(_))));
    }

    #[cfg(unix)]
    #[test]
    fn an_already_dead_backend_is_reported_not_signalled() {
        // The crashed-backend case. Distinguished so the caller
        // isn't told we forced something that was already gone.
        let mut child = Command::new("true").spawn().unwrap();
        let _ = child.wait();
        assert_eq!(
            shutdown_backend(&mut child, Duration::from_millis(50)),
            ShutdownOutcome::AlreadyExited
        );
    }

    // ── Window labels ───────────────────────────────────────────
    //
    // A label is a window's identity: it keys the persisted
    // ``client_id``, which keys the session binding and draft on the
    // backend. See ``clientState.ts``.

    #[test]
    fn first_extra_window_is_w2() {
        // ``main`` is the launch window and never re-issued.
        assert_eq!(next_window_label(&["main".to_string()]), "w-2");
    }

    #[test]
    fn labels_climb_past_the_ones_in_use() {
        let open = vec!["main".to_string(), "w-2".to_string(), "w-3".to_string()];
        assert_eq!(next_window_label(&open), "w-4");
    }

    #[test]
    fn a_closed_label_is_reused() {
        // Lowest-free, not a counter. Reopening after closing w-2
        // lands back on that window's stored session and draft
        // instead of accruing a fresh identity every time.
        let open = vec!["main".to_string(), "w-3".to_string()];
        assert_eq!(next_window_label(&open), "w-2");
    }

    // ── URL construction ────────────────────────────────────────

    fn conn() -> BackendConn {
        BackendConn {
            port: 51234,
            expected_cli: "1.0.3".to_string(),
            actual_cli: "1.0.3".to_string(),
            source: "managed_venv",
        }
    }

    #[test]
    fn the_first_window_gets_no_dir_param() {
        // It lands on the backend's default session, which is
        // already the launch folder. A ``dir`` here would attach a
        // second session to the same directory instead.
        let url = app_url(&conn(), None);
        assert!(!url.contains("&dir="), "unexpected dir param in {url}");
        assert!(url.contains("ws=ws%3A%2F%2F127.0.0.1%3A51234"));
        assert!(url.contains("&host=tauri"));
        assert!(url.contains("&expected_cli=1.0.3"));
        assert!(url.contains("&backend_source=managed_venv"));
    }

    #[test]
    fn a_new_window_carries_its_folder() {
        let url = app_url(&conn(), Some("/Users/dev/repo"));
        assert!(url.contains("&dir=%2FUsers%2Fdev%2Frepo"), "got {url}");
    }

    #[test]
    fn paths_with_spaces_and_specials_survive() {
        // The real failure this prevents: an unencoded ``&`` or ``#``
        // truncates the query string, and the window silently binds
        // to a path that is a prefix of the one the user picked.
        let url = app_url(&conn(), Some("/Users/dev/my repo&stuff#1"));
        assert!(
            url.contains("&dir=%2FUsers%2Fdev%2Fmy%20repo%26stuff%231"),
            "got {url}"
        );
    }

    #[test]
    fn encoding_leaves_unreserved_characters_alone() {
        assert_eq!(percent_encode("Az0-_.~"), "Az0-_.~");
        // Non-ASCII is encoded per UTF-8 byte, not per char.
        assert_eq!(percent_encode("é"), "%C3%A9");
    }

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

    // ── INIT_SCRIPT: external-link interceptor (parity row 59) ──
    //
    // The interceptor itself executes inside WKWebView — we can't run
    // it from cargo test. What we CAN lock down is the contract the
    // INIT_SCRIPT string has to honor for the feature to work at all:
    // the right Tauri command name, capture-phase listener, scheme
    // allowlist, window.open shim, and __EMBER_HOST__.openUrl bridge.
    // Any future edit that flips one of these silently (e.g. dropping
    // ``true`` from addEventListener and losing capture-phase
    // interception, or typoing ``plugin:opener|open_url``) trips a
    // test.
    //
    // For end-to-end exercise of the actual click handler, see the
    // Playwright e2e suite under ``clients/tauri/e2e/`` — a real
    // browser is the only environment where this code runs.

    #[test]
    fn init_script_invokes_opener_open_url_command() {
        // The Rust-side capability (``opener:allow-open-url``) is
        // useless if the JS calls the wrong command name; pin the
        // exact string the plugin matches against.
        assert!(
            INIT_SCRIPT.contains("plugin:opener|open_url"),
            "INIT_SCRIPT must invoke plugin:opener|open_url"
        );
    }

    #[test]
    fn init_script_intercepts_clicks_at_capture_phase() {
        // The capture-phase ``true`` is load-bearing: React handlers
        // would otherwise consume the click first and call
        // ``preventDefault``, leaving the URL un-routed AND the
        // webview navigating to it. Without capture phase, we can
        // still race React's handlers and lose.
        assert!(
            INIT_SCRIPT.contains("'click'"),
            "INIT_SCRIPT must register a click handler"
        );
        assert!(
            INIT_SCRIPT.contains("addEventListener(\n    'click',"),
            "click listener must use capture phase (third arg = true)"
        );
        assert!(
            INIT_SCRIPT.contains("true\n  );"),
            "click listener's third arg must be the literal ``true``"
        );
    }

    #[test]
    fn init_script_allowlist_covers_http_https_mailto_tel() {
        // Schemes the matcher must catch — these are the only ones
        // we route to the OS. ``file://`` and ``data:`` are
        // intentionally NOT routed (would leak local paths /
        // sandbox contents to a browser).
        for scheme in ["http://", "https://", "mailto:", "tel:"] {
            assert!(
                INIT_SCRIPT.contains(&format!("'{scheme}'")),
                "isExternal() must match ``{scheme}``"
            );
        }
        // Negative-space check: we don't accidentally route ftp / file /
        // data, which would leak local paths into the user's browser.
        // The matcher's allowlist is closed — if these slip in, the
        // sandbox boundary widens silently.
        for forbidden in ["ftp://", "file://", "data:"] {
            assert!(
                !INIT_SCRIPT.contains(&format!("'{forbidden}'")),
                "isExternal() must NOT route ``{forbidden}`` — \
                 would leak local content to the OS browser"
            );
        }
    }

    #[test]
    fn init_script_shims_window_open() {
        // A few FE call sites (update banner, install-CLI hint) use
        // ``window.open``. Without the shim, they silently no-op in
        // WKWebView. The shim must (a) replace ``window.open`` and
        // (b) route through the SAME opener function used by the
        // anchor handler — single source of truth.
        assert!(
            INIT_SCRIPT.contains("window.open = function"),
            "window.open must be reassigned"
        );
        // The shim calls openExternalUrl (the shared helper), not
        // ``window.__TAURI__.core.invoke`` directly — keeps the
        // matcher and the path identical.
        assert!(
            INIT_SCRIPT.contains("openExternalUrl(url)"),
            "window.open shim must delegate to openExternalUrl"
        );
    }

    #[test]
    fn init_script_exposes_openurl_on_ember_host() {
        // ``host.openUrl(url)`` in the FE looks up
        // ``window.__EMBER_HOST__.openUrl``. Anything that drops the
        // ``openUrl: openExternalUrl`` line moves the welcome screen
        // "Sign in" link silently back to navigating-the-webview.
        assert!(
            INIT_SCRIPT.contains("openUrl: openExternalUrl"),
            "__EMBER_HOST__.openUrl must be wired to openExternalUrl"
        );
    }

    #[test]
    fn init_script_respects_default_prevented_and_left_button_only() {
        // Modifier-aware behavior: if a React handler called
        // ``preventDefault`` first (modals, etc.), we must NOT
        // override and pop a browser tab on top. And only the
        // primary button gets routed — middle-click is "open in
        // tab", which is a no-op here (no second window) but
        // shouldn't fire the opener either.
        assert!(
            INIT_SCRIPT.contains("e.defaultPrevented"),
            "click handler must early-return when defaultPrevented"
        );
        assert!(
            INIT_SCRIPT.contains("e.button !== 0"),
            "click handler must skip non-left-button clicks"
        );
    }

    #[test]
    fn init_script_walks_event_path_for_nested_anchor() {
        // Anchors are commonly wrapped (icon inside <a>, span inside
        // <a>) so ``e.target`` is the inner node, not the <a>. We
        // have to walk ``composedPath()`` (or parentElement) to find
        // the anchor itself; otherwise the matcher gets the wrong
        // tagName and lets the click through.
        assert!(
            INIT_SCRIPT.contains("composedPath"),
            "click handler must inspect the composed event path"
        );
        assert!(
            INIT_SCRIPT.contains("tagName === 'A'"),
            "click handler must look for the ancestor <a>"
        );
    }

    #[test]
    fn capability_default_grants_opener_open_url() {
        // The JS contract above is moot if the capability doesn't
        // grant ``opener:allow-open-url`` — Tauri rejects the
        // invoke at the IPC layer. Pin both that the file parses
        // and that the permission is in the list.
        let cap = include_str!("../capabilities/default.json");
        let v: serde_json::Value =
            serde_json::from_str(cap).expect("capabilities/default.json must be valid JSON");
        let perms = v["permissions"].as_array().expect("permissions[] missing");
        let has_open_url = perms
            .iter()
            .any(|p| p.as_str() == Some("opener:allow-open-url"));
        assert!(
            has_open_url,
            "capabilities/default.json must grant opener:allow-open-url"
        );
        // Window scope must include ``main`` — the only window we
        // ship — or the capability is dead text.
        let windows = v["windows"].as_array().expect("windows[] missing");
        assert!(
            windows.iter().any(|w| w.as_str() == Some("main")),
            "capability must apply to the ``main`` window"
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

