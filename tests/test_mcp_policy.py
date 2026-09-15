"""Tests for MCP managed policy — MCPPolicy model and from_managed_settings()."""

import json

import pytest

from ember_code.core.mcp import config as mcp_config
from ember_code.core.mcp.config import MCPPolicy
from ember_code.core.paths import managed_policy_dir


class TestMCPPolicy:
    """Unit tests for the MCPPolicy model."""

    def test_defaults(self):
        policy = MCPPolicy()
        assert policy.required == []
        assert policy.allowed == []
        assert policy.denied == []

    def test_is_denied_exact(self):
        policy = MCPPolicy(denied=["evil-server"])
        assert policy.is_denied("evil-server") is True
        assert policy.is_denied("good-server") is False

    def test_is_denied_glob(self):
        policy = MCPPolicy(denied=["test-*", "*.local"])
        assert policy.is_denied("test-foo") is True
        assert policy.is_denied("test-bar") is True
        assert policy.is_denied("my.local") is True
        assert policy.is_denied("production") is False

    def test_is_allowed_empty_allows_all(self):
        policy = MCPPolicy()
        assert policy.is_allowed("anything") is True

    def test_is_allowed_explicit_list(self):
        policy = MCPPolicy(allowed=["server-a", "server-b"])
        assert policy.is_allowed("server-a") is True
        assert policy.is_allowed("server-b") is True
        assert policy.is_allowed("server-c") is False

    def test_denied_overrides_allowed(self):
        policy = MCPPolicy(allowed=["server-a"], denied=["server-a"])
        assert policy.is_allowed("server-a") is False

    def test_denied_glob_overrides_allowed(self):
        policy = MCPPolicy(allowed=["test-server"], denied=["test-*"])
        assert policy.is_allowed("test-server") is False

    def test_required_field(self):
        policy = MCPPolicy(required=["mandatory-server"])
        assert "mandatory-server" in policy.required


class TestFromManagedSettings:
    """Where MCP policy is read from, and what happens when it is not there.

    These used to patch ``platform.system`` and then substitute the whole
    ``Path`` class with a side_effect that matched on the substring
    ``"EmberCode"``. That machinery existed only because the directory was
    hardcoded in the function body — and it hid the actual bug: MCP read
    a *different* directory from every other managed tier, on every
    platform. A test that fakes the path it expects cannot notice that
    the path is wrong.

    Now the directory comes from
    :func:`ember_code.core.paths.managed_policy_dir`, so pointing these
    at a temp directory is one line, and the shared-directory question is
    asserted where it belongs (``test_context.py``).
    """

    def test_returns_empty_policy_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_config, "managed_policy_dir", lambda: tmp_path)
        assert MCPPolicy.from_managed_settings() == MCPPolicy()

    def test_returns_empty_policy_on_unsupported_platform(self, monkeypatch):
        """No managed directory means no policy — not a policy that
        denies everything, which would lock a user out of their own MCP
        servers on any platform we do not recognise."""
        monkeypatch.setattr(mcp_config, "managed_policy_dir", lambda: None)
        assert MCPPolicy.from_managed_settings() == MCPPolicy()

    def test_loads_policy_from_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_config, "managed_policy_dir", lambda: tmp_path)
        (tmp_path / "managed-settings.json").write_text(
            json.dumps(
                {
                    "mcp": {
                        "required": ["company-server"],
                        "allowed": ["company-server", "vscode"],
                        "denied": ["untrusted-*"],
                    }
                }
            )
        )

        policy = MCPPolicy.from_managed_settings()

        assert policy.required == ["company-server"]
        assert policy.is_allowed("vscode") is True
        assert policy.is_denied("untrusted-foo") is True

    def test_handles_corrupt_json(self, tmp_path, monkeypatch):
        """A malformed managed file must not stop a session starting. It
        yields no policy, which is the same as no file — the alternative
        is a deployment nobody can use until an admin notices."""
        monkeypatch.setattr(mcp_config, "managed_policy_dir", lambda: tmp_path)
        (tmp_path / "managed-settings.json").write_text("not valid json{{{")

        assert MCPPolicy.from_managed_settings() == MCPPolicy()

    def test_handles_missing_mcp_key(self, tmp_path, monkeypatch):
        """The file is shared with other managed settings, so an ``mcp``
        section is optional rather than expected."""
        monkeypatch.setattr(mcp_config, "managed_policy_dir", lambda: tmp_path)
        (tmp_path / "managed-settings.json").write_text(json.dumps({"other_setting": True}))

        assert MCPPolicy.from_managed_settings() == MCPPolicy()

    @pytest.mark.parametrize(
        ("platform", "expected"),
        [
            ("darwin", "/Library/Application Support/igni"),
            ("linux", "/etc/igni"),
        ],
    )
    def test_the_real_directory_per_platform(self, platform, expected, monkeypatch):
        """Asserted against the shared resolver, not against a substring
        of a faked path."""
        monkeypatch.setattr("sys.platform", platform)
        assert str(managed_policy_dir()) == expected

    def test_windows_is_supported(self, monkeypatch):
        """It was not. MCP handled Darwin and Linux and returned an empty
        policy everywhere else, so a Windows admin could not deploy MCP
        policy at all while every other managed tier worked."""
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setenv("PROGRAMDATA", r"C:\TestProgramData")

        root = managed_policy_dir()

        assert root is not None
        assert "igni" in str(root)
