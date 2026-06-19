//! End-to-end spawn smoke for the Tauri shell.
//!
//! This integration test exercises ``lib.rs::run()`` as an actual
//! process: spawn the compiled binary, wait for it to reach a
//! "fully booted" state (BE child alive + WS port bound), then
//! gracefully shut it down and verify the parent-PID watchdog
//! takes the BE down with it.
//!
//! Why this test exists: the unit tests in ``lib.rs``/``runtime.rs``
//! cover pure helpers but never instantiate the Tauri runtime.
//! The Playwright suites exercise the shared web bundle against a
//! browser, not against Tauri's WKWebView + IPC bridge. This test
//! sits in the middle — no GUI assertions, but proves the *shell*
//! boots, spawns its BE, the BE binds a loopback port, and the
//! watchdog cleans up on parent exit. The most-likely regression
//! class ("a refactor in ``run()`` silently breaks startup") fails
//! here loudly.
//!
//! Gated behind ``EMBER_TAURI_SMOKE=1`` because it (a) takes
//! ~10-20s, (b) needs the debug binary + the project venv +
//! ``clients/web/dist`` to all be pre-built, and (c) actually
//! opens a webview window on macOS. CI sets the env var on the
//! macOS runner where those preconditions hold; local dev runs
//! ``cargo test`` without flipping it.
//!
//! Currently macOS-only (uses ``pgrep``/``lsof``). Linux support is
//! a small addition (same tools exist); Windows would need a
//! different process-tree probe.

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

/// Resolve the repo root by walking up from the manifest dir until
/// we see a ``pyproject.toml`` (the workspace marker).
fn repo_root() -> PathBuf {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut probe = manifest.clone();
    loop {
        if probe.join("pyproject.toml").is_file() {
            return probe;
        }
        if !probe.pop() {
            panic!(
                "couldn't find repo root (no pyproject.toml) starting from {}",
                manifest.display()
            );
        }
    }
}

/// Find child PIDs of ``parent_pid`` whose command line mentions
/// ``ember_code.backend``. Uses ``ps`` so we don't take any deps.
fn find_be_descendant(parent_pid: u32) -> Option<u32> {
    // ``-A`` lists every process; ``-o pid=,ppid=,command=`` strips
    // the header so each line is "pid ppid command...".
    let out = Command::new("ps")
        .args(["-A", "-o", "pid=,ppid=,command="])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    // BFS through the process tree — the BE may be a grandchild via
    // a shell wrapper. (In practice ``EMBER_DEV_BACKEND`` makes it a
    // direct child, but be defensive.)
    let mut want = vec![parent_pid];
    let mut seen: Vec<u32> = vec![parent_pid];
    while let Some(p) = want.pop() {
        for line in text.lines() {
            let mut parts = line.split_whitespace();
            let pid: u32 = match parts.next().and_then(|s| s.parse().ok()) {
                Some(x) => x,
                None => continue,
            };
            let ppid: u32 = match parts.next().and_then(|s| s.parse().ok()) {
                Some(x) => x,
                None => continue,
            };
            if ppid != p || seen.contains(&pid) {
                continue;
            }
            let rest = parts.collect::<Vec<_>>().join(" ");
            if rest.contains("ember_code.backend") {
                return Some(pid);
            }
            seen.push(pid);
            want.push(pid);
        }
    }
    None
}

/// Return ``Some(port)`` if the given pid is listening on a loopback
/// TCP port. ``lsof -p <pid> -P -n`` lists every FD owned by ``pid``;
/// we filter for ``LISTEN`` lines and look for the loopback address.
///
/// Why not ``-iTCP -sTCP:LISTEN``: those filters OR with ``-p`` on
/// macOS rather than AND, so the output includes every listening
/// process system-wide — defeats the purpose.
fn listening_port(pid: u32) -> Option<u16> {
    let out = Command::new("lsof")
        .args(["-p", &pid.to_string(), "-P", "-n"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    for line in String::from_utf8_lossy(&out.stdout).lines() {
        if !line.contains("(LISTEN)") {
            continue;
        }
        for tok in line.split_whitespace() {
            if let Some(rest) = tok.strip_prefix("127.0.0.1:") {
                if let Ok(port) = rest.parse::<u16>() {
                    return Some(port);
                }
            }
        }
    }
    None
}

/// Best-effort: send SIGTERM via ``kill``. Returns once ``kill``
/// returns; doesn't wait for the target to actually exit. Stderr is
/// silenced so the test output isn't polluted by "No such process"
/// when the target has already self-terminated (the parent-watchdog
/// happy path).
fn sigterm(pid: u32) {
    let _ = Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .stderr(Stdio::null())
        .status();
}

/// Wait up to ``timeout`` for ``pred`` to return ``Some(_)``.
fn poll_until<T, F: FnMut() -> Option<T>>(
    timeout: Duration,
    mut pred: F,
) -> Option<T> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if let Some(v) = pred() {
            return Some(v);
        }
        thread::sleep(Duration::from_millis(250));
    }
    None
}

/// Returns true if a process with ``pid`` exists. Uses ``kill -0``
/// which is the POSIX way to probe without sending a real signal.
/// Stderr is silenced so the "No such process" line for the
/// exited-pid happy path doesn't pollute test output.
fn pid_alive(pid: u32) -> bool {
    Command::new("kill")
        .args(["-0", &pid.to_string()])
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[test]
fn tauri_binary_boots_be_and_watchdog_cleans_up_on_exit() {
    if std::env::var("EMBER_TAURI_SMOKE").ok().as_deref() != Some("1") {
        eprintln!(
            "skip: set EMBER_TAURI_SMOKE=1 to run this test (needs \
             .venv, web/dist, and pops a webview window)"
        );
        return;
    }
    if !cfg!(target_os = "macos") {
        eprintln!("skip: tauri_binary_boots_be_and_watchdog_cleans_up_on_exit only supports macOS today");
        return;
    }

    let root = repo_root();
    let venv_python = root.join(".venv").join("bin").join("python");
    assert!(
        venv_python.is_file(),
        "expected venv python at {} — run `uv sync` from repo root first",
        venv_python.display()
    );
    let dist_index = root.join("clients/web/dist/index.html");
    assert!(
        dist_index.is_file(),
        "expected web bundle at {} — run `npm --prefix clients/web run build` first",
        dist_index.display()
    );

    let binary = root
        .join("clients/tauri/src-tauri/target/debug/ember-code-app");
    assert!(
        binary.is_file(),
        "expected debug binary at {} — run `cargo build` in src-tauri/ first",
        binary.display()
    );

    // Point at a tmp project dir so the test doesn't touch any
    // real on-disk state. The dir MUST exist before the BE boots —
    // it's where ``state.db`` (sessions, prefs, …) lives and the BE
    // fails-to-ready if the path doesn't resolve to a directory.
    let project_dir = std::env::temp_dir()
        .join(format!("ember-tauri-smoke-{}", std::process::id()));
    std::fs::create_dir_all(&project_dir).expect("create temp project dir");

    // Spawn the shell with the project venv as the BE python so
    // we skip the uv-download bootstrap (which would dominate the
    // test runtime + require network).
    let mut child: Child = Command::new(&binary)
        .env("EMBER_DEV_BACKEND", &venv_python)
        .env("EMBER_PROJECT_DIR", &project_dir)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn ember-code-app");
    let tauri_pid = child.id();

    // Use a guard so the binary is killed even if assertions panic.
    struct Killer<'a>(&'a mut Child, u32);
    impl Drop for Killer<'_> {
        fn drop(&mut self) {
            sigterm(self.1);
            let _ = self.0.wait();
        }
    }
    let guard = Killer(&mut child, tauri_pid);

    // ── Phase A: BE child appears in process tree ──
    let be_pid = poll_until(Duration::from_secs(30), || {
        find_be_descendant(tauri_pid)
    })
    .unwrap_or_else(|| {
        panic!(
            "Tauri (pid={tauri_pid}) never spawned an ember_code.backend \
             child within 30s — startup is broken"
        )
    });

    // ── Phase B: BE binds a loopback port ──
    let port = poll_until(Duration::from_secs(10), || listening_port(be_pid))
        .unwrap_or_else(|| {
            panic!(
                "BE (pid={be_pid}) is alive but never bound a loopback TCP \
                 port within 10s — transport never started"
            )
        });
    assert!(
        port > 1024,
        "BE bound a privileged port {port} — should auto-assign from the ephemeral range"
    );

    // ── Phase C: shut down, watchdog cleans up ──
    sigterm(tauri_pid);
    let _ = guard.0.wait();
    // Defuse the drop guard — we already waited.
    std::mem::forget(guard);

    // The EMBER_PARENT_PID watchdog inside the BE must notice its
    // parent (the Tauri shell) is gone and self-terminate. Default
    // watchdog interval is fast — 10s is generous.
    let exited = poll_until(Duration::from_secs(15), || {
        if pid_alive(be_pid) {
            None
        } else {
            Some(())
        }
    });
    assert!(
        exited.is_some(),
        "BE (pid={be_pid}) still alive 15s after Tauri shell exited — \
         parent-PID watchdog is broken"
    );
}
