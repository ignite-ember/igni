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
//! Marked ``#[ignore]`` because it (a) takes ~10-20s, (b) needs the
//! debug binary + the project venv + ``clients/web/dist`` to all be
//! pre-built, and (c) actually opens a webview window on macOS. Run it
//! with ``cargo test -- --ignored``.
//!
//! CI runs it: the ``tauri-smoke`` job in ``ci.yml`` builds the web
//! bundle and the venv, then ``xvfb-run cargo test -- --ignored`` on
//! Linux, where WebKitGTK gets a virtual display. For a long time
//! nothing ran it at all while this paragraph said CI did; the
//! ``tauri-rust`` job only compiles it.
//!
//! macOS and Linux (``ps``, ``lsof`` and ``kill``, which both have).
//! Windows would need a different process-tree probe.

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

/// The repository root: ``clients/tauri/src-tauri`` is three levels down.
fn repo_root() -> PathBuf {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest
        .ancestors()
        .nth(3)
        .map(PathBuf::from)
        .expect("clients/tauri/src-tauri sits three levels below the repository root")
}

/// igni-core's checkout, whose venv runs the backend: ``IGNI_CORE_DIR``,
/// or ``igni-core`` beside this repository.
fn igni_core() -> PathBuf {
    std::env::var_os("IGNI_CORE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| repo_root().join("..").join("igni-core"))
}

/// Find child PIDs of ``parent_pid`` whose command line mentions
/// ``igni_core.backend``. Uses ``ps`` so we don't take any deps.
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
    // a shell wrapper. (In practice ``IGNI_DEV_BACKEND`` makes it a
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
            if rest.contains("igni_core.backend") {
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

/// `#[ignore]` rather than an early `return` on an env var.
///
/// The gate used to be `IGNI_TAURI_SMOKE=1`, checked inside the body,
/// which meant an ordinary `cargo test` printed **`1 passed`** for a
/// test that did nothing. A skip that reports as a pass is the same
/// mistake as a `--grep-invert` in a Playwright command line: no
/// skipped test, no line in the report, and a green run that covered
/// nothing.
///
/// `#[ignore]` is the mechanism built for this. `cargo test` now says
/// `1 ignored` — visible in every summary — and `cargo test --
/// --ignored` runs it. Nothing outside this file referenced the env
/// var, so there was nothing to keep working.
#[test]
#[ignore = "needs a built debug binary, the project venv, clients/web/dist and a display;             pops a webview window. Run with `cargo test -- --ignored` (CI: the tauri-smoke job)."]
fn tauri_binary_boots_be_and_watchdog_cleans_up_on_exit() {
    // A Windows run would pass by probing nothing, so it fails instead.
    assert!(
        cfg!(unix),
        "tauri_binary_boots_be_and_watchdog_cleans_up_on_exit probes processes with ps and lsof: macOS or Linux only"
    );

    let root = repo_root();
    let venv_python = igni_core().join(".venv").join("bin").join("python");
    assert!(
        venv_python.is_file(),
        "expected venv python at {} — run `uv sync` in igni-core's checkout (IGNI_CORE_DIR) first",
        venv_python.display()
    );
    let dist_index = root.join("clients/web/dist/index.html");
    assert!(
        dist_index.is_file(),
        "expected web bundle at {} — run `npm --prefix clients/web run build` first",
        dist_index.display()
    );

    // Cargo resolves this at compile time from the bin target, so a
    // renamed binary is a build error rather than a silent miss.
    //
    // It was a hardcoded `target/debug/ember-code-app`, which is a name
    // this crate stopped producing at the rename — the package builds
    // `igni-app` now. On a fresh clone the assert below would have
    // fired; on any machine that had built before the rename, the stale
    // artifact was still sitting in `target/debug` and the test would
    // have spawned a month-old binary and passed. Testing dead code and
    // reporting success is the worse of the two failures, and only one
    // of them is visible.
    let binary = PathBuf::from(env!("CARGO_BIN_EXE_igni-app"));
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
        .join(format!("igni-tauri-smoke-{}", std::process::id()));
    std::fs::create_dir_all(&project_dir).expect("create temp project dir");

    // Spawn the shell with the project venv as the BE python so
    // we skip the uv-download bootstrap (which would dominate the
    // test runtime + require network).
    let mut child: Child = Command::new(&binary)
        .env("IGNI_DEV_BACKEND", &venv_python)
        // The dev override is gated on an explicit acknowledgement,
        // added after this test was written: `runtime.rs` ignores
        // `IGNI_DEV_BACKEND` unless `IGNITE_EMBER_DEV` is `1` or
        // `true`, so an ambient variable in a shell profile cannot
        // silently redirect a real user to a stale interpreter.
        //
        // Without it the shell fell through to the managed-venv
        // bootstrap — a uv download over the network — and phase A
        // timed out after 30 seconds. The test then reported "startup
        // is broken", which was not true and was the only thing anybody
        // would have read. Setting it here is the test opting in to the
        // dev mode it was always assuming.
        .env("IGNITE_EMBER_DEV", "1")
        .env("IGNI_PROJECT_DIR", &project_dir)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn igni-app");
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
        // Two causes, and the test cannot tell them apart from here, so
        // it names both rather than accusing the app. It accused the app
        // for as long as it went unrun: the real cause was this test not
        // acknowledging dev mode, and "startup is broken" is what
        // somebody would have spent the afternoon on.
        panic!(
            "Tauri (pid={tauri_pid}) never spawned an igni_core.backend child \
             within 30s. Either startup is broken, or the dev-backend override \
             was declined and the shell is bootstrapping a managed venv over the \
             network — check stderr for the IGNITE_EMBER_DEV notice."
        )
    });

    // ── Phase B: BE binds a loopback port ──
    //
    // 60s, not the 10s this had: the backend now takes about 12s from
    // spawn to listening on an M-series Mac (it imports the model stack
    // before it binds), so the first run in months failed here with
    // "transport never started" while the shell was fine. A CI runner is
    // slower still. The bound is for a hang, not a speed check.
    let port = poll_until(Duration::from_secs(60), || listening_port(be_pid))
        .unwrap_or_else(|| {
            panic!(
                "BE (pid={be_pid}) is alive but never bound a loopback TCP \
                 port within 60s — transport never started"
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

    // The IGNI_PARENT_PID watchdog inside the BE must notice its
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
