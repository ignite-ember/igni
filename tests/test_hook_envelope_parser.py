"""Tests for ``_hook_result_from_envelope`` — the universal
hook-return translator.

The function bridges the ``mcp_tool`` handler's structured
Python return values into the same ``HookResult`` shape the
``command`` handler produces from stdout-JSON. CC-compatible
envelope keys:

  * ``continue: false`` → block the tool
  * ``systemMessage: "..."`` → carry an explanation
  * ``hookSpecificOutput.permissionDecision: "allow|deny|ask|defer"``
    → structured permission verdict
  * bare ``permissionDecision`` → legacy fallback

The branches are small but the **precedence** between the two
permissionDecision shapes is the silent-regression risk. Pin
each one.
"""

from __future__ import annotations

from ember_code.core.hooks.executor import _hook_result_from_envelope
from ember_code.core.hooks.schemas import HookResult


class TestDictWithoutPermission:
    """Plain envelope without a permission verdict — just the
    legacy continue/message contract."""

    def test_empty_dict_defaults_to_continue_true(self):
        # ``{}`` is "no opinion" → defaults to continue=True,
        # empty message, empty permission_decision. The hook
        # didn't block and didn't speak.
        result = _hook_result_from_envelope({})
        assert isinstance(result, HookResult)
        assert result.should_continue is True
        assert result.message == ""
        assert result.permission_decision == ""

    def test_continue_false_blocks(self):
        # The classic block — tool execution halts here.
        result = _hook_result_from_envelope({"continue": False})
        assert result.should_continue is False
        assert result.message == ""

    def test_continue_true_explicit(self):
        result = _hook_result_from_envelope({"continue": True})
        assert result.should_continue is True

    def test_system_message_propagates(self):
        # Carries human-readable context for the agent. The
        # ``message`` field is what the agent sees in the
        # blocked-tool reply.
        result = _hook_result_from_envelope(
            {"continue": False, "systemMessage": "blocked by policy"}
        )
        assert result.should_continue is False
        assert result.message == "blocked by policy"


class TestPermissionDecisionEnvelope:
    """The CC-compatible structured-permission shape. The hook
    expresses one of four values which the evaluator routes
    against the existing permission pipeline."""

    def test_hook_specific_output_allow(self):
        # The recommended shape — nested under
        # ``hookSpecificOutput`` so future per-event fields can
        # share the namespace without colliding.
        result = _hook_result_from_envelope({"hookSpecificOutput": {"permissionDecision": "allow"}})
        assert result.permission_decision == "allow"
        # ``should_continue`` defaults to True — the verdict is
        # the load-bearing signal, not the legacy bool.
        assert result.should_continue is True

    def test_hook_specific_output_deny(self):
        result = _hook_result_from_envelope({"hookSpecificOutput": {"permissionDecision": "deny"}})
        assert result.permission_decision == "deny"

    def test_hook_specific_output_ask(self):
        # ``ask`` means "kick to HITL". Pin separately because
        # this routes differently from deny.
        result = _hook_result_from_envelope({"hookSpecificOutput": {"permissionDecision": "ask"}})
        assert result.permission_decision == "ask"

    def test_bare_permission_decision_fallback(self):
        # Some hooks emit the verdict at the top level (legacy
        # shape from before the ``hookSpecificOutput`` wrapper
        # was documented). The fallback keeps them working.
        result = _hook_result_from_envelope({"permissionDecision": "deny"})
        assert result.permission_decision == "deny"


class TestPermissionDecisionPrecedence:
    """When BOTH shapes are present, the nested one wins. The
    wrapped form is the documented version; the bare is legacy
    fallback. Drift on this precedence is the silent-regression
    risk — a hook author might rely on the wrapped shape, then
    a refactor flips precedence and the bare key overrides
    silently."""

    def test_hook_specific_output_wins_over_bare(self):
        # Nested decides; bare is ignored.
        result = _hook_result_from_envelope(
            {
                "hookSpecificOutput": {"permissionDecision": "allow"},
                "permissionDecision": "deny",
            }
        )
        assert result.permission_decision == "allow"

    def test_empty_nested_falls_back_to_bare(self):
        # If the nested wrapper exists but its decision is
        # empty/missing, the bare fallback kicks in. (Useful
        # for hooks that share the wrapper across event types
        # but only some emit a decision.)
        result = _hook_result_from_envelope(
            {
                "hookSpecificOutput": {},
                "permissionDecision": "ask",
            }
        )
        assert result.permission_decision == "ask"

    def test_non_dict_hook_specific_output_falls_back_to_bare(self):
        # Defensive — a hook returning ``hookSpecificOutput:
        # "some-string"`` instead of a dict. The bare fallback
        # still catches the top-level decision.
        result = _hook_result_from_envelope(
            {
                "hookSpecificOutput": "garbage",
                "permissionDecision": "deny",
            }
        )
        assert result.permission_decision == "deny"


class TestNonDictReturns:
    """Hooks that return None / str / list / etc. The contract:
    treat as non-blocking + stringify the value into ``message``
    so the agent still sees the MCP tool's response payload."""

    def test_none_yields_continue_true_empty_message(self):
        # A hook returning ``None`` is the "all clear" signal.
        # No message, no verdict, just keep going.
        result = _hook_result_from_envelope(None)
        assert result.should_continue is True
        assert result.message == ""
        assert result.permission_decision == ""

    def test_string_passes_through_as_message(self):
        # The mcp_tool handler returns the tool's raw output.
        # We don't want to discard it — surface as message so
        # the agent gets the payload.
        result = _hook_result_from_envelope("OK from mcp tool")
        assert result.should_continue is True
        assert result.message == "OK from mcp tool"

    def test_list_stringified_into_message(self):
        # A list return (e.g. structured data from a knowledge-
        # base lookup tool). Stringified rather than discarded.
        result = _hook_result_from_envelope([1, 2, "three"])
        assert result.should_continue is True
        assert "1" in result.message and "three" in result.message

    def test_integer_stringified(self):
        # Numbers, bools, etc. — same path.
        result = _hook_result_from_envelope(42)
        assert result.should_continue is True
        assert result.message == "42"


class TestEnvelopeRobustness:
    def test_non_bool_continue_coerced(self):
        # ``continue: 0`` should be falsy. The bool() coercion
        # is explicit in the source, so 0/1 work like JSON's
        # numeric-bool convention.
        result = _hook_result_from_envelope({"continue": 0})
        assert result.should_continue is False
        result = _hook_result_from_envelope({"continue": 1})
        assert result.should_continue is True

    def test_none_message_field_does_not_leak_string_none(self):
        # ``systemMessage: None`` must NOT render as the
        # literal "None" in the agent's view. The ``or ""``
        # guard catches it.
        result = _hook_result_from_envelope({"continue": False, "systemMessage": None})
        assert result.message == ""

    def test_none_permission_decision_field_safe(self):
        # Same defensive guard for the permission decision.
        result = _hook_result_from_envelope({"permissionDecision": None})
        assert result.permission_decision == ""
