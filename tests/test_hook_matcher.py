"""Tests for the Claude Code-compatible tri-mode hook matcher.

Old behavior: ``re.search`` on everything — bare names like ``Edit``
would substring-match ``Edited`` / ``Edits`` / etc., which is
surprising and diverges from CC.

New behavior: empty or ``"*"`` matches all; alphanumeric (with
optional ``|`` pipe-list) is an EXACT match; anything else falls
through to regex. Malformed regex is treated as no-match.
"""

from __future__ import annotations

from ember_code.core.hooks.executor import HookExecutor, _matcher_matches
from ember_code.core.hooks.schemas import HookDefinition

# ── Helper directly ─────────────────────────────────────────────


def test_empty_matcher_matches_all() -> None:
    assert _matcher_matches("", "Edit") is True
    assert _matcher_matches("", "anything") is True


def test_star_matcher_matches_all() -> None:
    assert _matcher_matches("*", "Edit") is True
    assert _matcher_matches("*", "Read") is True


def test_bare_name_is_exact_match() -> None:
    """The headline behavior change: ``Edit`` matches only ``Edit``,
    not ``Edited`` / ``edit_file`` / ``MultiEdit``."""
    assert _matcher_matches("Edit", "Edit") is True
    assert _matcher_matches("Edit", "Edited") is False
    assert _matcher_matches("Edit", "edit_file") is False
    assert _matcher_matches("Edit", "MultiEdit") is False


def test_pipe_list_matches_any_alternative_exactly() -> None:
    assert _matcher_matches("Edit|Write", "Edit") is True
    assert _matcher_matches("Edit|Write", "Write") is True
    assert _matcher_matches("Edit|Write", "Read") is False
    # Still exact — substring doesn't match.
    assert _matcher_matches("Edit|Write", "Edited") is False
    assert _matcher_matches("Edit|Write", "Writer") is False


def test_pipe_list_three_alternatives() -> None:
    assert _matcher_matches("Bash|Edit|Write", "Bash") is True
    assert _matcher_matches("Bash|Edit|Write", "Read") is False


def test_underscores_in_name_are_part_of_exact() -> None:
    """``edit_file`` is a single alphanumeric identifier (underscore
    is a word char) → exact match, not regex."""
    assert _matcher_matches("edit_file", "edit_file") is True
    assert _matcher_matches("edit_file", "edit_files") is False


def test_anchored_regex_falls_to_regex_mode() -> None:
    """``^Edit$`` has anchors → regex mode (and matches exactly
    via the regex)."""
    assert _matcher_matches("^Edit$", "Edit") is True
    assert _matcher_matches("^Edit$", "Edited") is False


def test_regex_special_chars_engage_regex_mode() -> None:
    """``Edit.*`` is regex → matches ``Edit`` AND ``Edited``."""
    assert _matcher_matches("Edit.*", "Edit") is True
    assert _matcher_matches("Edit.*", "Edited") is True
    assert _matcher_matches("Edit.*", "Read") is False


def test_regex_character_class() -> None:
    assert _matcher_matches("[Ee]dit", "edit") is True
    assert _matcher_matches("[Ee]dit", "Edit") is True
    assert _matcher_matches("[Ee]dit", "Read") is False


def test_malformed_regex_is_no_match() -> None:
    """``(unclosed`` is not a valid regex AND not pure alphanumeric
    (has a paren), so it falls to regex mode where ``re.error``
    gets caught — returns False rather than crashing."""
    assert _matcher_matches("(unclosed", "anything") is False
    assert _matcher_matches("[unclosed", "anything") is False


# ── Through the executor (integration) ───────────────────────────


def test_executor_exact_match_does_not_match_substring() -> None:
    """The behavior-change canary: a hook matching ``"Edit"`` must
    NOT fire on a tool named ``"edit_file"``."""
    hook = HookDefinition(type="command", command="x", matcher="Edit")
    executor = HookExecutor({"PreToolUse": [hook]})
    assert len(executor.get_matching_hooks("PreToolUse", "Edit")) == 1
    assert len(executor.get_matching_hooks("PreToolUse", "edit_file")) == 0
    assert len(executor.get_matching_hooks("PreToolUse", "MultiEdit")) == 0


def test_executor_pipe_list_still_works() -> None:
    """The pre-existing pipe-list case stays green — important
    because the existing test suite asserts this."""
    hook = HookDefinition(type="command", command="x", matcher="Write|Edit")
    executor = HookExecutor({"PreToolUse": [hook]})
    assert len(executor.get_matching_hooks("PreToolUse", "Write")) == 1
    assert len(executor.get_matching_hooks("PreToolUse", "Edit")) == 1
    assert len(executor.get_matching_hooks("PreToolUse", "Read")) == 0


def test_executor_star_matches_all() -> None:
    hook = HookDefinition(type="command", command="x", matcher="*")
    executor = HookExecutor({"PreToolUse": [hook]})
    assert len(executor.get_matching_hooks("PreToolUse", "Anything")) == 1
    assert len(executor.get_matching_hooks("PreToolUse", "Bash")) == 1


def test_executor_empty_matcher_matches_all() -> None:
    """Empty matcher (the default) keeps "match every invocation of
    this event" — same behavior as before."""
    hook = HookDefinition(type="command", command="x")  # matcher=""
    executor = HookExecutor({"PreToolUse": [hook]})
    assert len(executor.get_matching_hooks("PreToolUse", "Anything")) == 1


def test_executor_regex_with_anchor() -> None:
    hook = HookDefinition(type="command", command="x", matcher="^edit_")
    executor = HookExecutor({"PreToolUse": [hook]})
    assert len(executor.get_matching_hooks("PreToolUse", "edit_file")) == 1
    assert len(executor.get_matching_hooks("PreToolUse", "save_edit")) == 0
