"""``igni -m`` exits non-zero when its turn did not happen.

Seen running igni end to end against the real igni-1.0-4b: a run that
stopped at a confirmation nobody headless can answer, and a run that found
no model, both printed an error and exited 0 — so a script, a CI job or a
scheduler driving ``igni -m`` took them for done. A non-streamed run does
not raise when it fails either: Agno hands back ``status=error`` with the
error as its content, which was printed as the assistant's answer.

The turn runs through the real :class:`SessionMessageHandler` and
:class:`SingleMessageRun`; only the session around them is a double.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from agno.agent import Agent

from ember_code.core.config.null_model import NoModelConfigured
from ember_code.core.config.settings import Settings
from ember_code.core.guardrails.runner import GuardrailRunner
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.session.message_handler import SessionMessageHandler
from ember_code.core.session.runner import run_single_message
from ember_code.core.session.single_message_run import SingleMessageRun


class _Team:
    """What ``arun`` hands back, without a model behind it."""

    def __init__(self, response: Any) -> None:
        self._response = response

    async def arun(self, *_: Any, **__: Any) -> Any:
        return self._response


def _session(team: Any) -> MagicMock:
    display = MagicMock()
    handler = SessionMessageHandler(
        hook_executor=HookExecutor({}),
        audit=MagicMock(),
        display=display,
        guardrail_runner=GuardrailRunner(Settings()),
        team_ref=lambda: team,
        pending_reminders_drain=lambda: [],
        compact_hook=AsyncMock(return_value=False),
        ensure_mcp=AsyncMock(),
        session_id_ref=lambda: "s1",
        context_window_ref=lambda: 100_000,
        latch_input_tokens=lambda _tokens: None,
    )
    session = MagicMock()
    session.session_id = "s1"
    session.settings.models.default = "igni-1.0-4b"
    session.hook_executor = HookExecutor({})
    session.display = display
    session.plan_store = None
    session.handle_message = handler.handle
    type(session).last_turn_failure = PropertyMock(side_effect=lambda: handler.last_failure)
    return session


async def _run(team: Any) -> tuple[int, MagicMock]:
    session = _session(team)
    with patch("ember_code.core.session.session_run.Session", return_value=session):
        try:
            await run_single_message(MagicMock(), "change config.toml")
        except SystemExit as exit_:
            return int(exit_.code or 0), session.display
    return 0, session.display


async def test_a_run_that_found_no_model_exits_non_zero():
    code, display = await _run(Agent(model=NoModelConfigured()))

    assert code == SingleMessageRun.FAILED_EXIT_CODE != 0
    (error,) = display.print_error.call_args.args
    assert "No model configured" in error
    display.print_response.assert_not_called()


async def test_a_run_stopped_at_a_confirmation_exits_non_zero():
    paused = SimpleNamespace(
        status="paused",
        tools=[SimpleNamespace(tool_name="run_shell_command", is_paused=True)],
        content="I have tools to execute, but I need confirmation.",
    )

    code, display = await _run(_Team(paused))

    assert code != 0
    assert "Run paused for confirmation" in display.print_error.call_args.args[0]
    display.print_response.assert_not_called()


async def test_a_failed_run_is_an_error_not_a_reply():
    failed = SimpleNamespace(status="error", content="The model provider refused the request.")

    code, display = await _run(_Team(failed))

    assert code != 0
    assert "The model provider refused the request." in display.print_error.call_args.args[0]
    display.print_response.assert_not_called()


async def test_a_run_that_answered_exits_zero():
    done = SimpleNamespace(status="completed", content="config.toml now has debug = true.")

    code, display = await _run(_Team(done))

    assert code == 0
    display.print_response.assert_called_once_with("config.toml now has debug = true.")
    display.print_error.assert_not_called()


async def test_a_bare_help_prints_the_commands():
    """B35: a bare ``/help`` opens a panel in the clients, so its result has
    no content, and ``igni -m /help`` printed nothing."""
    session = _session(_Team(None))
    with patch("ember_code.core.session.session_run.Session", return_value=session):
        await run_single_message(MagicMock(), "/help")

    printed = "\n".join(c.args[0] for c in session.display.print_markdown.call_args_list)
    assert "`/help`" in printed
    assert "`/help schedule`" in printed


async def test_login_says_where_to_sign_in():
    """``/login`` is carried out by the clients, so its result has no
    content: ``igni -m /login`` printed nothing and exited 0, and the only
    other hint named a TUI and a ``--no-tui`` flag removed in v0.9.7."""
    session = _session(_Team(None))
    with patch("ember_code.core.session.session_run.Session", return_value=session):
        await run_single_message(MagicMock(), "/login")

    printed = "\n".join(c.args[0] for c in session.display.print_info.call_args_list)
    assert "`igni login`" in printed
    assert "~/.igni/credentials.json" in printed
    assert "TUI" not in printed and "--no-tui" not in printed
