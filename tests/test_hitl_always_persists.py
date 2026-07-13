"""Regression: HITL "Always allow" / "Allow similar" / "Deny"
must persist a rule.

The bug this file guards: the web ``HitlDialog`` shipped
``{action: "confirm", choice: "always"}`` on the wire but the
BE's ``resolve_hitl_batch`` only read ``d.action``. So "Always
allow" did the exact same thing as "Allow once": confirmed
this call, persisted nothing, re-prompted on the next call.
Only the TUI's ``hitl_handler`` persisted, because it called
``save_permission_rule`` client-side before firing the resolve.

The fix moved the persistence into ``resolve_hitl_batch`` so
every client (web, VSCode, JetBrains, TUI) shares one code
path. Web + friends: no client change needed; the choice
they already send is now honored. TUI: still calls
``save_permission_rule`` explicitly — that's idempotent
alongside the server-side save, so the double-save is a
no-op (``save_rule`` dedupes across levels).

These tests pin the four choices land as expected:
- ``always`` → specific rule at ``allow`` level
- ``similar`` → pattern rule at ``allow`` level
- ``deny`` → specific rule at ``deny`` level  (reject action)
- ``once`` → no rule persisted (the confirm/reject happened
  in-memory only, nothing sticky).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from ember_code.backend.server import BackendServer
from ember_code.core.config.permission_eval import PermissionDecision, PermissionEvaluator


def _make_server(project_dir: Path, with_evaluator: bool = False) -> BackendServer:
    """Build a BackendServer shell that can call
    ``_maybe_persist_choice``. Skips Agno / persistence init
    — we only need ``self._session.project_dir`` for the
    ToolPermissions constructor.

    ``with_evaluator=True`` attaches a real ``PermissionEvaluator``
    to ``self._session.permission_evaluator`` so tests can verify
    the in-memory patch that stops the next-call re-prompt loop."""
    server = BackendServer.__new__(BackendServer)
    evaluator = PermissionEvaluator.from_strings() if with_evaluator else None
    server._session = SimpleNamespace(
        project_dir=project_dir,
        permission_evaluator=evaluator,
    )
    return server


def _requirement_for(tool_name: str, tool_args: dict) -> MagicMock:
    """Build a fake requirement with a ``tool_execution`` sub-
    object shaped like Agno's. ``_maybe_persist_choice`` reads
    ``req.tool_execution.tool_name`` + ``.tool_args``."""
    req = MagicMock()
    req.tool_execution = SimpleNamespace(tool_name=tool_name, tool_args=tool_args)
    return req


def _load_saved_rules(project_dir: Path) -> dict[str, list[str]]:
    """Return the ``permissions`` block from settings.local.json,
    or an empty dict if the file wasn't written."""
    path = project_dir / ".ember" / "settings.local.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get("permissions", {})


class TestAlwaysAllowPersists:
    def test_always_writes_specific_allow_rule(self, tmp_path: Path) -> None:
        # "Always allow" for ``run_shell_command(python3 -m http.server 8000)``
        # should persist as an allow rule keyed to that exact
        # command. The user's shell command should never prompt
        # again on this project.
        server = _make_server(tmp_path)
        req = _requirement_for("run_shell_command", {"command": "python3 -m http.server 8000"})
        server._maybe_persist_choice(SimpleNamespace(choice="always"), req)

        saved = _load_saved_rules(tmp_path)
        assert "Bash(python3 -m http.server 8000)" in saved.get("allow", []), (
            f"expected Bash allow rule; saved={saved}. "
            "If this fails, the web dialog's 'Always allow' "
            "silently degrades to 'Allow once' — the same v0.8.1 "
            "regression that made users re-approve every shell "
            "call."
        )

    def test_similar_writes_pattern_allow_rule(self, tmp_path: Path) -> None:
        # "Allow similar" should broaden to the command family.
        # For a shell command, that's the leading token followed
        # by a space-star fnmatch pattern: ``Bash(python3 *)``.
        # The space-star form (not ``python3:*``) is what the
        # session's PermissionEvaluator can actually match against
        # the raw command string — see build_pattern_rule for the
        # matcher-mismatch bug this format guards.
        server = _make_server(tmp_path)
        req = _requirement_for("run_shell_command", {"command": "python3 -m http.server 8000"})
        server._maybe_persist_choice(SimpleNamespace(choice="similar"), req)
        saved = _load_saved_rules(tmp_path)
        assert "Bash(python3 *)" in saved.get("allow", [])

    def test_once_persists_nothing(self, tmp_path: Path) -> None:
        # "Allow once" is per-invocation; no sticky rule.
        server = _make_server(tmp_path)
        req = _requirement_for("run_shell_command", {"command": "ls"})
        server._maybe_persist_choice(SimpleNamespace(choice="once"), req)

        assert _load_saved_rules(tmp_path) == {}, "'once' must not leak a persisted rule"

    def test_deny_writes_specific_deny_rule(self, tmp_path: Path) -> None:
        # "Deny" (from the reject branch) writes a persistent
        # deny that blocks the same command next time.
        server = _make_server(tmp_path)
        req = _requirement_for("run_shell_command", {"command": "rm -rf /"})
        server._maybe_persist_choice(SimpleNamespace(choice="deny"), req)

        saved = _load_saved_rules(tmp_path)
        assert "Bash(rm -rf /)" in saved.get("deny", [])

    def test_missing_tool_execution_is_noop(self, tmp_path: Path) -> None:
        # Older Agno versions might not populate ``tool_execution``
        # on the requirement — the helper must degrade
        # gracefully rather than raise inside the resolve loop.
        server = _make_server(tmp_path)
        req = SimpleNamespace(tool_execution=None)
        server._maybe_persist_choice(SimpleNamespace(choice="always"), req)
        assert _load_saved_rules(tmp_path) == {}

    def test_unknown_choice_is_noop(self, tmp_path: Path) -> None:
        # A client sending an unrecognised choice string
        # shouldn't accidentally persist under some default
        # level.
        server = _make_server(tmp_path)
        req = _requirement_for("run_shell_command", {"command": "ls"})
        server._maybe_persist_choice(SimpleNamespace(choice="banana"), req)
        assert _load_saved_rules(tmp_path) == {}


class TestInMemoryEvaluatorPatch:
    """The v0.8.2 fix has two halves: (a) write the rule to disk
    so it survives a restart, (b) patch the session's live
    ``PermissionEvaluator`` so the NEXT tool call in the current
    session short-circuits before firing another HITL dialog.

    Half (a) alone is what made the v0.8.1 fix insufficient: the
    settings file was updated but ``session.permission_evaluator``
    is built once at startup, so the running session still saw an
    empty allow list and re-prompted forever. These tests pin
    half (b) — the ``.allow`` / ``.deny`` lists on the live
    evaluator get the new rule immediately."""

    def test_always_appends_to_evaluator_allow(self, tmp_path: Path) -> None:
        server = _make_server(tmp_path, with_evaluator=True)
        req = _requirement_for("run_shell_command", {"command": "python3 -m http.server 8000"})
        # Before: evaluator hasn't seen this command → DEFER.
        # (DEFER surfaces as the requirement being deferred to the
        # user prompt path — i.e. dialog fires.)
        evaluator = server._session.permission_evaluator
        assert (
            evaluator.evaluate("run_shell_command", {"command": "python3 -m http.server 8000"})
            is PermissionDecision.DEFER
        )

        server._maybe_persist_choice(SimpleNamespace(choice="always"), req)

        # After: same evaluator, same call — must resolve to ALLOW
        # without needing a rebuild. If this fails, the user's
        # exact reported bug reproduces: dialog appears, "Always
        # allow" is clicked, next call re-prompts because the
        # in-memory evaluator never learned about the rule.
        assert (
            evaluator.evaluate("run_shell_command", {"command": "python3 -m http.server 8000"})
            is PermissionDecision.ALLOW
        )

    def test_similar_pattern_matches_family_via_evaluator(self, tmp_path: Path) -> None:
        server = _make_server(tmp_path, with_evaluator=True)
        req = _requirement_for("run_shell_command", {"command": "python3 -m http.server 8000"})
        server._maybe_persist_choice(SimpleNamespace(choice="similar"), req)

        evaluator = server._session.permission_evaluator
        # Different command, same family (leading ``python3``) —
        # the pattern rule should cover it. This is the whole
        # point of "Allow similar": one prompt, family whitelisted.
        assert (
            evaluator.evaluate("run_shell_command", {"command": "python3 -c 'print(1)'"})
            is PermissionDecision.ALLOW
        )

    def test_deny_appends_to_evaluator_deny(self, tmp_path: Path) -> None:
        server = _make_server(tmp_path, with_evaluator=True)
        req = _requirement_for("run_shell_command", {"command": "rm -rf /"})
        server._maybe_persist_choice(SimpleNamespace(choice="deny"), req)

        evaluator = server._session.permission_evaluator
        assert (
            evaluator.evaluate("run_shell_command", {"command": "rm -rf /"})
            is PermissionDecision.DENY
        )

    def test_missing_evaluator_is_noop(self, tmp_path: Path) -> None:
        # Older session shapes that don't expose
        # ``permission_evaluator`` mustn't crash the resolve loop.
        server = _make_server(tmp_path, with_evaluator=False)
        req = _requirement_for("run_shell_command", {"command": "ls"})
        server._maybe_persist_choice(SimpleNamespace(choice="always"), req)  # must not raise
        assert "Bash(ls)" in _load_saved_rules(tmp_path).get("allow", [])


class TestFuncNameCanonicalization:
    """Agno passes internal function names (``run_shell_command``)
    but persisted rules use canonical tool names (``Bash``) so
    the settings file reads the way a user would write it by
    hand. Pin the mapping through the ``FUNC_TO_TOOL`` alias."""

    def test_file_read_persists_as_read_rule(self, tmp_path: Path) -> None:
        server = _make_server(tmp_path)
        req = _requirement_for("read_file", {"file_path": "src/main.py"})
        server._maybe_persist_choice(SimpleNamespace(choice="always"), req)

        saved = _load_saved_rules(tmp_path)
        assert "Read(src/main.py)" in saved.get("allow", [])

    def test_web_fetch_similar_uses_domain(self, tmp_path: Path) -> None:
        # "Allow similar" for a web fetch broadens to the domain.
        server = _make_server(tmp_path)
        req = _requirement_for("web_fetch", {"url": "https://github.com/anthropics/claude-code"})
        server._maybe_persist_choice(SimpleNamespace(choice="similar"), req)

        saved = _load_saved_rules(tmp_path)
        assert "WebFetch(domain:github.com)" in saved.get("allow", [])
