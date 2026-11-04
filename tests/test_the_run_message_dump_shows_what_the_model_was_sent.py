"""The DEBUG dump of a run shows what the model was sent, and never breaks the run.

:class:`RunMessagesDebugDumper` is how a tool result that never reached
the model is diagnosed: one line per message with its role, the tool call
it answers, the tools it calls, whether it was compressed or replayed from
history, and the start of its content. It runs on the response path, so a
team in an unexpected shape must cost a DEBUG line, not the reply.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from ember_code.core.session.run_debug import RunMessagesDebugDumper

LOGGER = "ember_code.core.session.run_debug"


class _Message:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class _Run:
    def __init__(self, messages: Any) -> None:
        self.messages = messages


class _Team:
    def __init__(self, run_response: Any) -> None:
        self.run_response = run_response


@pytest.fixture
def debug_log(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    return caplog


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def test_each_message_is_one_line_with_what_it_carried(debug_log):
    messages = [
        _Message(role="user", content="fix it"),
        _Message(
            role="assistant",
            content=None,
            tool_calls=[{"function": {"name": "run_shell_command"}}, {"function": {}}],
        ),
        _Message(
            role="tool",
            content="x" * 250,
            tool_call_id="call_1",
            compressed_content="short",
            from_history=True,
        ),
    ]

    RunMessagesDebugDumper.dump_team(_Team(_Run(messages)))

    header, user, assistant, tool = _lines(debug_log)
    assert header == "RUN_MESSAGES: 3 messages total"
    assert "role=user" in user and user.endswith("| fix it")
    assert "calls=['run_shell_command', '?']" in assistant
    assert assistant.endswith("| <None>")
    assert "tcid=call_1" in tool
    assert "COMPRESSED(5ch)" in tool
    assert "HIST" in tool
    assert tool.endswith("x" * 200 + "... (250 total)")


def test_a_team_that_has_not_run_says_so(debug_log):
    RunMessagesDebugDumper(_Team(None)).dump()

    assert _lines(debug_log) == ["RUN_MESSAGES: no run_response"]


def test_a_run_with_no_messages_says_so(debug_log):
    RunMessagesDebugDumper(_Team(_Run([]))).dump()

    assert _lines(debug_log) == ["RUN_MESSAGES: no messages in run_response"]


def test_a_message_in_an_unexpected_shape_costs_a_line_not_the_reply(debug_log):
    malformed = _Message(role="assistant", content="", tool_calls=["not a dict"])

    RunMessagesDebugDumper(_Team(_Run([malformed]))).dump()

    assert _lines(debug_log)[-1].startswith("RUN_MESSAGES: error: ")
