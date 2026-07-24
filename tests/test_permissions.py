"""Tests for the ``core/config/permissions/`` package.

The pre-refactor tests poked private attributes (``guard.allowlist``,
``guard._is_protected_path``, ``guard._is_in_allowlist``, etc.). The
new tests target the collaborators directly:

    * :class:`AllowlistStore` for persistence and matching
    * :class:`PermissionPolicy` for the pure decision layer
    * :class:`PermissionGuard` for the composed public API
"""

import pytest
from rich.console import Console

from ember_code.core.config.permissions import (
    AllowlistPattern,
    AllowlistStore,
    ApprovalPrompt,
    DecisionSource,
    PermissionCategory,
    PermissionGuard,
    PermissionPolicy,
    PermissionRequest,
    SessionApprovalCache,
)
from ember_code.core.config.settings import PermissionsConfig, Settings


class _NoopPrompt:
    """Stub :class:`ApprovalPrompt` that fails loudly if invoked.

    Every ``allow`` / ``deny`` -level test below asserts that the
    interactive prompt is NEVER reached — the policy layer should
    settle the verdict on its own.
    """

    def ask(self, request):  # noqa: D401 - trivial
        raise AssertionError(
            f"Interactive prompt should not fire for {request.category}:{request.value}"
        )


def _guard_with_stub_prompt(settings: Settings, tmp_path) -> PermissionGuard:
    """Build a ``PermissionGuard`` whose prompt raises on invocation.

    Injects the stub through the public ``prompt=`` kwarg on
    :class:`PermissionGuard` — no reach into a private slot.
    """
    return PermissionGuard(
        settings,
        permissions_path=tmp_path / "permissions.yaml",
        prompt=_NoopPrompt(),
    )


class TestPermissionGuard:
    @pytest.fixture
    def guard(self, tmp_path):
        """Guard with all-allow permissions — no prompt should ever fire."""
        settings = Settings(
            permissions=PermissionsConfig(
                file_read="allow",
                file_write="allow",
                shell_execute="allow",
            )
        )
        return _guard_with_stub_prompt(settings, tmp_path)

    @pytest.fixture
    def strict_guard(self, tmp_path):
        """Guard with deny permissions — no prompt should ever fire."""
        settings = Settings(
            permissions=PermissionsConfig(
                file_read="deny",
                file_write="deny",
                shell_execute="deny",
            )
        )
        return _guard_with_stub_prompt(settings, tmp_path)

    def test_file_read_allow(self, guard):
        assert guard.check_file_read("any_file.py") is True

    def test_file_read_deny(self, strict_guard):
        assert strict_guard.check_file_read("any_file.py") is False

    def test_file_write_allow(self, guard):
        assert guard.check_file_write("any_file.py") is True

    def test_file_write_deny(self, strict_guard):
        assert strict_guard.check_file_write("any_file.py") is False

    def test_shell_execute_allow(self, guard):
        assert guard.check_shell_execute("ls -la") is True

    def test_shell_execute_deny(self, strict_guard):
        assert strict_guard.check_shell_execute("ls -la") is False

    def test_protected_path_blocks_write(self, tmp_path):
        settings = Settings(permissions=PermissionsConfig(file_write="allow"))
        guard = _guard_with_stub_prompt(settings, tmp_path)
        assert guard.check_file_write("regular.py") is True
        assert guard.check_file_write("secrets.json") is False
        assert guard.check_file_write("credentials.yaml") is False
        assert guard.check_file_write("server.pem") is False
        assert guard.check_file_write("private.key") is False

    def test_blocked_command(self, tmp_path):
        settings = Settings(permissions=PermissionsConfig(shell_execute="allow"))
        guard = _guard_with_stub_prompt(settings, tmp_path)
        assert guard.check_shell_execute("rm -rf /") is False

    def test_require_confirmation_beats_allow_level(self, tmp_path):
        """Regression guard for synthesis note 3: ``shell_execute='allow'``
        must NOT let ``git push`` short-circuit past the confirmation
        prompt."""
        settings = Settings(permissions=PermissionsConfig(shell_execute="allow"))
        guard = PermissionGuard(settings, permissions_path=tmp_path / "permissions.yaml")
        # If require_confirmation is honoured, evaluate returns
        # verdict=DEFER and the guard routes into the prompt. Inject
        # a recording prompt via the public ``prompt=`` kwarg on
        # PermissionGuard so we don't touch the private slot.
        called: list[PermissionRequest] = []

        from ember_code.core.config.permissions.schemas import DecisionVerdict

        class _Recorder:
            def ask(self, request):
                called.append(request)
                from ember_code.core.config.permissions import GuardDecision

                return GuardDecision(
                    allowed=False,
                    verdict=DecisionVerdict.DENY,
                    reason="stub",
                    source=DecisionSource.USER,
                )

        recorder = _Recorder()
        guard = PermissionGuard(settings, permissions_path=tmp_path / "p.yaml", prompt=recorder)
        assert guard.check_shell_execute("git push") is False
        assert len(called) == 1
        assert called[0].value == "git push"

    def test_permissions_path_property(self, tmp_path):
        """``guard.permissions_path`` still points at the on-disk file
        even though it's now sourced from the injected store."""
        settings = Settings()
        target = tmp_path / "permissions.yaml"
        guard = PermissionGuard(settings, permissions_path=target)
        assert guard.permissions_path == target

    def test_allowlist_pattern_derivation(self):
        """Pattern-derivation heuristic lives on :class:`AllowlistPattern`."""
        assert AllowlistPattern.from_value("npm test").pattern == "npm *"
        assert AllowlistPattern.from_value("pytest tests/").pattern == "pytest *"
        assert AllowlistPattern.from_value("src/auth.py").pattern == "src/*"
        assert AllowlistPattern.from_value("standalone").pattern == "standalone"


class TestPermissionPolicy:
    """Direct tests for the pure decision layer."""

    def _policy(self, settings: Settings, tmp_path) -> PermissionPolicy:
        store = AllowlistStore(tmp_path / "permissions.yaml")
        return PermissionPolicy(settings, store)

    def test_is_protected_path(self, tmp_path):
        policy = self._policy(Settings(), tmp_path)
        assert policy.is_protected_path(".env") is True
        assert policy.is_protected_path(".env.production") is True
        assert policy.is_protected_path("server.pem") is True
        assert policy.is_protected_path("my.key") is True
        assert policy.is_protected_path("credentials.json") is True
        assert policy.is_protected_path("secrets.yaml") is True
        assert policy.is_protected_path("regular_file.py") is False

    def test_is_blocked_command(self, tmp_path):
        policy = self._policy(Settings(), tmp_path)
        assert policy.is_blocked_command("rm -rf /") is True
        assert policy.is_blocked_command(":(){ :|:& };:") is True
        assert policy.is_blocked_command("ls -la") is False

    def test_requires_confirmation(self, tmp_path):
        policy = self._policy(Settings(), tmp_path)
        assert policy.requires_confirmation("git push") is True
        assert policy.requires_confirmation("git push --force") is True
        assert policy.requires_confirmation("ls") is False

    def test_unrecognised_level_falls_back_to_ask(self, tmp_path):
        """User typo like ``file_write: aloow`` must NOT crash — it
        gets logged and treated as 'ask'. The load-bearing behaviour
        is the fallback; the log is a diagnostic aid whose visibility
        is subject to other tests' logger configuration."""
        settings = Settings(permissions=PermissionsConfig(file_write="aloow"))
        policy = self._policy(settings, tmp_path)
        from ember_code.core.config.permissions import PermissionLevel

        assert policy.level_for(PermissionCategory.FILE_WRITE) is PermissionLevel.ASK

    def test_evaluate_allow(self, tmp_path):
        settings = Settings(permissions=PermissionsConfig(file_read="allow"))
        policy = self._policy(settings, tmp_path)
        decision = policy.evaluate(
            PermissionRequest(
                category=PermissionCategory.FILE_READ,
                value="foo.py",
                description="Read file: foo.py",
            )
        )
        assert decision.allowed is True
        assert decision.source is DecisionSource.POLICY


class TestAllowlistStore:
    def test_matches_after_add(self, tmp_path):
        store = AllowlistStore(tmp_path / "permissions.yaml")
        store.add(PermissionCategory.FILE_WRITE, AllowlistPattern(pattern="src/*"))
        store.add(PermissionCategory.FILE_WRITE, AllowlistPattern(pattern="tests/*"))

        assert store.matches(PermissionCategory.FILE_WRITE, "src/main.py") is True
        assert store.matches(PermissionCategory.FILE_WRITE, "tests/test_x.py") is True
        assert store.matches(PermissionCategory.FILE_WRITE, "config/secret.yaml") is False

    def test_add_persists_to_disk(self, tmp_path):
        path = tmp_path / "permissions.yaml"
        store = AllowlistStore(path)
        store.add(PermissionCategory.SHELL_EXECUTE, AllowlistPattern(pattern="npm *"))

        # Fresh store instance reads back the saved patterns.
        reopened = AllowlistStore(path)
        assert reopened.matches(PermissionCategory.SHELL_EXECUTE, "npm test") is True

    def test_legacy_format_migrates(self, tmp_path):
        """Old ``{allowlist: {file_write: [raw string]}}`` files must
        load without crashing."""
        path = tmp_path / "permissions.yaml"
        path.write_text("allowlist:\n  file_write:\n    - 'src/*'\n    - 'tests/*'\n")
        store = AllowlistStore(path)
        assert store.matches(PermissionCategory.FILE_WRITE, "src/main.py") is True
        assert store.matches(PermissionCategory.FILE_WRITE, "tests/x.py") is True


class TestSessionApprovalCache:
    def test_remember_and_contains(self):
        cache = SessionApprovalCache()
        assert cache.contains(PermissionCategory.FILE_READ, "foo.py") is False
        cache.remember(PermissionCategory.FILE_READ, "foo.py")
        assert cache.contains(PermissionCategory.FILE_READ, "foo.py") is True
        # Different category with same value is distinct.
        assert cache.contains(PermissionCategory.FILE_WRITE, "foo.py") is False


class TestApprovalPromptWiring:
    """Smoke tests that the injected ``Console`` is honoured (kills
    the old module-level singleton)."""

    def test_console_is_injected(self, tmp_path):
        console = Console(record=True, force_terminal=False)
        store = AllowlistStore(tmp_path / "permissions.yaml")
        cache = SessionApprovalCache()
        prompt = ApprovalPrompt(console, cache, store)
        # Nothing to assert on the interactive path without a stdin
        # stub, but we can verify the injected console is retained.
        assert prompt._console is console
