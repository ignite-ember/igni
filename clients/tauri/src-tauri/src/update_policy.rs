//! Where — and whether — the desktop app looks for releases.
//!
//! Three settings decide it, read by `desktop_settings` from the files the
//! backend merges them from:
//!
//! * `update_check_ttl: 0` — no update checks at all (F126).
//! * `update_endpoint` — a release mirror replacing the compiled
//!   `github.com/ignite-ember/igni/releases` (DEC-11).
//! * `air_gapped: true` — the deployment has no route out. The compiled
//!   endpoint is never contacted; the app asks the configured mirror, or
//!   nothing. `IGNI_AIR_GAPPED=1` in the environment sets it too.

use crate::desktop_settings::{air_gapped_value, DesktopSettings, AIR_GAPPED_ENV};

/// What the updater may do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum UpdateSource {
    /// Make no request. Carries the reason, said to whoever asked.
    Off(String),
    /// The endpoint compiled into `tauri.conf.json`.
    Compiled,
    /// A mirror the deployment configured.
    Mirror(String),
}

/// The merged settings and the decision they make.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct UpdatePolicy {
    settings: DesktopSettings,
    air_gapped_by_env: bool,
}

impl UpdatePolicy {
    /// Merge the layers, highest precedence first: managed, group, user.
    /// `env` is the value of `IGNI_AIR_GAPPED`, which can only turn
    /// air-gapped mode on. (`load` merges through `DesktopSettings::load`.)
    #[cfg(test)]
    pub fn from_layers(managed: DesktopSettings, group: DesktopSettings, user: DesktopSettings, env: Option<&str>) -> Self {
        UpdatePolicy::from_merged(managed.over(group).over(user), env)
    }

    /// Settings already merged, and the value of `IGNI_AIR_GAPPED`, which
    /// can only turn air-gapped mode on.
    pub fn from_merged(settings: DesktopSettings, env: Option<&str>) -> Self {
        UpdatePolicy { settings, air_gapped_by_env: air_gapped_value(env) }
    }

    /// Read every layer from where this machine keeps it.
    pub fn load() -> Self {
        UpdatePolicy::from_merged(DesktopSettings::load(), std::env::var(AIR_GAPPED_ENV).ok().as_deref())
    }

    pub fn air_gapped(&self) -> bool {
        self.air_gapped_by_env || self.settings.air_gapped == Some(true)
    }

    /// Whether to ask, and whom.
    pub fn source(&self) -> UpdateSource {
        if self.settings.checks_disabled == Some(true) {
            return UpdateSource::Off("update checks are disabled (update_check_ttl: 0)".to_string());
        }
        match (&self.settings.update_endpoint, self.air_gapped()) {
            (Some(mirror), _) => UpdateSource::Mirror(mirror.clone()),
            (None, true) => UpdateSource::Off(
                "igni is air-gapped (air_gapped: true) and no update_endpoint names a release mirror \
                 inside this network"
                    .to_string(),
            ),
            (None, false) => UpdateSource::Compiled,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn yaml(text: &str) -> DesktopSettings {
        DesktopSettings::from_yaml(text)
    }

    fn none() -> DesktopSettings {
        DesktopSettings::default()
    }

    #[test]
    fn an_ordinary_install_uses_the_compiled_endpoint() {
        assert_eq!(UpdatePolicy::from_layers(none(), none(), none(), None).source(), UpdateSource::Compiled);
    }

    #[test]
    fn air_gapped_without_a_mirror_makes_no_request() {
        let p = UpdatePolicy::from_layers(none(), none(), yaml("air_gapped: true\n"), None);
        assert!(p.air_gapped());
        assert!(matches!(p.source(), UpdateSource::Off(reason) if reason.contains("air_gapped")));
    }

    #[test]
    fn air_gapped_with_a_mirror_asks_only_the_mirror() {
        let p = UpdatePolicy::from_layers(
            none(),
            none(),
            yaml("air_gapped: true\nupdate_endpoint: https://releases.acme.internal/latest.json\n"),
            None,
        );
        assert_eq!(p.source(), UpdateSource::Mirror("https://releases.acme.internal/latest.json".into()));
    }

    #[test]
    fn the_environment_variable_turns_it_on() {
        for on in ["1", "true", "TRUE", " yes ", "on"] {
            let p = UpdatePolicy::from_layers(none(), none(), none(), Some(on));
            assert!(matches!(p.source(), UpdateSource::Off(_)), "{on:?}");
        }
        for off in ["", "0", "false", "no"] {
            let p = UpdatePolicy::from_layers(none(), none(), none(), Some(off));
            assert_eq!(p.source(), UpdateSource::Compiled, "{off:?}");
        }
    }

    #[test]
    fn the_environment_variable_cannot_turn_it_off() {
        let p = UpdatePolicy::from_layers(none(), none(), yaml("air_gapped: true\n"), Some("0"));
        assert!(p.air_gapped());
    }

    #[test]
    fn a_managed_file_wins_over_the_user() {
        let p = UpdatePolicy::from_layers(yaml("air_gapped: true\n"), none(), yaml("air_gapped: false\n"), None);
        assert!(matches!(p.source(), UpdateSource::Off(_)));
    }

    #[test]
    fn a_managed_json_file_is_read() {
        let managed = DesktopSettings::from_text("{\"air_gapped\": true, \"update_endpoint\": \"https://m.internal/l.json\"}");
        let p = UpdatePolicy::from_layers(managed, none(), none(), None);
        assert_eq!(p.source(), UpdateSource::Mirror("https://m.internal/l.json".into()));
    }

    #[test]
    fn a_group_policy_air_gap_applies() {
        let group = DesktopSettings::from_json("{\"air_gapped\": true}");
        let p = UpdatePolicy::from_layers(none(), group, yaml("update_check_ttl: 86400\n"), None);
        assert!(matches!(p.source(), UpdateSource::Off(_)));
    }

    #[test]
    fn a_zero_ttl_is_off_even_with_a_mirror() {
        let p = UpdatePolicy::from_layers(
            none(),
            none(),
            yaml("update_check_ttl: 0\nupdate_endpoint: https://m.internal/l.json\n"),
            None,
        );
        assert!(matches!(p.source(), UpdateSource::Off(reason) if reason.contains("update_check_ttl")));
    }

    #[test]
    fn only_an_explicit_true_is_air_gapped() {
        for text in ["air_gapped: maybe\n", "air_gapped:\n", "  air_gapped: true\n", "air_gapped: false\n"] {
            let p = UpdatePolicy::from_layers(none(), none(), yaml(text), None);
            assert_eq!(p.source(), UpdateSource::Compiled, "{text:?}");
        }
        assert!(UpdatePolicy::from_layers(none(), none(), yaml("air_gapped: true  # no route out\n"), None).air_gapped());
    }

    #[test]
    fn a_garbled_group_cache_is_no_policy() {
        assert_eq!(DesktopSettings::from_json("{truncated"), none());
        assert_eq!(DesktopSettings::from_json("[1, 2]"), none());
    }
}
