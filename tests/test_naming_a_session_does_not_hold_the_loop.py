"""Titling a session is a small async call that never holds the loop.

The backend titles a session after its first run. It used Agno's
``aset_session_name``, which calls the model *synchronously* on the
event loop with the whole conversation — the ~12k-token system prompt
included — and asks up to four times when the answer runs past five
words. Against the real igni-1.0-4b, which answers the conversation
instead of naming it, each new session cost four uncached ~12k-token
calls, and the loop sat blocked long enough for the backend to miss
its WebSocket pings: the client was dropped mid-task with
``1011 keepalive ping timeout``.

These drive :meth:`SessionsController.maybe_auto_name_session` against
a real Agno agent and SQLite session row, with a model that blocks the
thread when called synchronously and yields when awaited.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.sqlite import AsyncSqliteDb
from agno.models.message import Message
from agno.models.openai.like import OpenAILike
from agno.models.response import ModelResponse
from agno.run.agent import RunInput, RunOutput
from agno.session.agent import AgentSession

from ember_code.backend.server_sessions import SessionsController
from ember_code.core.session.persistence import SessionPersistence
from ember_code.core.session.persistence.title_asker import TitleAsker

SESSION_ID = "sess-title"
SYSTEM_PROMPT = "You are igni. " + "Rules for working with the user. " * 1500
FIRST_MESSAGE = (
    "What port does the service in config.toml listen on, and what is its "
    "max_batch? Read the file to check."
)
MODEL_SECONDS = 0.3


class TitleModel(OpenAILike):
    """Answers from a script; ``response`` blocks the thread as a
    synchronous HTTP call does, ``aresponse`` yields as an async one."""

    def __init__(self, answers: list[str]) -> None:
        super().__init__(id="title-model", api_key="unused", base_url="http://127.0.0.1:9")
        self.answers = answers
        self.calls: list[list[Message]] = []
        self.budgets: list[Any] = []

    def _next(self, messages: list[Message]) -> ModelResponse:
        self.calls.append(list(messages))
        self.budgets.append(self.max_tokens)
        answer = self.answers[min(len(self.calls), len(self.answers)) - 1]
        return ModelResponse(role="assistant", content=answer)

    def response(self, messages: list[Message], **_: Any) -> ModelResponse:  # type: ignore[override]
        time.sleep(MODEL_SECONDS)
        return self._next(messages)

    async def aresponse(self, messages: list[Message], **_: Any) -> ModelResponse:  # type: ignore[override]
        await asyncio.sleep(MODEL_SECONDS)
        return self._next(messages)


class Rig:
    def __init__(self, tmp_path: Path, answers: list[str], log: list[Any]) -> None:
        self.db = AsyncSqliteDb(
            db_file=str(tmp_path / "state.db"),
            session_table="ember_sessions",
            memory_table="ember_memories",
        )
        self.model = TitleModel(answers)
        # The model is shared: copies made for the title call append
        # to the same lists.
        self.model.calls = log
        self.persistence = SessionPersistence(db=self.db, session_id=SESSION_ID)
        agent = Agent(model=self.model, db=self.db, session_id=SESSION_ID)
        self.controller = SessionsController(
            SimpleNamespace(persistence=self.persistence, main_team=agent, session_named=False),
            chat_history_provider=lambda _sid: _empty(),
        )

    async def first_run_done(self) -> None:
        run = RunOutput(
            run_id="run-1",
            agent_id="ember",
            session_id=SESSION_ID,
            input=RunInput(
                input_content=(
                    "<system-context>Current datetime: 2026-11-03 01:45 CET</system-context>\n"
                    + FIRST_MESSAGE
                )
            ),
            messages=[
                Message(role="system", content=SYSTEM_PROMPT),
                Message(role="user", content=FIRST_MESSAGE),
                Message(role="assistant", content="It listens on port 8443."),
            ],
            content="It listens on port 8443.",
        )
        await self.db.upsert_session(
            AgentSession(
                session_id=SESSION_ID,
                agent_id="ember",
                runs=[run],
                session_data={},
                created_at=int(time.time()),
            )
        )

    async def stored_name(self) -> str:
        row = await self.db.get_session(session_id=SESSION_ID, session_type=SessionType.AGENT)
        return (row.session_data or {}).get("session_name", "") if row else ""


async def _empty() -> list[Any]:
    return []


async def _name_while_ticking(rig: Rig, *, busy: bool = False) -> tuple[Any, int]:
    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        if busy:
            result = await rig.controller.maybe_auto_name_session(model_busy=True)
        else:
            result = await rig.controller.maybe_auto_name_session()
    finally:
        stop.set()
        await task
    return result, ticks


async def test_the_loop_keeps_running_while_the_model_titles_the_session(tmp_path):
    calls: list[Any] = []
    rig = Rig(tmp_path, ["Config port and max_batch"], calls)
    await rig.first_run_done()

    result, ticks = await _name_while_ticking(rig)

    assert result.ok and result.name == "Config port and max_batch"
    assert len(calls) == 1
    # A blocked loop ticks once or twice over the 0.3 s call; a free
    # one ~30 times.
    assert ticks >= 10, f"the loop ticked {ticks} times while the model ran"
    assert await rig.stored_name() == "Config port and max_batch"


async def test_the_title_call_carries_the_opening_only_with_a_token_budget(tmp_path):
    calls: list[Any] = []
    rig = Rig(tmp_path, ["Config port and max_batch"], calls)
    await rig.first_run_done()

    await rig.controller.maybe_auto_name_session()

    sent = "\n".join(str(m.content) for m in calls[0])
    assert FIRST_MESSAGE in sent
    assert "Rules for working with the user" not in sent
    assert "system-context" not in sent
    assert "It listens on port 8443" not in sent
    assert len(sent) < 1500
    assert rig.model.budgets == [TitleAsker.MAX_TITLE_TOKENS]
    # The live model's own budget is not touched.
    assert rig.model.max_tokens != TitleAsker.MAX_TITLE_TOKENS


async def test_an_answer_that_is_not_a_title_is_retried_once_then_the_opening_is_used(tmp_path):
    calls: list[Any] = []
    answer = "It listens on port **8443** and has `max_batch = 250`, as the file config.toml says."
    rig = Rig(tmp_path, [answer, answer, answer, answer], calls)
    await rig.first_run_done()

    result = await rig.controller.maybe_auto_name_session()

    assert len(calls) == TitleAsker.MAX_ATTEMPTS == 2
    assert result.ok
    assert result.name == "What port does the service in…"
    assert await rig.stored_name() == result.name


async def test_a_busy_model_is_not_asked(tmp_path):
    calls: list[Any] = []
    rig = Rig(tmp_path, ["Config port and max_batch"], calls)
    await rig.first_run_done()

    result, _ = await _name_while_ticking(rig, busy=True)

    assert calls == []
    assert result.name == "What port does the service in…"


async def test_a_named_session_is_left_alone(tmp_path):
    calls: list[Any] = []
    rig = Rig(tmp_path, ["Something else"], calls)
    await rig.first_run_done()
    await rig.persistence.rename("Chosen by the person")

    result = await rig.controller.maybe_auto_name_session()

    assert calls == []
    assert result.name == "Chosen by the person"


def test_a_short_clean_answer_is_a_title_and_a_wrapped_one_is_unwrapped():
    assert TitleAsker.usable("\n\nConfig check and bug fix.") == "Config check and bug fix"
    assert TitleAsker.usable('"**Port lookup**"') == "Port lookup"
    assert TitleAsker.usable("<think>naming it</think>Port lookup") == "Port lookup"
    assert TitleAsker.usable("Conversation Naming Guidelines\n\n1. Keep it short") == ""
    assert TitleAsker.usable("") == ""


def test_the_fallback_is_the_openings_first_words():
    assert TitleAsker.fallback("Say hello") == "Say hello"
    assert TitleAsker.fallback("Fix   the\nfailing test in mathx please now") == (
        "Fix the failing test in mathx…"
    )
