//! The few settings the desktop shell reads itself, out of the files the
//! backend merges them from.
//!
//! Read here rather than asked of the backend because this process acts on
//! them before there is a backend: it checks for releases, and it finds (or
//! refuses to download) the Python runtime the backend runs on. A check that
//! consults a policy one process away can be reached without consulting it;
//! a check that reads the file it is about cannot.
//!
//! The layers, in the backend's precedence: the managed-settings file a
//! sysadmin or MDM deploys, then the organisation's group policy, then
//! `~/.igni/config.yaml`. A project's own `.igni/config.yaml` is not
//! consulted: the shell has no project when it reads these.
//!
//! The group policy layer is the pack the backend cached exactly as igni
//! server signed it (`group-policy/pack.jws`), and it counts only if its
//! signature, issuer, subject and version verify (`group_pack.rs`). The
//! unsigned `group_settings.json` this used to read is gone: a file anyone
//! who can write the directory can edit is not the organisation's policy.
//!
//! Parsed by hand rather than with a YAML crate. A few scalars at known
//! top-level keys are not worth a dependency in a binary that ships to
//! customers. Two failure modes are chosen deliberately: an unreadable TTL
//! leaves update checks **on** (silently disabling updates strands a machine
//! on an old build), while `air_gapped` counts only an explicit `true` as on,
//! so a typo cannot make an ordinary install stop updating.

use std::path::PathBuf;

/// The environment variable that turns air-gapped mode on.
pub(crate) const AIR_GAPPED_ENV: &str = "IGNI_AIR_GAPPED";

/// Whether an `IGNI_AIR_GAPPED` value turns the mode on. It never turns it off.
pub(crate) fn air_gapped_value(value: Option<&str>) -> bool {
    value
        .map(|v| matches!(v.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(false)
}

/// The settings, as one layer (one file) states them.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct DesktopSettings {
    pub air_gapped: Option<bool>,
    pub checks_disabled: Option<bool>,
    pub update_endpoint: Option<String>,
    /// An interpreter with `ignite-ember` installed, provisioned by whoever
    /// installed the app (`runtime_python`).
    pub runtime_python: Option<String>,
}

impl DesktopSettings {
    /// A layer from a YAML file's text (`config.yaml`, or a managed file
    /// written as YAML). Only top-level keys count.
    pub fn from_yaml(text: &str) -> Self {
        DesktopSettings {
            air_gapped: top_level_bool(text, "air_gapped"),
            checks_disabled: top_level_value(text, "update_check_ttl").map(|_| update_checks_disabled_in(text)),
            update_endpoint: configured_update_endpoint_in(text),
            runtime_python: top_level_string(text, "runtime_python"),
        }
    }

    /// A layer from a JSON object's text (a verified group pack's settings,
    /// or a managed file written as JSON — which is also YAML, but not the
    /// line-per-key YAML the hand parser reads).
    pub fn from_json(text: &str) -> Self {
        let Ok(serde_json::Value::Object(map)) = serde_json::from_str::<serde_json::Value>(text) else {
            return DesktopSettings::default();
        };
        let string = |key: &str| {
            map.get(key)
                .and_then(serde_json::Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_string)
        };
        DesktopSettings {
            air_gapped: map.get("air_gapped").and_then(serde_json::Value::as_bool),
            checks_disabled: map.get("update_check_ttl").map(|v| v.as_i64().is_some_and(|ttl| ttl <= 0)),
            update_endpoint: string("update_endpoint"),
            runtime_python: string("runtime_python"),
        }
    }

    /// Either form, told apart by the first character.
    pub fn from_text(text: &str) -> Self {
        if text.trim_start().starts_with('{') {
            Self::from_json(text)
        } else {
            Self::from_yaml(text)
        }
    }

    /// `self` over `lower`: each setting `self` states wins.
    pub fn over(self, lower: DesktopSettings) -> DesktopSettings {
        DesktopSettings {
            air_gapped: self.air_gapped.or(lower.air_gapped),
            checks_disabled: self.checks_disabled.or(lower.checks_disabled),
            update_endpoint: self.update_endpoint.or(lower.update_endpoint),
            runtime_python: self.runtime_python.or(lower.runtime_python),
        }
    }

    /// Every layer on this machine, merged: managed over group over user.
    pub fn load() -> Self {
        let text = |path: Option<PathBuf>| path.and_then(|p| std::fs::read_to_string(p).ok());
        let home = dirs::home_dir();
        let managed = text(managed_settings_path());
        let user = text(home.as_ref().map(|h| h.join(".igni/config.yaml")));
        let group = home.as_ref().map(|h| verified_group_layer(h, managed.as_deref(), user.as_deref())).unwrap_or_default();
        let layer = |t: Option<&String>| t.map(|t| DesktopSettings::from_text(t)).unwrap_or_default();
        layer(managed.as_ref()).over(group).over(layer(user.as_ref()))
    }
}

/// The organisation's layer, from the signed pack under `home/.igni`, or
/// nothing when there is no pack or it does not verify.
///
/// `api_url` (the pack's issuer) is read from the managed file over the
/// user's, as the backend reads it; the pinned keys from the environment or
/// else the managed file only — a pin the user could write would let them
/// sign their own policy.
fn verified_group_layer(home: &std::path::Path, managed: Option<&str>, user: Option<&str>) -> DesktopSettings {
    let dir = home.join(".igni/group-policy");
    let read = |name: &str| std::fs::read_to_string(dir.join(name)).ok();
    let Some(compact) = read("pack.jws") else {
        return DesktopSettings::default();
    };
    let jwks = read("jwks.json").and_then(|t| serde_json::from_str(&t).ok()).unwrap_or(serde_json::Value::Null);
    let api_url = managed
        .and_then(|t| top_level_string(t, "api_url"))
        .or_else(|| user.and_then(|t| top_level_string(t, "api_url")));
    let env_pins = crate::group_pack::pins_from_env(std::env::var(crate::group_pack::TRUSTED_KEYS_ENV).ok().as_deref());
    let trust = crate::group_pack::Trust {
        api_url,
        subject: std::fs::read_to_string(home.join(".igni/credentials.json"))
            .ok()
            .and_then(|t| crate::group_pack::subject_from_credentials(&t)),
        pins: if env_pins.is_empty() { managed.map(crate::group_pack::pins_from_managed).unwrap_or_default() } else { env_pins },
        min_version: read("pack_state.json")
            .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
            .and_then(|v| v.get("version").and_then(serde_json::Value::as_i64)),
    };
    match crate::group_pack::verify(&compact, &jwks, &trust) {
        Ok(policy) => DesktopSettings::from_json(&crate::group_pack::settings_json(&policy)),
        Err(why) => {
            eprintln!("igni: group policy pack ignored ({why})");
            DesktopSettings::default()
        }
    }
}

/// The managed-settings file, at the path the backend reads
/// (`ember_code.core.paths.managed_policy_dir`).
fn managed_settings_path() -> Option<PathBuf> {
    let dir = if cfg!(target_os = "macos") {
        PathBuf::from("/Library/Application Support/igni")
    } else if cfg!(target_os = "linux") {
        PathBuf::from("/etc/igni")
    } else if cfg!(target_os = "windows") {
        PathBuf::from(std::env::var("PROGRAMDATA").unwrap_or_else(|_| r"C:\ProgramData".to_string())).join("igni")
    } else {
        return None;
    };
    Some(dir.join("managed-settings.yaml"))
}

/// The raw value of a top-level `key:` line, if there is one.
///
/// No trimming of the line: `strip_prefix` on the raw line is what anchors
/// this to the top level. An indented key of the same name belongs to
/// whatever section contains it, and reading it as the top-level setting
/// would let an unrelated block decide where — or whether — the binary is
/// updated, or which interpreter runs the backend.
fn top_level_value<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    let prefix = format!("{key}:");
    text.lines().find_map(|line| line.strip_prefix(prefix.as_str()))
}

fn top_level_bool(text: &str, key: &str) -> Option<bool> {
    let value = top_level_value(text, key)?;
    match value.split('#').next().unwrap_or("").trim().to_ascii_lowercase().as_str() {
        "true" | "yes" | "on" => Some(true),
        "false" | "no" | "off" => Some(false),
        _ => None,
    }
}

/// A string value: quotes stripped, blank read as absent. No `#` splitting —
/// a path or URL may contain one — so a trailing comment on such a line is
/// part of the value.
fn top_level_string(text: &str, key: &str) -> Option<String> {
    let value = top_level_value(text, key)?;
    let value = value.trim().trim_matches('"').trim_matches('\'');
    (!value.is_empty()).then(|| value.to_string())
}

/// Whether `update_check_ttl` in this text turns checks off: `<= 0`, as on
/// the Python side — two implementations of one setting have to agree on the
/// boundary. Present but unreadable leaves checks **enabled**, so a
/// malformed config never silently strands a machine on an old build.
pub(crate) fn update_checks_disabled_in(text: &str) -> bool {
    let Some(value) = top_level_value(text, "update_check_ttl") else {
        return false;
    };
    let value = value.split('#').next().unwrap_or("").trim();
    value.parse::<i64>().map(|ttl| ttl <= 0).unwrap_or(false)
}

/// The `update_endpoint` this text names, if any. Present and blank means
/// "use the compiled default", the same as absent — both are "I have not
/// chosen a mirror". A URL may contain a fragment, so a trailing YAML
/// comment on this key is worth losing to keep that true.
pub(crate) fn configured_update_endpoint_in(text: &str) -> Option<String> {
    top_level_string(text, "update_endpoint")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn layers_merge_in_the_backends_order() {
        let managed = DesktopSettings::from_yaml("runtime_python: /opt/igni/bin/python\n");
        let user = DesktopSettings::from_yaml("runtime_python: /home/me/py\nair_gapped: true\n");
        let merged = managed.over(DesktopSettings::default()).over(user);
        assert_eq!(merged.runtime_python.as_deref(), Some("/opt/igni/bin/python"));
        assert_eq!(merged.air_gapped, Some(true));
    }

    #[test]
    fn runtime_python_is_read_from_yaml_and_json() {
        assert_eq!(
            DesktopSettings::from_text("runtime_python: \"/opt/igni/venv/bin/python\"\n").runtime_python.as_deref(),
            Some("/opt/igni/venv/bin/python")
        );
        assert_eq!(
            DesktopSettings::from_text("{\"runtime_python\": \"C:\\\\igni\\\\python.exe\"}").runtime_python.as_deref(),
            Some(r"C:\igni\python.exe")
        );
        assert_eq!(DesktopSettings::from_text("runtime_python:\n").runtime_python, None);
        assert_eq!(DesktopSettings::from_text("x:\n  runtime_python: /evil\n").runtime_python, None, "nested");
    }

    #[test]
    fn an_unsigned_group_settings_file_is_not_a_layer() {
        let home = std::env::temp_dir().join(format!("igni-desktop-settings-{}", std::process::id()));
        let dir = home.join(".igni/group-policy");
        std::fs::create_dir_all(&dir).unwrap();
        // What the cache used to hold, and a pack that is not signed.
        std::fs::write(dir.join("group_settings.json"), "{\"runtime_python\": \"/evil/python\"}").unwrap();
        assert_eq!(verified_group_layer(&home, None, Some("api_url: https://igni.example.invalid\n")), DesktopSettings::default());
        std::fs::write(dir.join("pack.jws"), "e30.eyJydW50aW1lX3B5dGhvbiI6Ii9ldmlsIn0.").unwrap();
        assert_eq!(verified_group_layer(&home, None, Some("api_url: https://igni.example.invalid\n")), DesktopSettings::default());
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn the_variable_only_turns_air_gap_on() {
        for on in ["1", "true", "TRUE", " yes ", "on"] {
            assert!(air_gapped_value(Some(on)), "{on:?}");
        }
        for off in ["", "0", "false", "no"] {
            assert!(!air_gapped_value(Some(off)), "{off:?}");
        }
        assert!(!air_gapped_value(None));
    }
}
