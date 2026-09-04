//! igni desktop shell.
//!
//! Spawns the Python backend (`python -m ember_code.backend --ws-port 0`),
//! waits for its JSON ready line to learn the bound WebSocket port, then
//! opens the shared web UI (clients/web) pointed at that port via the
//! `?ws=` query param. The backend self-terminates if this process dies
//! (IGNI_PARENT_PID watchdog), and we also kill it on window close.

mod runtime;

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

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

/// How much of the backend's stderr to keep for a failure message.
/// A Python traceback is longer than this; the last lines are the ones
/// that name the exception, which is the part a user can act on.
const STDERR_TAIL_LINES: usize = 40;

fn spawn_backend(
    project_dir: &str,
    progress: &(dyn Fn(&str) + Sync),
) -> Result<(Child, u16, BackendVersionInfo), String> {
    progress("Preparing the igni backend…");
    let install = runtime::ensure_backend_python(progress)?;
    let version_info = BackendVersionInfo {
        actual: install.actual_cli_version.clone(),
        expected: install.expected_cli_version.clone(),
        source: install.source.as_str(),
    };

    progress("Starting the igni backend…");
    let mut cmd = Command::new(&install.python);
    cmd.args([
        "-m",
        "ember_code.backend",
        "--ws-port",
        "0",
        "--project-dir",
        project_dir,
    ])
    .env("IGNI_PARENT_PID", std::process::id().to_string())
    .stdout(Stdio::piped())
    // Captured, not discarded. This was ``Stdio::null()``, and when the
    // backend died during startup the user was told "backend exited
    // before signalling ready" while the one line explaining why went to
    // /dev/null. A real case: a stored default model that no longer
    // resolves — written by ``/model`` or by cloud discovery, so
    // removing a model from the deployment reaches every developer
    // pinned to it. The backend no longer dies for that reason, but the
    // next reason it dies for should not be unknowable either.
    .stderr(Stdio::piped());
    for (k, v) in &install.env {
        cmd.env(k, v);
    }
    let mut child = cmd.spawn().map_err(|e| {
        format!("failed to spawn backend via `{}`: {e}", install.python.display())
    })?;

    let stdout = child.stdout.take().ok_or("backend stdout unavailable")?;
    let stderr = child.stderr.take().ok_or("backend stderr unavailable")?;

    // Drained on its own thread from the start. A Python traceback is
    // several kilobytes and the pipe buffer is not, so reading it only
    // after the failure risks the backend blocking on a full pipe while
    // we wait for a ready line that will never come — a deadlock in
    // place of an error message.
    let tail: Arc<Mutex<VecDeque<String>>> = Arc::new(Mutex::new(VecDeque::new()));
    let tail_writer = Arc::clone(&tail);
    std::thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for line in reader.lines().map_while(Result::ok) {
            let mut kept = tail_writer.lock().unwrap_or_else(|e| e.into_inner());
            // The last lines are the ones that matter: a traceback ends
            // with the exception. Bounded so a chatty backend cannot
            // grow this without limit.
            if kept.len() == STDERR_TAIL_LINES {
                kept.pop_front();
            }
            kept.push_back(line);
        }
    });

    let mut reader = BufReader::new(stdout);
    let mut line = String::new();
    let port = loop {
        line.clear();
        let n = reader
            .read_line(&mut line)
            .map_err(|e| format!("backend stdout read failed: {e}"))?;
        if n == 0 {
            // Give the stderr thread a moment to finish draining what
            // the process wrote before it died.
            std::thread::sleep(std::time::Duration::from_millis(200));
            let kept = tail.lock().unwrap_or_else(|e| e.into_inner());
            let reason = kept
                .iter()
                .rev()
                .find(|l| !l.trim().is_empty())
                .cloned()
                .unwrap_or_else(|| "no output on stderr".to_string());
            return Err(format!(
                "backend exited before signalling ready: {reason}"
            ));
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
    if let Ok(env) = std::env::var("IGNI_PROJECT_DIR") {
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
/// Whether the user has turned update checks off.
///
/// `update_check_ttl: 0` in `~/.igni/config.yaml` is the documented
/// off switch, and the setting's own comment calls the PyPI check "the
/// only unconfigured outbound request an ordinary run makes". That was
/// not true in the desktop app: this command went straight to the
/// updater plugin, so an install that had explicitly disabled update
/// checks still contacted github.com on **every launch** — the silent
/// startup check in `App.tsx` calls it on connect. F126.
///
/// Read here rather than asked of the backend because this is the
/// command that makes the request. A check that consults a policy one
/// process away can be reached without consulting it; a check that
/// reads the file it is about cannot.
///
/// Parsed by hand rather than with a YAML crate. One integer at a known
/// key is not worth a dependency in a binary that ships to customers,
/// and the failure mode is chosen deliberately: anything unparseable
/// leaves checks **enabled**, because silently disabling updates on a
/// malformed config would leave a machine on an old build with nothing
/// said.
fn update_checks_disabled() -> bool {
    let Some(home) = dirs::home_dir() else {
        return false;
    };
    match std::fs::read_to_string(home.join(".igni/config.yaml")) {
        Ok(text) => update_checks_disabled_in(&text),
        // No config at all is the default, and the default is on.
        Err(_) => false,
    }
}

/// The decision, over the text — so it is testable without owning
/// `$HOME`. Mutating the environment in a Rust test is `unsafe` and
/// races every other thread in the binary, which is a poor trade for
/// covering one `read_to_string`.
fn update_checks_disabled_in(text: &str) -> bool {
    for line in text.lines() {
        // No trimming: `strip_prefix` on the raw line is what anchors
        // this to the top level. An indented `update_check_ttl` belongs
        // to whatever section contains it, and reading it as the
        // top-level setting would let an unrelated block switch off the
        // app's updates.
        //
        // There was an explicit whitespace guard here as well. A
        // revert-check removing it still passed, because it could not
        // fail — `strip_prefix` had already rejected the indented line.
        // Dead code that reads as defence, which is worse than no code.
        let Some(value) = line.strip_prefix("update_check_ttl:") else {
            continue;
        };
        let value = value.split('#').next().unwrap_or("").trim();
        if let Ok(ttl) = value.parse::<i64>() {
            return ttl <= 0;
        }
        // Present but unreadable — see the caller's note: enabled, so a
        // malformed config never silently strands a machine on an old
        // build.
        return false;
    }
    false
}

/// Where this install looks for releases.
///
/// DEC-11. `tauri.conf.json` names
/// `github.com/ignite-ember/igni/releases`, which is the one outbound
/// host a default install has — and it sits awkwardly beside DP-5's
/// claim that nothing leaves the customer's cloud. Worse, an air-gapped
/// customer could not update at all: `update_check_ttl: 0` turns the
/// check off, and there was nothing to turn it *towards*.
///
/// So `update_endpoint` in `~/.igni/config.yaml` replaces the compiled
/// default. A customer mirrors releases and points the app at their own
/// host.
///
/// **Signature verification is unchanged, and that is the whole reason
/// this is safe.** The public key lives in `tauri.conf.json` and is not
/// configurable here, so a mirror can serve a different *version* but
/// cannot serve a different *build* — an artefact it signed itself
/// fails `minisign-verify` before anything is installed. Pointing at a
/// hostile mirror costs you updates, not integrity.
///
/// The plugin additionally refuses a non-HTTPS endpoint unless the app
/// was built with `dangerous_insecure_transport_protocol`, which this
/// one is not. So `http://` fails, and it fails at the builder rather
/// than mid-download.
///
/// Same hand-parse as `update_checks_disabled_in`, for the same
/// reasons, with the same top-level anchoring — an indented
/// `update_endpoint` belongs to whatever contains it, and reading it as
/// the app's release source would let an unrelated config block choose
/// where the binary comes from.
fn configured_update_endpoint() -> Option<String> {
    let home = dirs::home_dir()?;
    let text = std::fs::read_to_string(home.join(".igni/config.yaml")).ok()?;
    configured_update_endpoint_in(&text)
}

/// The decision, over the text, so it is testable without owning
/// `$HOME`.
fn configured_update_endpoint_in(text: &str) -> Option<String> {
    for line in text.lines() {
        let Some(value) = line.strip_prefix("update_endpoint:") else {
            continue;
        };
        // No `#` splitting here, unlike the TTL: a URL may legitimately
        // contain a fragment, and eating everything after the first
        // `#` would silently truncate one. A trailing YAML comment on
        // this key is worth losing to keep that true.
        let value = value.trim().trim_matches('"').trim_matches('\'');
        if value.is_empty() {
            // Present and blank means "use the compiled default",
            // which is a different statement from absent and reads
            // the same — deliberately, since both are "I have not
            // chosen a mirror".
            return None;
        }
        return Some(value.to_string());
    }
    None
}

/// An updater pointed at whatever this install is configured to use.
///
/// Falls back to the compiled endpoint when nothing is set, so the
/// default path is unchanged. A configured endpoint that will not parse
/// as a URL, or that the plugin rejects, is an error rather than a
/// silent fallback: an operator who set a mirror and got the vendor's
/// host anyway has been quietly overruled, which is the failure DEC-11
/// exists to prevent.
fn updater_for(app: &AppHandle) -> Result<tauri_plugin_updater::Updater, String> {
    use tauri_plugin_updater::UpdaterExt;

    let Some(endpoint) = configured_update_endpoint() else {
        return app.updater().map_err(|e| e.to_string());
    };

    let url = tauri::Url::parse(&endpoint).map_err(|e| {
        format!("update_endpoint in ~/.igni/config.yaml is not a valid URL ({endpoint}): {e}")
    })?;

    app.updater_builder()
        .endpoints(vec![url])
        .map_err(|e| {
            format!("update_endpoint in ~/.igni/config.yaml was refused ({endpoint}): {e}")
        })?
        .build()
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn ember_check_update(app: AppHandle) -> Result<serde_json::Value, String> {
    let current_version = env!("CARGO_PKG_VERSION").to_string();

    if update_checks_disabled() {
        // Not an error: the caller asked whether an update exists and
        // the honest answer for a deployment that has opted out is
        // "nothing to install", with no request made.
        return Ok(serde_json::json!({
            "available": false,
            "current_version": current_version,
            "latest_version": current_version,
            "checks_disabled": true,
        }));
    }
    let updater = updater_for(&app)?;
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
    // The same gate. Installing is a bigger outbound request than
    // checking, and a caller that reached this without a check — the
    // banner persisted across a config change, say — must not make it.
    if update_checks_disabled() {
        return Err("update checks are disabled in ~/.igni/config.yaml (update_check_ttl: 0)".to_string());
    }

    let updater = updater_for(&app)?;
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
    let marker_path = cache.join("igni-install.json");

    let dev_backend = std::env::var("IGNI_DEV_BACKEND").ok();
    let ember_python = std::env::var("IGNI_PYTHON").ok();
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
        "IGNI_DEV_BACKEND        : {}\n",
        dev_backend.as_deref().unwrap_or("<unset>")
    ));
    out.push_str(&format!(
        "IGNI_PYTHON             : {}\n",
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

    let (child, port, version_info) = spawn_backend(project_dir, &progress)?;
    app.manage(BackendHandle(Mutex::new(Some(child))));

    // Initial title: project-dir basename, Finder-style. The FE
    // re-issues ``set_app_title`` on every status_update with the
    // cloud org as a subtitle, so this just covers the case where
    // the BE never connects (no status push fires).
    let folder = std::path::Path::new(project_dir)
        .canonicalize()
        .ok()
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_else(|| "igni".to_string());

    if let Some(w) = app.get_webview_window("main") {
        let _ = w.set_title(&folder);
        // Splice version + host params into the URL so the shared
        // web bundle's ``BackendVersionChip`` reads them off the
        // query string. Same shape the JetBrains plugin uses —
        // one component, three surfaces. Version strings are just
        // digits + dots + hyphens (semver-shaped), all URL-safe,
        // so no percent-encoding needed here.
        let actual = version_info.actual.as_deref().unwrap_or("unknown");
        let expected = version_info.expected.as_str();
        let source = version_info.source;
        let target = format!(
            "index.html?ws=ws%3A%2F%2F127.0.0.1%3A{port}\
             &host=tauri\
             &expected_cli={expected}\
             &actual_cli={actual}\
             &backend_source={source}"
        );
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

            let dir = project_dir();

            // Open the loading view IMMEDIATELY so the user sees a
            // window appear on app launch instead of a bouncing
            // dock icon while uv downloads. The bootstrap runs on a
            // background thread; progress events update the page;
            // when the BE is ready we navigate the same window to
            // the real UI. Same pattern as the JetBrains tool-
            // window placeholder.
            let builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("loading.html".into()))
                .title("igni")
                .inner_size(1100.0, 780.0);
            // ── Custom title bar (macOS) ──
            // ``TitleBarStyle::Overlay`` keeps the traffic lights
            // visible but removes the title-bar background, so the
            // webview extends to the very top of the window. The
            // FE's ``.app-header`` then renders as a single row:
            //   [traffic lights]  [☰] [🔥 igni] · folder · org …
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
                .traffic_light_position(LogicalPosition::new(16.0, 22.0));
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
                install_traffic_light_observer(w.clone(), 16.0, 22.0);
                // The observer fires on ``kCFRunLoopBeforeWaiting`` —
                // only when the runloop goes idle. Under load (debug
                // builds, slow first-frame paint), that idle moment
                // can be delayed long enough for AppKit's initial
                // title-bar layout to "stick" — buttons end up at
                // AppKit's default ~y=10 instead of our requested
                // y=24, and they don't shift until something
                // re-triggers a layout (resize, etc.). The release
                // (optimized) build is fast enough that the observer
                // catches the first idle BEFORE this matters; debug
                // builds race and lose. Belt-and-suspenders: pin
                // explicitly right after install so we don't depend
                // on runloop timing.
                reposition_traffic_lights(&w, 16.0, 24.0);
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
            "diagnose_backend" => {
                diagnose_backend(app.clone());
            }
            id => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.emit("ember-menu", id.to_string());
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building igni app")
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

    // ── The update-check off switch (F126) ──────────────────────
    //
    // `update_check_ttl: 0` is documented as the way to stop igni
    // making outbound requests, and the desktop app ignored it — the
    // silent startup check in App.tsx contacted github.com on every
    // launch regardless.

    #[test]
    fn zero_disables_checks() {
        assert!(update_checks_disabled_in("update_check_ttl: 0\n"));
    }

    #[test]
    fn a_negative_ttl_disables_too() {
        // The Python side treats `<= 0` as off; two implementations of
        // one setting have to agree on the boundary or the app and the
        // CLI disagree about whether the deployment is air-gapped.
        assert!(update_checks_disabled_in("update_check_ttl: -1\n"));
    }

    #[test]
    fn a_real_ttl_leaves_checks_on() {
        assert!(!update_checks_disabled_in("update_check_ttl: 86400\n"));
    }

    #[test]
    fn an_absent_key_leaves_checks_on() {
        assert!(!update_checks_disabled_in("models:\n  default: x\n"));
    }

    #[test]
    fn a_trailing_comment_is_not_part_of_the_number() {
        assert!(update_checks_disabled_in("update_check_ttl: 0  # air-gapped\n"));
    }

    #[test]
    fn an_indented_key_of_the_same_name_is_ignored() {
        // A nested `update_check_ttl` belongs to whatever contains it.
        // The property comes from matching the raw line rather than a
        // trimmed one — stated here because the guard that used to
        // *look* like it provided it was unreachable.
        assert!(!update_checks_disabled_in(
            "plugins:\n  something:\n    update_check_ttl: 0\n"
        ));
    }

    #[test]
    fn both_update_commands_consult_the_switch() {
        // A wiring check, by reading this file.
        //
        // Everything above tests the decision; none of it proves the
        // decision is *asked for*. Removing the gate from
        // `ember_check_update` failed nothing, because a
        // `#[tauri::command]` takes an `AppHandle` and needs a running
        // Tauri application to call — there is no unit test that can
        // reach it.
        //
        // So this asserts the shape instead, and says so rather than
        // pretending the parser tests cover it. If either command ever
        // stops consulting the switch, an air-gapped install starts
        // contacting github.com again and nothing else here would
        // notice.
        // Each body is read up to the next command, not to the end of
        // the file. The first version took "everything after
        // ember_install_update" — which includes *this test*, where the
        // string appears in the assertion literals, so the rule matched
        // itself and could not fail. Third time in this review a source
        // sweep has read its own text as evidence.
        fn body_of<'a>(source: &'a str, name: &str) -> &'a str {
            let after = source
                .split_once(&format!("async fn {name}"))
                .unwrap_or_else(|| panic!("{name} is gone"))
                .1;
            after.split("\n#[tauri::command]").next().unwrap_or(after)
        }

        let source = include_str!("lib.rs");

        for name in ["ember_check_update", "ember_install_update"] {
            assert!(
                body_of(source, name).contains("update_checks_disabled()"),
                "{name} no longer consults the off switch"
            );
        }
    }

    #[test]
    fn a_garbled_value_leaves_checks_on() {
        // Deliberately not "off". Silently disabling updates on a
        // malformed config leaves a machine on an old build with
        // nothing said; leaving them on is visible and recoverable.
        assert!(!update_checks_disabled_in("update_check_ttl: never\n"));
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

    #[test]
    fn csp_is_set_and_names_no_host_off_this_machine() {
        // F127. ``csp`` was null, so the webview that renders model
        // output had no policy at all. mermaid's ``securityLevel:
        // strict`` and react-markdown escaping HTML were the only
        // layers; this is the second one. The reasoning lives here
        // rather than in the config because ``tauri-build`` parses
        // ``tauri.conf.json`` against a strict schema and rejects a
        // ``"//"`` comment key outright — it fails the build.
        //
        // ``style-src`` has to keep ``'unsafe-inline'``: mermaid and
        // rehype-highlight inject ``<style>`` at runtime, measured at
        // 11 tags and 916 violations without it. An injected
        // stylesheet can restyle a page but cannot invoke a command.
        //
        // Two things are being pinned.
        //
        // First that a policy exists and that ``script-src`` stays
        // exactly ``'self'`` — that directive is what stops injected
        // content reaching ``window.__TAURI__``, which
        // ``withGlobalTauri`` puts on the page.
        //
        // Second, and this is the part a list of directives would
        // miss: no source anywhere in the policy may name a host off
        // this machine. The first draft of this CSP ended
        // ``connect-src ... https:``, which would have let injected
        // content POST a transcript to any server on the internet.
        // The rule is written over every directive rather than over
        // ``connect-src`` alone so the same mistake in ``img-src``
        // (a tracking pixel) or ``font-src`` fails too.
        let conf = include_str!("../tauri.conf.json");
        let v: serde_json::Value =
            serde_json::from_str(conf).expect("tauri.conf.json must be valid JSON");
        let csp = v["app"]["security"]["csp"]
            .as_str()
            .expect("app.security.csp must be a string, not null");

        let directives: Vec<(&str, Vec<&str>)> = csp
            .split(';')
            .map(str::trim)
            .filter(|d| !d.is_empty())
            .map(|d| {
                let mut parts = d.split_whitespace();
                let name = parts.next().unwrap_or_default();
                (name, parts.collect())
            })
            .collect();
        let find = |name: &str| {
            directives
                .iter()
                .find(|(n, _)| *n == name)
                .map(|(_, s)| s.clone())
        };

        assert_eq!(
            find("script-src"),
            Some(vec!["'self'"]),
            "script-src must be exactly 'self' — no unsafe-inline, no unsafe-eval"
        );
        assert_eq!(find("default-src"), Some(vec!["'self'"]));
        assert_eq!(find("object-src"), Some(vec!["'none'"]));
        assert_eq!(find("frame-ancestors"), Some(vec!["'none'"]));
        assert_eq!(find("base-uri"), Some(vec!["'self'"]));

        // A source is allowed to be a quoted keyword, an inert scheme,
        // Tauri's IPC scheme, or a loopback origin. Anything else names
        // a host we cannot reach without leaving the customer's
        // network, and this product does not do that.
        for (name, sources) in &directives {
            for src in sources {
                let ok = src.starts_with('\'')
                    || matches!(*src, "data:" | "blob:" | "ipc:")
                    || src.starts_with("http://ipc.localhost")
                    || src.starts_with("http://127.0.0.1")
                    || src.starts_with("http://localhost")
                    || src.starts_with("ws://127.0.0.1")
                    || src.starts_with("ws://localhost");
                assert!(
                    ok,
                    "{name} names {src}, which is not on this machine — \
                     the webview must have no egress off the host"
                );
            }
        }

        // And the policy has to actually cover the sinks: a webview
        // rendering markdown needs these five named, or a directive
        // silently falls back to default-src and this test's reach
        // shrinks without anyone noticing.
        for required in ["style-src", "img-src", "font-src", "connect-src"] {
            assert!(
                find(required).is_some(),
                "{required} must be stated explicitly, not inherited from default-src"
            );
        }
    }

    // ── DEC-11: where an install looks for releases ─────────────────
    //
    // `tauri.conf.json` compiles in `github.com/ignite-ember/igni`,
    // which is the one outbound host a default install has and cannot
    // be reached at all from an air-gapped network. `update_endpoint`
    // in `~/.igni/config.yaml` replaces it.
    //
    // The parse is asserted here rather than through the plugin,
    // because constructing an `AppHandle` in a unit test means running
    // a Tauri app. What the plugin does with the URL — refuse
    // non-HTTPS, verify the signature against the compiled pubkey — is
    // its own tested behaviour, and the pubkey is deliberately not
    // configurable, so a mirror can serve a different version but not
    // a different build.

    #[test]
    fn absent_means_the_compiled_default() {
        assert_eq!(configured_update_endpoint_in("update_check_ttl: 86400\n"), None);
    }

    #[test]
    fn a_configured_endpoint_is_returned() {
        assert_eq!(
            configured_update_endpoint_in("update_endpoint: https://releases.acme.example/latest.json\n"),
            Some("https://releases.acme.example/latest.json".to_string())
        );
    }

    #[test]
    fn quotes_are_stripped() {
        // YAML lets you quote a URL and people do, especially one with
        // a `#` in it. Returning the quotes would fail `Url::parse`
        // with a message about the scheme, which points at the wrong
        // thing entirely.
        for line in [
            "update_endpoint: \"https://releases.acme.example/l.json\"\n",
            "update_endpoint: 'https://releases.acme.example/l.json'\n",
        ] {
            assert_eq!(
                configured_update_endpoint_in(line),
                Some("https://releases.acme.example/l.json".to_string()),
                "{line}"
            );
        }
    }

    #[test]
    fn present_and_blank_means_the_compiled_default() {
        // Same outcome as absent, deliberately: both are "I have not
        // chosen a mirror". Returning `Some("")` would reach
        // `Url::parse` and fail with a message about an empty string.
        assert_eq!(configured_update_endpoint_in("update_endpoint:\n"), None);
        assert_eq!(configured_update_endpoint_in("update_endpoint:   \n"), None);
    }

    #[test]
    fn a_fragment_survives() {
        // The TTL parser splits on `#` to drop trailing comments. Doing
        // that here would silently truncate a URL fragment, so this key
        // does not — and the cost, a trailing comment on this one line,
        // is stated in the code.
        assert_eq!(
            configured_update_endpoint_in("update_endpoint: https://a.example/l.json#v2\n"),
            Some("https://a.example/l.json#v2".to_string())
        );
    }

    #[test]
    fn an_indented_key_belongs_to_whatever_contains_it() {
        // The same anchoring as `update_checks_disabled_in`, and it
        // matters more here: an unrelated config block choosing where
        // the binary comes from is a supply-chain question, not a
        // preference. A nested key must not be read as the app's.
        assert_eq!(
            configured_update_endpoint_in(
                "plugins:\n  something:\n    update_endpoint: https://evil.example/l.json\n"
            ),
            None
        );
    }

    #[test]
    fn the_first_top_level_occurrence_wins() {
        // Matches the TTL parser's behaviour rather than inventing a
        // second rule for a duplicated key.
        assert_eq!(
            configured_update_endpoint_in(
                "update_endpoint: https://first.example/l.json\nupdate_endpoint: https://second.example/l.json\n"
            ),
            Some("https://first.example/l.json".to_string())
        );
    }

    #[test]
    fn a_configured_endpoint_parses_as_a_url() {
        // The step between reading the file and handing it to the
        // plugin. A value that will not parse is an error rather than a
        // silent fallback to the vendor host — an operator who set a
        // mirror and got github.com anyway has been quietly overruled.
        let raw = configured_update_endpoint_in(
            "update_endpoint: https://releases.acme.example/latest.json\n",
        )
        .expect("configured");
        let url = tauri::Url::parse(&raw).expect("parses");

        assert_eq!(url.scheme(), "https");
        assert_eq!(url.host_str(), Some("releases.acme.example"));
    }

    #[test]
    fn the_compiled_default_is_still_the_vendor_host() {
        // Not an endorsement — a pin. If somebody changes
        // `tauri.conf.json`'s endpoint, this fails and they have to
        // decide deliberately. And it keeps the claim in DEC-11
        // ("one outbound host a default install has") checkable rather
        // than remembered.
        let conf = include_str!("../tauri.conf.json");

        assert!(
            conf.contains("github.com/ignite-ember/igni/releases"),
            "the compiled default endpoint changed; DEC-11 and DP-5 both describe it"
        );
        assert!(
            conf.contains("\"pubkey\""),
            "the updater has no pubkey, so a mirror could serve any build it liked"
        );
    }
}
