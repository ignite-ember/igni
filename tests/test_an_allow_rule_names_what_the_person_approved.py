"""An "always allow" rule says what was approved: this call, or calls like it.

When the person picks "Allow" or "Allow similar" in the approval dialog,
:class:`ToolInvocation` writes the rule that is persisted. "Allow" must
name this exact call (``Bash(python3 -m http.server 8000)``) so it does
not widen; "Allow similar" widens by one deliberate step — the binary, the
parent directory, the domain — and to nothing wider when there is no such
step. Rules written by the earlier free functions must still round-trip
byte for byte, so the shapes below are the wire format, not a detail.
"""

from __future__ import annotations

import pytest

from ember_code.core.config.tool_permissions.tool_invocation import ToolInvocation


@pytest.mark.parametrize(
    ("tool", "args", "exact", "similar"),
    [
        (
            "Bash",
            {"args": ["python3", "-m", "http.server", "8000"]},
            "Bash(python3 -m http.server 8000)",
            "Bash(python3 *)",
        ),
        ("Bash", {"command": "  npm run build  "}, "Bash(  npm run build  )", "Bash(npm *)"),
        ("Edit", {"path": "src/app/main.py"}, "Edit(src/app/main.py)", "Edit(src/app/*)"),
        ("Write", {"file_path": "docs/a.md"}, "Write(docs/a.md)", "Write(docs/*)"),
        (
            "WebFetch",
            {"url": "https://example.org/a/b?c=1"},
            "WebFetch(https://example.org/a/b?c=1)",
            "WebFetch(domain:example.org)",
        ),
        ("WebSearch", {"query": "pydantic v2"}, "WebSearch(pydantic v2)", "WebSearch"),
    ],
)
def test_allow_names_the_call_and_allow_similar_widens_one_step(tool, args, exact, similar):
    invocation = ToolInvocation.from_raw(tool, args)

    assert invocation.exact_rule() == exact
    assert invocation.pattern_rule() == similar


def test_a_call_with_no_arguments_is_allowed_by_its_bare_name():
    for raw in (None, {}):
        invocation = ToolInvocation.from_raw("TodoWrite", raw)

        assert invocation.exact_rule() == "TodoWrite"
        assert invocation.pattern_rule() == "TodoWrite"


def test_a_file_at_the_top_level_does_not_widen_to_the_whole_project():
    invocation = ToolInvocation.from_raw("Edit", {"path": "README.md"})

    assert invocation.pattern_rule() == "Edit"


def test_a_blank_command_widens_to_nothing():
    invocation = ToolInvocation.from_raw("Bash", {"command": "   "})

    assert invocation.pattern_rule() == "Bash"


def test_a_url_without_a_host_widens_to_nothing():
    invocation = ToolInvocation.from_raw("WebFetch", {"url": "not-a-url"})

    assert invocation.pattern_rule() == "WebFetch"


def test_unrecognised_arguments_are_named_and_cut_to_a_hundred_characters():
    invocation = ToolInvocation.from_raw("Custom", {"payload": "x" * 300})

    rule = invocation.exact_rule()

    assert rule.startswith("Custom({'payload': 'xxx")
    assert len(rule) == len("Custom()") + 100
    assert invocation.pattern_rule() == "Custom"
