"""Tool output is wrapped as untrusted data, but only external content is.

The wrapper is the runtime half of the prompt-injection defense: the model,
trained on the same delimiter in the SFT corpus, reads anything inside as data
and never obeys instructions there. igni's own control tools (plan mode, todo,
schedule, the loop) return guidance the model is MEANT to act on and must not be
quarantined — this pins that split, plus idempotency and the nonce.
"""

from __future__ import annotations

from ember_code.core.hooks.untrusted import should_wrap, wrap_untrusted


def test_wraps_with_a_nonce_on_both_tags():
    out = wrap_untrusted("secret contents")
    assert out.startswith("<untrusted_tool_output nonce=")
    assert out.rstrip().endswith(">")
    assert "secret contents" in out
    # same nonce opens and closes
    nonce = out.split("nonce=", 1)[1].split(">", 1)[0]
    assert out.count(f"nonce={nonce}") == 2


def test_is_idempotent():
    once = wrap_untrusted("x")
    assert wrap_untrusted(once) == once  # already wrapped → unchanged


def test_external_content_tools_are_wrapped():
    for name in ("read_file", "run_shell_command", "fetch_url", "fetch_json",
                 "spawn_agent", "spawn_team", "grep", "web_search"):
        assert should_wrap(name), name


def test_control_tools_are_not_wrapped():
    # these return guidance/confirmations the model should act on
    for name in ("enter_plan_mode", "exit_plan_mode", "todo_write", "schedule_task",
                 "create_file", "edit_file", "loop_start", "visualize"):
        assert not should_wrap(name), name
