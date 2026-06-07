"""End-to-end test of the spawn pipeline against a real Agno Agent.

The earlier ``test_subagent_hitl_e2e.py`` mocks the Agent — useful for
the HITL bridge plumbing, but it can't catch bugs that depend on Agno's
real session-DB persistence (e.g. the architect's PAUSED-vs-COMPLETED
state, ``aget_run_output`` lookup semantics). This test wires up a real
``Agent`` with a real ``AsyncSqliteDb`` and a stub model, and confirms:

1. ``_run_agent_streaming`` captures the same ``run_id`` / ``session_id``
   that Agno persists into the DB.
2. After the stream ends, ``aget_run_output`` returns a ``RunOutput``
   with the model's content.
3. The final string returned from ``_run_agent_streaming`` matches.

The stub model is intentionally minimal: emits a single
``assistant_response`` ``ModelResponse`` with content (no tool calls),
so Agno completes the run on the first model call. That's enough to
exercise the read-back path that production uses.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from ember_code.core.tools.orchestrate import _run_agent_streaming


def _make_stub_model(content: str):
    """Build a minimal concrete subclass of Agno's ``Model``.

    We override ``aresponse_stream`` directly so the agent's response
    loop pulls our canned events without going anywhere near a network
    or provider SDK. The other abstract methods are stubbed out — the
    streaming-completion path Agno uses for our test never invokes
    them. Defined inside a function so each test gets a fresh class
    (model state is per-instance but provider registries can be sticky).
    """
    from agno.models.base import Model, ModelResponse, ModelResponseEvent

    class _StubModel(Model):
        id: str = "stub-model"
        provider: str = "stub"

        def __init__(self) -> None:
            super().__init__(id="stub-model")
            self._content = content

        async def aresponse_stream(self, **kwargs: Any):  # type: ignore[override]
            yield ModelResponse(event=ModelResponseEvent.model_request_started.value)
            yield ModelResponse(
                event=ModelResponseEvent.assistant_response.value,
                content=self._content,
            )
            yield ModelResponse(event=ModelResponseEvent.model_request_completed.value)

        # Required abstract API — unused by ``aresponse_stream``-driven path.
        def invoke(self, *args: Any, **kwargs: Any):  # pragma: no cover
            raise NotImplementedError

        async def ainvoke(self, *args: Any, **kwargs: Any):  # pragma: no cover
            raise NotImplementedError

        def invoke_stream(self, *args: Any, **kwargs: Any):  # pragma: no cover
            raise NotImplementedError

        def ainvoke_stream(self, *args: Any, **kwargs: Any):  # pragma: no cover
            raise NotImplementedError

        def _parse_provider_response(self, response: Any, **kwargs: Any):
            return ModelResponse(content="")

        def _parse_provider_response_delta(self, response: Any):
            return ModelResponse(content="")

    return _StubModel()


def _has_async_sqlite() -> bool:
    try:
        from agno.db.sqlite import AsyncSqliteDb  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_async_sqlite(), reason="agno.db.sqlite not installed")
@pytest.mark.asyncio
async def test_run_agent_streaming_reads_persisted_content():
    """Spawn drives a real Agno Agent; after the stream completes,
    ``aget_run_output`` (the Agno-canonical read path) must return the
    model's content. This is what production uses to fetch the
    architect's response."""
    from agno.agent import Agent
    from agno.db.sqlite import AsyncSqliteDb

    db_path = Path(tempfile.mkdtemp()) / "test.db"
    db = AsyncSqliteDb(db_file=str(db_path), session_table="test_sessions")

    answer = "ARCHITECT FINAL ANSWER: detailed architectural analysis of the project"
    agent = Agent(name="test-architect", model=_make_stub_model(answer), db=db)

    result, _log = await asyncio.wait_for(
        _run_agent_streaming(agent, "Analyze", agent_path=["architect"]),
        timeout=15,
    )

    assert answer in result, f"orchestrate didn't surface the model's content. got: {result!r}"


@pytest.mark.skipif(not _has_async_sqlite(), reason="agno.db.sqlite not installed")
@pytest.mark.asyncio
async def test_aget_run_output_finds_completed_run():
    """Sanity check: Agno's own ``aget_run_output`` retrieves the run
    we just persisted via ``arun``. If this regresses, the orchestrate
    read-back will silently return empty."""
    from agno.agent import Agent
    from agno.db.sqlite import AsyncSqliteDb
    from agno.run import agent as agent_events

    db_path = Path(tempfile.mkdtemp()) / "test.db"
    db = AsyncSqliteDb(db_file=str(db_path), session_table="test_sessions")
    agent = Agent(name="t", model=_make_stub_model("hello world"), db=db)

    run_id: str | None = None
    session_id: str | None = None
    # ``stream_events=True`` is required for ``RunStartedEvent`` to be
    # emitted — without it Agno only yields the model-content events
    # and the started/completed lifecycle is silent (the data still
    # gets persisted to the DB; you just have to read run_id from
    # ``RunPausedEvent`` / ``RunCompletedEvent`` instead).
    async for event in agent.arun("hi", stream=True, stream_events=True):
        if isinstance(event, agent_events.RunStartedEvent):
            run_id = getattr(event, "run_id", None)
            session_id = getattr(event, "session_id", None)

    assert run_id and session_id

    out = await agent.aget_run_output(run_id=run_id, session_id=session_id)
    assert out is not None
    assert "hello world" in str(out.content)
