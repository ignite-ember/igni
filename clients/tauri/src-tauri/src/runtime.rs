//! Managed-runtime bootstrap — Rust port of ``EmberRuntime.kt``
//! (JetBrains) and ``runtime.ts`` (VSCode).
//!
//! On first launch the Tauri shell now self-provisions everything
//! the BE needs: downloads ``uv``, asks uv to install a pinned
//! CPython, creates a managed venv, installs ``ignite-ember`` at
//! the pinned version, and prefetches igni's decision model
//! (Laya). Subsequent launches reuse the cache and skip
//! straight to spawning the BE — overhead drops to sub-100ms.
//!
//! Everything lives under a per-user cache dir
//! (``~/Library/Caches/ember-code`` on macOS, ``$XDG_CACHE_HOME``
//! on Linux, ``%LOCALAPPDATA%\ember-code`` on Windows).
//!
//! **Dev override.** When ``IGNI_DEV_BACKEND`` is set we use that
//! Python path verbatim and skip every download. This is the
//! escape hatch for ember-code contributors who want to point the
//! shell at an editable ``pip install -e .`` install — same
//! semantics as the JetBrains/VSCode versions.

use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use crate::desktop_settings::DesktopSettings;
use crate::update_policy::UpdatePolicy;

const PYTHON_VERSION: &str = "3.12";
const UV_VERSION: &str = "0.5.7";
/// Also the wire version discovery compares a running backend's
/// lockfile against — the BE stamps its own package `__version__`
/// there, and this is the version we pin it to. See `discovery.rs`.
pub(crate) const IGNITE_EMBER_VERSION: &str = env!("CARGO_PKG_VERSION");
const INSTALL_MARKER: &str = "ember-install.json";

/// 1 GB. Sized for uv (~25 MB) + CPython (~50 MB) + ignite-ember +
/// transitives (laya / transformers / torch — ~600 MB unpacked) +
/// headroom. The decision model itself (~1.7 GB) is fetched after. Failing
/// fast here is much kinder than letting pip die mid-install.
const MIN_BOOTSTRAP_FREE_BYTES: u64 = 1024 * 1024 * 1024;

/// Result of [`ensure_backend_python`]: the Python interpreter to
/// spawn the BE with, plus env vars the caller should layer onto
/// the BE process. ``HF_HOME`` keeps the HuggingFace cache inside
/// the plugin-managed directory so a clean reinstall wipes
/// everything.
///
/// ``actual_cli_version`` is the ``ember_code.__version__`` reported
/// by the chosen interpreter, captured at bootstrap time so
/// ``lib.rs`` can propagate it into the WKWebView URL — the shared
/// web bundle's ``BackendVersionChip`` reads the values off the
/// query string and renders a mismatch warning when the installed
/// CLI drifts from the pinned build. ``None`` on any probe failure
/// (dev override at a Python without ``ember_code`` importable,
/// sandbox denies subprocess, etc.).
pub struct BackendInstall {
    pub python: PathBuf,
    pub env: Vec<(String, String)>,
    pub actual_cli_version: Option<String>,
    pub expected_cli_version: String,
    pub source: BackendSource,
}

/// How the interpreter was resolved. Surfaced in the UI so a user
/// on a dev override or a bit-rotted venv can *see* it without
/// digging through logs.
#[derive(Debug, Clone, Copy)]
pub enum BackendSource {
    ManagedVenv,
    DevOverride,
    /// An interpreter the installer provisioned (`runtime_python`).
    Provisioned,
}

impl BackendSource {
    pub fn as_str(&self) -> &'static str {
        match self {
            BackendSource::ManagedVenv => "managed_venv",
            BackendSource::DevOverride => "dev_override",
            BackendSource::Provisioned => "provisioned",
        }
    }
}

/// The variable naming a provisioned interpreter; wins over the setting.
pub(crate) const RUNTIME_PYTHON_ENV: &str = "IGNI_RUNTIME_PYTHON";

/// The interpreter this install was provisioned with, if it names one:
/// `IGNI_RUNTIME_PYTHON`, else `runtime_python` in the settings files.
///
/// The supported way to run the desktop app where it cannot download its
/// runtime — an air-gapped network, or a locked-down estate. Whoever
/// installs the app installs `ignite-ember[decisions]` from their own
/// mirror into an interpreter and names it here. Unlike `IGNI_PYTHON` it
/// needs no `IGNITE_EMBER_DEV=1`: it is an administrator's decision,
/// written in the settings an administrator owns.
pub(crate) fn provisioned_python_from(env: Option<&str>, settings: &DesktopSettings) -> Option<PathBuf> {
    env.map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .or_else(|| settings.runtime_python.clone())
        .map(PathBuf::from)
}

/// Use a provisioned interpreter, or say why it cannot be used. Never
/// falls back to downloading: the administrator chose this one.
pub(crate) fn provisioned_install(
    python: PathBuf,
    expected: String,
    probe: impl Fn(&Path) -> Option<String>,
) -> Result<BackendInstall, String> {
    if !is_executable(&python) {
        return Err(format!(
            "runtime_python ({}) is not an executable. Point it (or {RUNTIME_PYTHON_ENV}) at the \
             interpreter ignite-ember is installed in.",
            python.display()
        ));
    }
    let Some(actual) = probe(&python) else {
        return Err(format!(
            "runtime_python ({}) runs, but cannot import ember_code. Install \
             ignite-ember[decisions]=={expected} into it from your package mirror.",
            python.display()
        ));
    };
    if actual != expected {
        // Shown in the app too (the version chip); not fatal, as with the
        // managed venv after a manual upgrade.
        eprintln!(
            "runtime_python at {} runs ignite-ember {actual}; this app is built for {expected}.",
            python.display()
        );
    }
    Ok(BackendInstall {
        python,
        env: vec![],
        actual_cli_version: Some(actual),
        expected_cli_version: expected,
        source: BackendSource::Provisioned,
    })
}

/// Progress callback signature — short human-readable status
/// strings the caller surfaces in the loading view ("Downloading
/// uv…" / "Installing Python…" / …).
pub type ProgressFn<'a> = &'a (dyn Fn(&str) + Sync);

/// Resolve a Python interpreter with ``ignite-ember`` installed and
/// its decision model pre-warmed. Bootstraps on first
/// call; returns cached on subsequent calls.
///
/// Returns an error with a human-readable message on any failure —
/// the caller surfaces it in the UI; we never panic.
pub fn ensure_backend_python(progress: ProgressFn) -> Result<BackendInstall, String> {
    let expected = IGNITE_EMBER_VERSION.to_string();

    // ── Dev / user overrides ──
    // Both ``IGNI_DEV_BACKEND`` and ``IGNI_PYTHON`` are opt-in
    // escape hatches for contributors running against an editable
    // checkout. They're deliberately gated on the explicit
    // ``IGNITE_EMBER_DEV=1`` ack so an ambient env var left over
    // in ``~/.zshenv`` or a launchd plist can't silently redirect
    // a regular user to a stale interpreter — the exact footgun
    // that hid a v0.3.8 Homebrew CLI behind a v0.8.x plugin.
    let dev_ack = ack_dev_mode();
    let dev_backend = std::env::var("IGNI_DEV_BACKEND")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let ember_python = std::env::var("IGNI_PYTHON")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());

    if let Some(dev) = dev_backend.clone() {
        if dev_ack {
            let path = PathBuf::from(&dev);
            let actual = probe_cli_version(&path);
            if let Some(ref v) = actual {
                if v != &expected {
                    eprintln!(
                        "IGNI_DEV_BACKEND at {} runs ignite-ember {}, plugin pinned to {}. \
                         Continuing (dev mode).",
                        dev, v, expected
                    );
                }
            }
            return Ok(BackendInstall {
                python: path,
                env: vec![],
                actual_cli_version: actual,
                expected_cli_version: expected,
                source: BackendSource::DevOverride,
            });
        } else {
            eprintln!(
                "IGNI_DEV_BACKEND={} detected but IGNITE_EMBER_DEV is unset — \
                 ignoring override and using the managed venv. \
                 Set IGNITE_EMBER_DEV=1 to opt in to the dev-mode override.",
                dev
            );
        }
    }
    if let Some(py) = ember_python.clone() {
        if dev_ack {
            let path = PathBuf::from(&py);
            let actual = probe_cli_version(&path);
            return Ok(BackendInstall {
                python: path,
                env: vec![],
                actual_cli_version: actual,
                expected_cli_version: expected,
                source: BackendSource::DevOverride,
            });
        } else {
            eprintln!(
                "IGNI_PYTHON={} detected but IGNITE_EMBER_DEV is unset — \
                 ignoring override and using the managed venv.",
                py
            );
        }
    }

    // ── A runtime the installer provisioned ──
    let settings = DesktopSettings::load();
    if let Some(python) = provisioned_python_from(std::env::var(RUNTIME_PYTHON_ENV).ok().as_deref(), &settings) {
        return provisioned_install(python, expected, probe_cli_version);
    }

    let cache = cache_root()?;
    fs::create_dir_all(&cache).map_err(|e| format!("create cache dir: {e}"))?;
    let hf_home = cache.join("huggingface");

    // ── Disk-space precondition ──
    ensure_free_space(&cache, MIN_BOOTSTRAP_FREE_BYTES)?;

    let uv_path = cache.join(uv_bin_name());
    let marker_path = cache.join(INSTALL_MARKER);
    let venv_dir = cache.join("venv");
    let venv_python = venv_dir.join(venv_python_rel_path());

    let want_marker = format!(
        "uv={UV_VERSION};python={PYTHON_VERSION};ignite={IGNITE_EMBER_VERSION}"
    );
    let have_marker = fs::read_to_string(&marker_path).ok();
    let marker_matches = have_marker.as_deref() == Some(want_marker.as_str());

    // Probe the venv's interpreter to catch a specific failure
    // mode: marker file says one version, but the wheels on disk
    // are a different version (manual pip upgrade, half-finished
    // install, plugin update that skipped the marker rewrite,
    // etc.). We only ACT on a positive mismatch — probe returned
    // a version AND it differs. Null probe = interpreter missing
    // or wedged; fall back to the marker/executable signals so a
    // transient subprocess hiccup doesn't trigger a multi-minute
    // reinstall on every startup.
    let venv_actual_version = if is_executable(&venv_python) {
        probe_cli_version(&venv_python)
    } else {
        None
    };
    let venv_version_mismatch = matches!(
        venv_actual_version.as_deref(),
        Some(v) if v != IGNITE_EMBER_VERSION
    );
    if venv_version_mismatch && marker_matches {
        eprintln!(
            "Managed venv marker says ignite={} but the interpreter reports {} — reinstalling.",
            IGNITE_EMBER_VERSION,
            venv_actual_version.as_deref().unwrap_or("<probe failed>")
        );
    }
    let needs_reinstall =
        !is_executable(&venv_python) || !marker_matches || venv_version_mismatch;

    // An air-gapped install has no route to github.com (uv), the Python
    // builds uv fetches, PyPI or Hugging Face, so it does not try: the
    // runtime has to be provisioned into the cache by whoever installs
    // the app. Said plainly rather than left to four timeouts.
    if (!is_executable(&uv_path) || needs_reinstall) && UpdatePolicy::load().air_gapped() {
        return Err(format!(
            "igni is air-gapped (air_gapped: true), so the desktop app will not download its Python \
             runtime. Install ignite-ember[decisions]=={IGNITE_EMBER_VERSION} from your package mirror \
             into a Python {PYTHON_VERSION} interpreter and set runtime_python in \
             ~/.igni/config.yaml or managed settings (or {RUNTIME_PYTHON_ENV}) to its path, then \
             start the app again. (Cache directory for a managed install: {}.)",
            cache.display()
        ));
    }

    // ── 1. uv binary ──
    if !is_executable(&uv_path) || needs_reinstall {
        progress("Downloading uv (one-time, ~25 MB)…");
        download_uv(&uv_path).map_err(|e| format!("download uv: {e}"))?;
    }

    // ── 2-5. Python + venv + ignite-ember + prefetch ──
    if needs_reinstall {
        if venv_dir.exists() {
            progress("Refreshing managed venv…");
            fs::remove_dir_all(&venv_dir).map_err(|e| format!("clean venv: {e}"))?;
        }
        progress(&format!("Installing Python {PYTHON_VERSION} (one-time)…"));
        run_uv(&uv_path, &["python", "install", PYTHON_VERSION])?;

        progress("Creating backend venv…");
        run_uv(&uv_path, &["venv", "--python", PYTHON_VERSION,
            venv_dir.to_string_lossy().as_ref()])?;

        progress("Installing ignite-ember (one-time)…");
        run_uv(
            &uv_path,
            &[
                "pip",
                "install",
                "--python",
                venv_python.to_string_lossy().as_ref(),
                // ``decisions``: igni's decisions service (Laya), which
                // the backend starts beside itself on first use.
                &format!("ignite-ember[decisions]=={IGNITE_EMBER_VERSION}"),
            ],
        )?;

        // Pre-warm the decision model so the first Bypass check or
        // `host.decide` doesn't stall on a silent HuggingFace download.
        progress("Downloading igni's decision model (one-time, ~1.7 GB)…");
        let mut cmd = Command::new(&venv_python);
        cmd.args(["-m", "ember_code.prefetch_models"]);
        cmd.env("HF_HOME", &hf_home);
        let status = cmd
            .status()
            .map_err(|e| format!("spawn prefetch_models: {e}"))?;
        if !status.success() {
            return Err(format!(
                "prefetch_models exited with status {status}"
            ));
        }

        fs::write(&marker_path, &want_marker)
            .map_err(|e| format!("write install marker: {e}"))?;
    }

    // Probe the (possibly-just-reinstalled) venv one more time so
    // the returned ``BackendInstall`` carries the confirmed
    // version. Skipped when the initial probe already matched and
    // no reinstall happened.
    let final_version = if needs_reinstall {
        probe_cli_version(&venv_python)
    } else {
        venv_actual_version
    };

    Ok(BackendInstall {
        python: venv_python,
        env: vec![("HF_HOME".to_string(), hf_home.to_string_lossy().into_owned())],
        actual_cli_version: final_version,
        expected_cli_version: expected,
        source: BackendSource::ManagedVenv,
    })
}

/// True if ``IGNITE_EMBER_DEV`` is set to something truthy. Kept
/// out of the two override branches so both interpret the ack
/// signal the same way — accept ``1`` or a case-insensitive
/// ``true``; treat anything else (including unset) as "not
/// acknowledged, ignore the override".
fn ack_dev_mode() -> bool {
    match std::env::var("IGNITE_EMBER_DEV") {
        Ok(v) => is_ack_value(&v),
        Err(_) => false,
    }
}

/// Pure classifier for the ``IGNITE_EMBER_DEV`` env var's *value*
/// — decoupled from ``std::env::var`` so tests can pin the
/// acceptance rules without needing to mutate process-global
/// state (which is racy under Rust's default parallel test
/// runner). Anything that isn't exactly ``"1"`` or a case-
/// insensitive ``"true"`` is treated as "not acknowledged".
fn is_ack_value(v: &str) -> bool {
    v == "1" || v.eq_ignore_ascii_case("true")
}

/// Return ``ember_code.__version__`` as reported by the given
/// Python interpreter, or ``None`` on any failure. 2s subprocess
/// timeout — the real observed cost for a cold ``import ember_code
/// + print(__version__)`` is 30-80ms; the ceiling is a safety net
/// against a wedged interpreter blocking the whole bootstrap.
pub fn probe_cli_version(python: &Path) -> Option<String> {
    if !is_executable(python) {
        return None;
    }
    let mut cmd = Command::new(python);
    cmd.args([
        "-c",
        "import ember_code, sys; sys.stdout.write(ember_code.__version__)",
    ]);
    // ``stderr`` merged so a Python-side import error doesn't
    // leak onto the parent's console; we only care about
    // stdout content on a successful exit.
    cmd.stderr(std::process::Stdio::null());
    let output = match cmd.output() {
        Ok(o) => o,
        Err(_) => return None,
    };
    if !output.status.success() {
        return None;
    }
    let out = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if out.is_empty() {
        None
    } else {
        Some(out)
    }
}

/// Wipe the managed cache so the next ``ensure_backend_python``
/// re-downloads everything. Wired to the "Reinstall Backend (Clean)"
/// menu item.
pub fn reset_cache() -> Result<(), String> {
    let cache = cache_root()?;
    if cache.exists() {
        fs::remove_dir_all(&cache)
            .map_err(|e| format!("wipe cache {}: {}", cache.display(), e))?;
    }
    Ok(())
}

// ── Platform + paths ───────────────────────────────────────────────

fn cache_root() -> Result<PathBuf, String> {
    dirs::cache_dir()
        .map(|d| d.join("ember-code"))
        .ok_or_else(|| "could not resolve per-user cache dir".to_string())
}

/// Cache root for display-only callers (Doctor dialog). Never fails
/// — returns a placeholder path if the OS refuses to hand us a
/// cache dir, so the diagnostic report still renders cleanly instead
/// of blowing up on the ``?`` here.
pub fn cache_root_or_display() -> PathBuf {
    cache_root().unwrap_or_else(|_| PathBuf::from("<cache dir unavailable>"))
}

/// Where the managed venv's Python interpreter lives under a given
/// cache root. Used by the Doctor dialog to render the same path
/// the bootstrap consults.
pub fn venv_python_path(cache: &Path) -> PathBuf {
    cache.join("venv").join(venv_python_rel_path())
}

/// Public thin wrapper around ``is_executable`` so the Doctor
/// dialog in ``lib.rs`` can check "is the managed venv actually
/// present" without duplicating the metadata call.
pub fn is_executable_path(p: &Path) -> bool {
    is_executable(p)
}

fn uv_bin_name() -> &'static str {
    if cfg!(target_os = "windows") { "uv.exe" } else { "uv" }
}

fn venv_python_rel_path() -> &'static str {
    if cfg!(target_os = "windows") { "Scripts/python.exe" } else { "bin/python" }
}

/// Map the running OS/arch onto the uv GitHub-release asset name.
fn uv_target() -> Result<&'static str, String> {
    let triple = match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => "aarch64-apple-darwin",
        ("linux", "aarch64") => "aarch64-unknown-linux-gnu",
        ("linux", "x86_64") => "x86_64-unknown-linux-gnu",
        ("windows", "x86_64") => "x86_64-pc-windows-msvc",
        (os, arch) => return Err(format!("unsupported platform: {os}/{arch}")),
    };
    Ok(triple)
}

fn is_executable(p: &Path) -> bool {
    fs::metadata(p)
        .map(|m| m.is_file())
        .unwrap_or(false)
}

fn ensure_free_space(dir: &Path, min_bytes: u64) -> Result<(), String> {
    let mut probe = dir.to_path_buf();
    while !probe.exists() {
        match probe.parent() {
            Some(p) => probe = p.to_path_buf(),
            None => return Ok(()),
        }
    }
    let free = fs2::available_space(&probe)
        .map_err(|e| format!("disk-space check failed: {e}"))?;
    if free >= min_bytes {
        return Ok(());
    }
    let free_mb = free / (1024 * 1024);
    let need_mb = min_bytes / (1024 * 1024);
    Err(format!(
        "Not enough disk space for the Ember backend bootstrap: \
         {free_mb} MB free at {}, need at least {need_mb} MB. \
         Free up space and try again.",
        dir.display()
    ))
}

// ── uv download + extract ─────────────────────────────────────────

fn download_uv(target: &Path) -> Result<(), String> {
    let triple = uv_target()?;
    let ext = if cfg!(target_os = "windows") { "zip" } else { "tar.gz" };
    let url = format!(
        "https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{triple}.{ext}"
    );

    // ureq follows redirects by default — GitHub release URLs
    // redirect once to a signed S3 URL.
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(15))
        .timeout_read(Duration::from_secs(120))
        .build();
    let resp = agent
        .get(&url)
        .call()
        .map_err(|e| format!("HTTP error fetching uv: {e}"))?;
    if resp.status() < 200 || resp.status() >= 300 {
        return Err(format!("uv download returned HTTP {} from {url}", resp.status()));
    }
    let tmp = std::env::temp_dir().join(format!("uv-download-{}.{ext}", std::process::id()));
    let mut bytes = Vec::new();
    resp.into_reader()
        .read_to_end(&mut bytes)
        .map_err(|e| format!("read uv body: {e}"))?;
    fs::write(&tmp, &bytes).map_err(|e| format!("write uv tmp: {e}"))?;

    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("create dest dir: {e}"))?;
    }
    extract_uv(&tmp, target, ext)?;
    let _ = fs::remove_file(&tmp);
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn extract_uv(archive: &Path, dest: &Path, _ext: &str) -> Result<(), String> {
    let file = fs::File::open(archive).map_err(|e| format!("open archive: {e}"))?;
    let gz = flate2::read::GzDecoder::new(file);
    let mut ar = tar::Archive::new(gz);
    let extract_dir = std::env::temp_dir().join(format!("uv-extract-{}", std::process::id()));
    fs::create_dir_all(&extract_dir).map_err(|e| format!("mkdir extract: {e}"))?;
    ar.unpack(&extract_dir).map_err(|e| format!("untar: {e}"))?;
    // uv release tarballs contain ``uv-<triple>/uv`` — find it.
    let uv_src = find_named(&extract_dir, "uv")
        .ok_or_else(|| "uv binary not found in extracted archive".to_string())?;
    fs::rename(&uv_src, dest)
        .or_else(|_| fs::copy(&uv_src, dest).map(|_| ()))
        .map_err(|e| format!("move uv to {}: {}", dest.display(), e))?;
    use std::os::unix::fs::PermissionsExt;
    let mut perms = fs::metadata(dest).unwrap().permissions();
    perms.set_mode(0o755);
    fs::set_permissions(dest, perms).map_err(|e| format!("chmod uv: {e}"))?;
    let _ = fs::remove_dir_all(&extract_dir);
    Ok(())
}

#[cfg(target_os = "windows")]
fn extract_uv(archive: &Path, dest: &Path, _ext: &str) -> Result<(), String> {
    let file = fs::File::open(archive).map_err(|e| format!("open archive: {e}"))?;
    let mut zip =
        zip::ZipArchive::new(file).map_err(|e| format!("open zip: {e}"))?;
    for i in 0..zip.len() {
        let mut entry = zip.by_index(i).map_err(|e| format!("zip entry: {e}"))?;
        if entry.is_file() && entry.name().ends_with("uv.exe") {
            let mut out = fs::File::create(dest).map_err(|e| format!("create dest: {e}"))?;
            std::io::copy(&mut entry, &mut out)
                .map_err(|e| format!("copy uv.exe: {e}"))?;
            return Ok(());
        }
    }
    Err("uv.exe not found in archive".to_string())
}

fn find_named(root: &Path, name: &str) -> Option<PathBuf> {
    let entries = fs::read_dir(root).ok()?;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.file_name().map(|n| n == name).unwrap_or(false) && path.is_file() {
            return Some(path);
        }
        if path.is_dir() {
            if let Some(p) = find_named(&path, name) {
                return Some(p);
            }
        }
    }
    None
}

// ── Subprocess invocation ─────────────────────────────────────────

fn run_uv(uv: &Path, args: &[&str]) -> Result<(), String> {
    let status = Command::new(uv)
        .args(args)
        .status()
        .map_err(|e| format!("spawn uv: {e}"))?;
    if !status.success() {
        return Err(format!("uv {} exited with status {}", args.join(" "), status));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn uv_target_returns_a_known_triple_on_this_platform() {
        // ``uv_target`` is compile-time-bound to the host arch; we can
        // only assert it returns *something* sensible here. The
        // exhaustive match is the contract; this test catches a
        // regression that removes the current host's arm.
        let triple = uv_target().expect("host platform must be mapped");
        let known = [
            "aarch64-apple-darwin",
            "aarch64-unknown-linux-gnu",
            "x86_64-unknown-linux-gnu",
            "x86_64-pc-windows-msvc",
        ];
        assert!(known.contains(&triple), "unexpected triple {triple:?}");
    }

    #[test]
    fn is_executable_detects_real_file() {
        let tmp = std::env::temp_dir().join(format!(
            "ember_runtime_test_{}",
            std::process::id()
        ));
        fs::write(&tmp, b"hi").unwrap();
        assert!(is_executable(&tmp));
        // is_executable says false for a directory.
        assert!(!is_executable(&std::env::temp_dir()));
        // …and false for a nonexistent path.
        assert!(!is_executable(&tmp.with_extension("nope")));
        let _ = fs::remove_file(&tmp);
    }

    #[test]
    fn ensure_free_space_accepts_zero_requirement() {
        // 0-byte requirement should pass on any reachable directory.
        let tmp = std::env::temp_dir();
        assert!(ensure_free_space(&tmp, 0).is_ok());
    }

    #[test]
    fn ensure_free_space_rejects_impossibly_large_requirement() {
        // A petabyte won't fit on a dev laptop — assert the error
        // message reports both available and required for the user.
        let tmp = std::env::temp_dir();
        let petabyte: u64 = 1024 * 1024 * 1024 * 1024 * 1024;
        let err = ensure_free_space(&tmp, petabyte).unwrap_err();
        assert!(err.contains("Not enough disk space"));
        assert!(err.contains("MB free"));
        assert!(err.contains("need at least"));
    }

    #[test]
    fn ensure_free_space_walks_up_to_existing_parent() {
        // Given a path whose own dir doesn't exist yet, the check
        // should probe the closest existing ancestor instead of
        // erroring on the missing leaf.
        let nonexistent = std::env::temp_dir()
            .join("ember_runtime_test_does_not_exist")
            .join("nested")
            .join("further");
        assert!(ensure_free_space(&nonexistent, 0).is_ok());
    }

    #[test]
    fn uv_bin_name_has_expected_extension() {
        let name = uv_bin_name();
        if cfg!(target_os = "windows") {
            assert_eq!(name, "uv.exe");
        } else {
            assert_eq!(name, "uv");
        }
    }

    #[test]
    fn venv_python_relpath_matches_platform_layout() {
        let p = venv_python_rel_path();
        if cfg!(target_os = "windows") {
            assert_eq!(p, "Scripts/python.exe");
        } else {
            assert_eq!(p, "bin/python");
        }
    }

    // ── ack_dev_mode / is_ack_value ───────────────────────────────
    //
    // The dev-override lifetime should be controlled by an *explicit*
    // ack, not by the presence of an ambient env var. These tests pin
    // exactly what counts as "acknowledged" so a future rename or
    // relaxation doesn't accidentally re-open the silent-hijack
    // path this whole subsystem was added to close.

    #[test]
    fn is_ack_value_accepts_one() {
        assert!(is_ack_value("1"));
    }

    #[test]
    fn is_ack_value_accepts_true_case_insensitive() {
        assert!(is_ack_value("true"));
        assert!(is_ack_value("True"));
        assert!(is_ack_value("TRUE"));
    }

    #[test]
    fn is_ack_value_rejects_zero() {
        // ``0`` is a common "off" marker; make sure it's not
        // accepted as a truthy value the way a naive
        // ``parse::<bool>`` fallback would.
        assert!(!is_ack_value("0"));
    }

    #[test]
    fn is_ack_value_rejects_empty_string() {
        // Empty string means "set but blank" — treat as unset.
        assert!(!is_ack_value(""));
    }

    #[test]
    fn is_ack_value_rejects_yes_and_other_truthy_words() {
        // Only ``1`` and ``true`` are contract. Anything else
        // (including plausibly-truthy words like ``yes``) is
        // rejected so users can't accidentally opt in via a
        // slightly different convention picked up from another
        // tool.
        assert!(!is_ack_value("yes"));
        assert!(!is_ack_value("on"));
        assert!(!is_ack_value("enable"));
    }

    // ── BackendSource ─────────────────────────────────────────────
    //
    // The wire shape of ``backend_source`` (in URL query params and
    // ``<meta>`` tags) is what the web bundle's ``BackendVersionChip``
    // matches on. Changing these strings without updating the FE
    // would silently drop the chip's "dev override" warning tone,
    // so pin the wire values.

    #[test]
    fn backend_source_serializes_as_stable_strings() {
        assert_eq!(BackendSource::ManagedVenv.as_str(), "managed_venv");
        assert_eq!(BackendSource::DevOverride.as_str(), "dev_override");
        assert_eq!(BackendSource::Provisioned.as_str(), "provisioned");
    }

    // ── A provisioned runtime (air-gapped installs) ──

    fn named(runtime_python: Option<&str>) -> DesktopSettings {
        DesktopSettings { runtime_python: runtime_python.map(str::to_string), ..DesktopSettings::default() }
    }

    #[test]
    fn the_variable_wins_over_the_setting() {
        assert_eq!(
            provisioned_python_from(Some("/env/python"), &named(Some("/cfg/python"))),
            Some(PathBuf::from("/env/python"))
        );
        assert_eq!(provisioned_python_from(Some("  "), &named(Some("/cfg/python"))), Some(PathBuf::from("/cfg/python")));
        assert_eq!(provisioned_python_from(None, &named(None)), None, "nothing named: the managed venv");
    }

    #[test]
    fn a_provisioned_runtime_needs_no_dev_acknowledgement() {
        // The selection reads only the variable and the settings; IGNITE_EMBER_DEV
        // is not among its inputs, which is the difference from IGNI_PYTHON.
        let src = include_str!("runtime.rs");
        let body = src.split("fn provisioned_python_from").nth(1).unwrap().split("\n}\n").next().unwrap();
        assert!(!body.contains("ack_dev_mode") && !body.contains("IGNITE_EMBER_DEV"));
    }

    #[test]
    fn a_missing_interpreter_is_refused_with_the_setting_named() {
        let err = provisioned_install(PathBuf::from("/nonexistent/python"), "1.0.3".into(), |_| Some("1.0.3".into()))
            .err()
            .expect("refused");
        assert!(err.contains("runtime_python") && err.contains(RUNTIME_PYTHON_ENV), "{err}");
    }

    #[cfg(unix)]
    fn fake_python(dir: &Path, prints: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let path = dir.join("python");
        fs::write(&path, format!("#!/bin/sh\nprintf '{prints}'\n")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    #[cfg(unix)]
    #[test]
    fn a_provisioned_interpreter_is_used_as_is() {
        let dir = std::env::temp_dir().join(format!("igni-provisioned-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let python = fake_python(&dir, "1.0.3");
        let install = provisioned_install(python.clone(), "1.0.3".into(), probe_cli_version).expect("used");
        assert_eq!(install.python, python);
        assert_eq!(install.source.as_str(), "provisioned");
        assert_eq!(install.actual_cli_version.as_deref(), Some("1.0.3"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_interpreter_without_igni_is_refused() {
        // `true` exists and is executable everywhere this runs, and has no ember_code.
        let python = PathBuf::from(if cfg!(windows) { "C:/Windows/System32/whoami.exe" } else { "/usr/bin/true" });
        if !is_executable(&python) {
            return;
        }
        let err = provisioned_install(python, "1.0.3".into(), |_| None).err().expect("refused");
        assert!(err.contains("cannot import ember_code"), "{err}");
    }
}
