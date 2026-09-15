//! Existing-backend discovery — attach rather than spawn a duplicate.
//!
//! The BE publishes its PID, WebSocket port and wire version at
//! `<project>/.igni/backend.lock` as soon as it binds (Python side:
//! `backend/supervisor.py::write_discovery_lockfile`, called from
//! `backend/app.py`). Every client is meant to read that before
//! starting one of its own. The IDE plugins do
//! (`clients/vscode/src/extension.ts`); this shell did not.
//!
//! What that cost: open a repo in VS Code and then the desktop app on
//! the same repo, and you got two backends. The second one overwrote
//! the lockfile — the write has no pre-check — so the first became
//! undiscoverable and a third client would attach to the second while
//! the first kept serving its own windows. Both then ran codeindex
//! sync and file watchers over one repo, and a session opened in both
//! had two live runtimes writing checkpoints and event-log rows for
//! it.
//!
//! The decision is deliberately split from the I/O: [`decide`] is pure
//! over (payload, pid alive, port reachable, expected version), so all
//! four outcomes are unit-testable without a filesystem, a process
//! table, or a socket.

use serde::Deserialize;
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::time::Duration;

/// Current config dir. The directory was called `.ember` before the
/// rename and is checked second — a backend started before the
/// upgrade still holds the old path, and a missing lock means "spawn",
/// so ignoring the legacy name would create the very duplicate this
/// module exists to prevent.
const CONFIG_DIR: &str = ".igni";
const LEGACY_CONFIG_DIR: &str = ".ember";

/// Matches the plugins' probe budget. Long enough for a loopback
/// connect to a live listener, short enough that a stale lock doesn't
/// visibly delay startup.
const PROBE_TIMEOUT: Duration = Duration::from_millis(500);

/// The fields we act on. `created_at` is in the file too and ignored
/// here — staleness is decided by probing the process and the port,
/// not by trusting a timestamp.
#[derive(Debug, Clone, Deserialize)]
pub struct LockfilePayload {
    pub pid: i32,
    pub port: u16,
    pub wire_version: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// A live BE for this project speaks our wire version — use it.
    Attach { port: u16, wire_version: String },
    /// A live BE owns this project but speaks a different version.
    /// Refuse: mixing traffic across wire versions corrupts state,
    /// and spawning alongside it recreates the duplicate.
    Refuse { running: String },
    /// No lock, a dead one, or an unreachable port — start our own.
    Spawn,
}

/// `<project>/.igni/backend.lock`, falling back to the legacy
/// `.ember` directory, defaulting to the current path when neither
/// exists (the caller then fails the read and spawns).
pub fn lockfile_path(project_dir: &str) -> PathBuf {
    let root = Path::new(project_dir);
    let current = root.join(CONFIG_DIR).join("backend.lock");
    if current.exists() {
        return current;
    }
    let legacy = root.join(LEGACY_CONFIG_DIR).join("backend.lock");
    if legacy.exists() {
        return legacy;
    }
    current
}

/// Parse the lockfile JSON. `None` on anything malformed — a corrupt
/// lock is treated exactly like a missing one.
pub fn parse_lockfile(raw: &str) -> Option<LockfilePayload> {
    serde_json::from_str(raw).ok()
}

/// The whole policy, as a pure function.
pub fn decide(
    payload: Option<&LockfilePayload>,
    pid_alive: bool,
    port_reachable: bool,
    expected_wire_version: &str,
) -> Decision {
    let Some(payload) = payload else {
        return Decision::Spawn;
    };
    if !pid_alive || !port_reachable {
        return Decision::Spawn;
    }
    if payload.wire_version != expected_wire_version {
        return Decision::Refuse {
            running: payload.wire_version.clone(),
        };
    }
    Decision::Attach {
        port: payload.port,
        wire_version: payload.wire_version.clone(),
    }
}

#[cfg(unix)]
pub fn pid_alive(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    // `kill(pid, 0)` is the canonical no-op probe: 0 when the process
    // exists, ESRCH when it doesn't, EPERM when it exists but we may
    // not signal it. EPERM still means alive; same-user processes
    // never hit it anyway.
    if unsafe { libc::kill(pid, 0) } == 0 {
        return true;
    }
    std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

#[cfg(not(unix))]
pub fn pid_alive(pid: i32) -> bool {
    // No liveness probe without pulling in the Windows API crate for
    // one call. The port probe still has to pass, so the case this
    // guard exists for — a dead BE whose port has been recycled by an
    // unrelated process — remains reachable on Windows alone.
    pid > 0
}

/// Loopback TCP connect. Port 0 (a lockfile written before the bind
/// resolved) fails here like any other closed port.
pub fn port_reachable(port: u16) -> bool {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
    TcpStream::connect_timeout(&addr, PROBE_TIMEOUT).is_ok()
}

/// Read, probe, decide — and drop a lock we've decided not to use.
///
/// A stale or corrupt lock is removed so the BE we're about to spawn
/// can publish its own. A version mismatch is *not* stale: that
/// backend is alive and legitimately owns the project, so its
/// lockfile stays exactly where it is.
pub fn discover(project_dir: &str, expected_wire_version: &str) -> Decision {
    let path = lockfile_path(project_dir);
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return Decision::Spawn;
    };
    let payload = parse_lockfile(&raw);
    let decision = match payload.as_ref() {
        Some(p) => decide(
            Some(p),
            pid_alive(p.pid),
            port_reachable(p.port),
            expected_wire_version,
        ),
        None => Decision::Spawn,
    };
    if decision == Decision::Spawn {
        let _ = std::fs::remove_file(&path);
    }
    decision
}

#[cfg(test)]
mod tests {
    use super::*;

    fn payload(version: &str) -> LockfilePayload {
        LockfilePayload {
            pid: 4242,
            port: 51234,
            wire_version: version.to_string(),
        }
    }

    // ── decide ────────────────────────────────────────────────

    #[test]
    fn no_lockfile_spawns() {
        assert_eq!(decide(None, false, false, "1.0.3"), Decision::Spawn);
    }

    #[test]
    fn dead_pid_spawns() {
        // The BE crashed or was killed; the lock outlived it.
        let p = payload("1.0.3");
        assert_eq!(decide(Some(&p), false, true, "1.0.3"), Decision::Spawn);
    }

    #[test]
    fn unreachable_port_spawns() {
        // PID recycled onto an unrelated process, or the BE is wedged
        // before binding. Either way there is nothing to talk to.
        let p = payload("1.0.3");
        assert_eq!(decide(Some(&p), true, false, "1.0.3"), Decision::Spawn);
    }

    #[test]
    fn version_mismatch_refuses_without_spawning() {
        // The important one: the alternative to refusing is a second
        // BE on the same project, which is the bug this module fixes.
        let p = payload("0.9.1");
        assert_eq!(
            decide(Some(&p), true, true, "1.0.3"),
            Decision::Refuse {
                running: "0.9.1".to_string()
            }
        );
    }

    #[test]
    fn live_matching_backend_attaches() {
        let p = payload("1.0.3");
        assert_eq!(
            decide(Some(&p), true, true, "1.0.3"),
            Decision::Attach {
                port: 51234,
                wire_version: "1.0.3".to_string()
            }
        );
    }

    // ── parse_lockfile ────────────────────────────────────────

    #[test]
    fn parses_the_python_payload_shape() {
        // `created_at` is written by the BE and unused here; it must
        // not make the parse fail.
        let got = parse_lockfile(
            r#"{"pid":4242,"port":51234,"wire_version":"1.0.3","created_at":1757000000}"#,
        )
        .expect("should parse");
        assert_eq!(got.pid, 4242);
        assert_eq!(got.port, 51234);
        assert_eq!(got.wire_version, "1.0.3");
    }

    #[test]
    fn corrupt_lockfile_is_none() {
        assert!(parse_lockfile("{not json").is_none());
        // Missing a required field is just as unusable as bad syntax.
        assert!(parse_lockfile(r#"{"pid":1}"#).is_none());
    }

    // ── pid_alive ─────────────────────────────────────────────

    #[test]
    fn own_process_is_alive() {
        assert!(pid_alive(std::process::id() as i32));
    }

    #[cfg(unix)]
    #[test]
    fn nonpositive_pids_are_dead() {
        // `kill(0, 0)` signals the whole process group and `kill(-1,
        // 0)` every process we may signal — both would report
        // "alive" and wrongly keep a corrupt lock alive.
        assert!(!pid_alive(0));
        assert!(!pid_alive(-1));
    }

    // ── lockfile_path ─────────────────────────────────────────

    #[test]
    fn prefers_current_dir_then_legacy_then_default() {
        let root = std::env::temp_dir().join(format!("igni-discovery-{}", std::process::id()));
        let igni = root.join(CONFIG_DIR);
        let ember = root.join(LEGACY_CONFIG_DIR);
        std::fs::create_dir_all(&igni).unwrap();
        std::fs::create_dir_all(&ember).unwrap();
        let dir = root.to_string_lossy().to_string();

        // Neither present → the current path, which the caller fails
        // to read and turns into a spawn.
        assert_eq!(lockfile_path(&dir), igni.join("backend.lock"));

        // Only the pre-rename lock exists — a BE started before the
        // upgrade. Missing this is what creates a duplicate.
        std::fs::write(ember.join("backend.lock"), "{}").unwrap();
        assert_eq!(lockfile_path(&dir), ember.join("backend.lock"));

        // Both exist → current wins.
        std::fs::write(igni.join("backend.lock"), "{}").unwrap();
        assert_eq!(lockfile_path(&dir), igni.join("backend.lock"));

        std::fs::remove_dir_all(&root).ok();
    }

    // ── discover ──────────────────────────────────────────────

    #[test]
    fn discover_removes_a_stale_lock_but_keeps_a_mismatched_one() {
        let root = std::env::temp_dir().join(format!("igni-discovery-io-{}", std::process::id()));
        let igni = root.join(CONFIG_DIR);
        std::fs::create_dir_all(&igni).unwrap();
        let dir = root.to_string_lossy().to_string();
        let lock = igni.join("backend.lock");

        // Dead PID → spawn, and the stale lock is cleared so the BE
        // we start can publish its own.
        std::fs::write(
            &lock,
            r#"{"pid":999999999,"port":1,"wire_version":"1.0.3","created_at":1}"#,
        )
        .unwrap();
        assert_eq!(discover(&dir, "1.0.3"), Decision::Spawn);
        assert!(!lock.exists(), "stale lock should be removed");

        // A live listener on a version we don't speak: refuse, and
        // leave the lock alone — that BE still owns the project.
        let server = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = server.local_addr().unwrap().port();
        std::fs::write(
            &lock,
            format!(
                r#"{{"pid":{},"port":{port},"wire_version":"0.9.1","created_at":1}}"#,
                std::process::id()
            ),
        )
        .unwrap();
        assert_eq!(
            discover(&dir, "1.0.3"),
            Decision::Refuse {
                running: "0.9.1".to_string()
            }
        );
        assert!(lock.exists(), "a live BE's lock must survive");

        std::fs::remove_dir_all(&root).ok();
    }
}
