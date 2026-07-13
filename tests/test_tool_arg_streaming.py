"""Tool-arg streaming — tests for the CustomEvent-based visualizer
delivery path.

The old content-stream approach (visualizer sub-agent outputs raw
JSON as its response body) has been replaced with a real
``visualize({spec: {...}})`` tool call. To preserve progressive
rendering, ``_LoggingModel`` wraps the model stream and emits
``CustomEvent(event='tool_call_input_delta')`` on every model chunk
that carries tool_call arg fragments. ``orchestrate.py`` picks
these up and forwards them to the FE as ``visualization_delta``.

These tests cover:
- ``_ToolCallAccumulatorStore.apply`` — deltas merge by index,
  first-fragment-only entries return None (no wire traffic
  warranted).
- ``_emit_tool_arg_delta_events`` — CustomEvent shape, tool_name /
  tool_call_id propagation, accumulated args are monotonic.
- ``_extract_spec_from_partial_args`` — jiter partial parse of
  ``{"spec": ...}`` including tail-truncated tokens.
"""

from __future__ import annotations

import json

import pytest

from agno.models.response import ModelResponse
from agno.run import agent as agent_events
from agno.run.agent import CustomEvent

from ember_code.core.config.models import (
    _aemit_tool_arg_deltas,
    _ToolCallAccumulator,
    _ToolCallAccumulatorStore,
    _ToolCallFragment,
    _emit_tool_arg_delta_events,
)
from ember_code.core.tools.orchestrate import (
    OrchestrateTools,
    _extract_spec_from_partial_args,
    _run_agent_streaming,
)


def _chunk(tool_calls: list[dict]) -> ModelResponse:
    """Build a ``ModelResponse`` shaped like an OpenAI-compatible
    streaming delta with the given tool_call list."""
    resp = ModelResponse()
    resp.tool_calls = tool_calls  # type: ignore[assignment]
    return resp


class TestToolCallFragment:
    def test_from_dict_shape(self):
        frag = _ToolCallFragment.from_provider(
            {"index": 0, "id": "call_1", "function": {"name": "visualize", "arguments": "{"}}
        )
        assert frag.index == 0
        assert frag.call_id == "call_1"
        assert frag.name == "visualize"
        assert frag.args_fragment == "{"

    def test_from_sdk_object_shape(self):
        # Simulate the openai-python ``ChoiceDeltaToolCall`` shape:
        # duck-typed object with .index/.id/.function attributes.
        class _Fn:
            name = "visualize"
            arguments = "{"

        class _TC:
            index = 0
            id = "call_1"
            function = _Fn()

        frag = _ToolCallFragment.from_provider(_TC())
        assert frag.index == 0
        assert frag.call_id == "call_1"
        assert frag.name == "visualize"
        assert frag.args_fragment == "{"

    def test_delta_carrying_only_args_fragment(self):
        # 2nd+ delta on OpenAI-compatible streams: only .index +
        # .function.arguments — no id, no name.
        frag = _ToolCallFragment.from_provider(
            {"index": 0, "function": {"arguments": ' "root":'}}
        )
        assert frag.index == 0
        assert frag.call_id is None
        assert frag.name is None
        assert frag.args_fragment == ' "root":'

    def test_unknown_shape_returns_empty(self):
        # Defensive: some future provider hands us a string or an
        # int and we don't crash — the delta is a no-op.
        frag = _ToolCallFragment.from_provider(42)
        assert frag.index is None
        assert frag.args_fragment is None


class TestAccumulatorStore:
    def test_merges_deltas_by_index(self):
        store = _ToolCallAccumulatorStore()
        # First delta: id + name + args fragment
        e1 = store.apply(
            _ToolCallFragment(index=0, call_id="call_1", name="visualize", args_fragment="{")
        )
        assert e1 is not None
        assert e1.call_id == "call_1"
        assert e1.name == "visualize"
        assert e1.args == "{"

        # Second delta: only index + args fragment (no id/name)
        e2 = store.apply(_ToolCallFragment(index=0, args_fragment='"spec":'))
        assert e2 is not None
        # Merged into the same accumulator entry.
        assert e2.call_id == "call_1"
        assert e2.name == "visualize"
        assert e2.args == '{"spec":'

    def test_first_fragment_without_args_returns_none(self):
        # Some providers send an initial delta with just id + name
        # and no argument bytes yet — we shouldn't emit a
        # CustomEvent for that (empty arguments_partial is noise).
        store = _ToolCallAccumulatorStore()
        result = store.apply(
            _ToolCallFragment(index=0, call_id="call_1", name="visualize", args_fragment=None)
        )
        assert result is None
        # But the state is still remembered — the NEXT delta with
        # args_fragment will find the seeded id + name.
        entry = store.by_index[0]
        assert entry.call_id == "call_1"
        assert entry.name == "visualize"

    def test_index_none_is_dropped(self):
        # Defensive: a fragment without an index has nothing to
        # merge against.
        store = _ToolCallAccumulatorStore()
        result = store.apply(_ToolCallFragment(index=None, args_fragment="{"))
        assert result is None
        assert store.by_index == {}

    def test_multiple_concurrent_tool_calls(self):
        # A model can start several tool calls in one turn; each
        # gets its own accumulator keyed by index.
        store = _ToolCallAccumulatorStore()
        store.apply(_ToolCallFragment(index=0, call_id="c0", name="visualize", args_fragment="{"))
        store.apply(_ToolCallFragment(index=1, call_id="c1", name="read_file", args_fragment="{"))
        store.apply(_ToolCallFragment(index=0, args_fragment='"a":1}'))
        store.apply(_ToolCallFragment(index=1, args_fragment='"path":"x"}'))
        assert store.by_index[0].args == '{"a":1}'
        assert store.by_index[1].args == '{"path":"x"}'


class TestEmitToolArgDeltaEvents:
    def test_emits_custom_event_per_delta(self):
        store = _ToolCallAccumulatorStore()
        chunk = _chunk(
            [{"index": 0, "id": "c1", "function": {"name": "visualize", "arguments": "{"}}]
        )
        events = _emit_tool_arg_delta_events(chunk, store)
        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, CustomEvent)
        assert ev.event == "tool_call_input_delta"
        assert ev.tool_call_id == "c1"
        assert ev.tool_name == "visualize"
        assert ev.arguments_partial == "{"

    def test_no_event_for_content_only_chunk(self):
        # Text-content chunks (the hot path) produce zero events.
        store = _ToolCallAccumulatorStore()
        chunk = ModelResponse()
        chunk.content = "some text"
        assert _emit_tool_arg_delta_events(chunk, store) == []

    def test_malformed_tool_call_shape_is_swallowed_not_raised(self):
        """Critical regression: if a chunk has an unexpected shape
        that our pydantic parser can't handle, we MUST swallow the
        error and return [] — never raise.

        Reason: Agno's ``aprocess_response_stream`` runs
        ``_populate_assistant_message_from_stream_data`` AFTER its
        ``async for`` loop. If our wrapper raises mid-loop, that
        post-loop call is skipped, and Agno's own tool_call
        accumulator (which is INDEPENDENT of ours) is never
        finalized — the model's tool_call arguments come out
        malformed ("'{' was never closed"). Losing progressive
        rendering on ONE chunk is much cheaper than corrupting the
        actual tool call the model spent tokens building.
        """
        store = _ToolCallAccumulatorStore()
        # A tool_call carrying garbage where args_fragment should
        # be. Our pydantic model would ordinarily raise a
        # ValidationError. The defensive try/except must catch it.
        garbage = _chunk(
            [
                {
                    "index": 0,
                    "id": "c1",
                    "function": {"name": "visualize", "arguments": {"not": "a string"}},
                }
            ]
        )
        # Must not raise.
        result = _emit_tool_arg_delta_events(garbage, store)
        # Also must return a valid list (empty or otherwise).
        assert isinstance(result, list)

    def test_arguments_partial_grows_monotonic(self):
        store = _ToolCallAccumulatorStore()
        _emit_tool_arg_delta_events(
            _chunk([{"index": 0, "id": "c1", "function": {"name": "visualize", "arguments": "{"}}]),
            store,
        )
        e2 = _emit_tool_arg_delta_events(
            _chunk([{"index": 0, "function": {"arguments": '"spec":'}}]),
            store,
        )
        e3 = _emit_tool_arg_delta_events(
            _chunk([{"index": 0, "function": {"arguments": '{"root":"r"}}'}}]),
            store,
        )
        # Each event's arguments_partial is a strict prefix-extension
        # of the previous — the FE relies on this for its
        # partial-JSON parser.
        assert e2[0].arguments_partial.startswith("{")
        assert e3[0].arguments_partial.startswith(e2[0].arguments_partial)


class TestExtractSpecFromPartialArgs:
    def test_extracts_complete_spec(self):
        full = '{"spec": {"root": "r", "elements": {"r": {"type": "Text", "props": {"text": "hi"}}}}}'
        got = _extract_spec_from_partial_args(full)
        assert got is not None
        # Result is a JSON string; parsing it should yield the
        # inner ``spec`` dict.
        assert json.loads(got) == {
            "root": "r",
            "elements": {"r": {"type": "Text", "props": {"text": "hi"}}},
        }

    def test_extracts_partial_spec_with_tail_truncated_string(self):
        # jiter's ``trailing-strings`` mode salvages a truncated
        # value so we can still render partial contents.
        partial = '{"spec": {"root": "r", "elements": {"r": {"type": "Text", "props": {"text": "AAPL '
        got = _extract_spec_from_partial_args(partial)
        assert got is not None
        parsed = json.loads(got)
        # Trailing string mode preserves the incomplete value.
        assert parsed["root"] == "r"
        assert parsed["elements"]["r"]["type"] == "Text"

    def test_returns_none_before_spec_object_opens(self):
        # First bytes of the argument JSON — ``spec`` isn't a dict yet.
        assert _extract_spec_from_partial_args("") is None
        assert _extract_spec_from_partial_args("{") is None
        assert _extract_spec_from_partial_args('{"sp') is None

    def test_returns_none_when_spec_is_not_a_dict(self):
        # Defensive — if someone somehow calls visualize with
        # ``{"spec": "..."}`` we don't emit a nonsense delta.
        assert _extract_spec_from_partial_args('{"spec": "not-an-object"}') is None


# ── Async pipeline: full generator wrapping ─────────────────────────


class TestAsyncPipelineWrapping:
    """Verifies the async wrapper glue (``_aemit_tool_arg_deltas``)
    interleaves CustomEvents into the chunk stream in the right
    order — same asserts as the sync path, on the async transport."""

    @pytest.mark.asyncio
    async def test_async_wrapper_interleaves_events(self):
        async def source():
            # First delta: id + name + args opener.
            yield _chunk(
                [{"index": 0, "id": "c1", "function": {"name": "visualize", "arguments": "{"}}]
            )
            # Second delta: args continues.
            yield _chunk([{"index": 0, "function": {"arguments": '"spec":'}}])
            # A plain content chunk in between — should pass through
            # untouched with no CustomEvent alongside.
            content = ModelResponse()
            content.content = "hello"
            yield content
            # Third delta: closing.
            yield _chunk(
                [{"index": 0, "function": {"arguments": '{"root":"r"}}'}}]
            )

        emitted: list = []
        async for ev in _aemit_tool_arg_deltas(source()):
            emitted.append(ev)

        # For each of the 3 tool_call chunks we get chunk + CustomEvent (6 events),
        # plus the standalone content chunk (1) = 7 total.
        assert len(emitted) == 7

        custom_events = [e for e in emitted if isinstance(e, CustomEvent)]
        assert len(custom_events) == 3
        assert all(e.event == "tool_call_input_delta" for e in custom_events)
        assert [e.arguments_partial for e in custom_events] == [
            "{",
            '{"spec":',
            '{"spec":{"root":"r"}}',
        ]

    @pytest.mark.asyncio
    async def test_async_wrapper_ignores_reasoning_content(self):
        # Reasoning-content chunks come through the same delta path
        # (``model_response.reasoning_content``) but don't carry
        # ``tool_calls``. Wrapper must NOT emit a CustomEvent for
        # them — they're just passthrough.
        async def source():
            reasoning = ModelResponse()
            reasoning.reasoning_content = "thinking..."
            yield reasoning
            yield _chunk(
                [{"index": 0, "id": "c1", "function": {"name": "visualize", "arguments": "{}"}}]
            )

        emitted: list = []
        async for ev in _aemit_tool_arg_deltas(source()):
            emitted.append(ev)

        custom_events = [e for e in emitted if isinstance(e, CustomEvent)]
        assert len(custom_events) == 1  # only for the tool_call chunk


# ── Full orchestrate.py integration ────────────────────────────────


class _RunStartedFake:
    """Minimal RunStartedEvent shape orchestrate.py reads for
    ``run_id`` / ``session_id`` / ``parent_run_id`` capture."""

    def __init__(self, run_id: str, session_id: str, parent_run_id: str | None = None):
        self.run_id = run_id
        self.session_id = session_id
        self.parent_run_id = parent_run_id


class _ToolExecutionFake:
    """ToolExecution stand-in for ToolCallStartedEvent — carries the
    full parsed ``tool_args`` (Agno only surfaces the whole dict at
    ToolCallStartedEvent time, hence why we emit the final delta
    from that handler)."""

    def __init__(self, tool_name: str, tool_args: dict, tool_call_id: str = "c1"):
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.tool_call_id = tool_call_id
        self.result = None


class _MockAgent:
    """Minimum surface of an Agno Agent that ``_run_agent_streaming``
    reads. Yields a pre-built event tape and returns nothing
    meaningful from the DB lookup — the visualization we care about
    lands via ``on_progress``, not via the parent's returned text."""

    def __init__(self, events):
        self._events = events

    def arun(self, task: str, stream: bool = True, stream_events: bool = False):
        _ = task, stream, stream_events

        async def _gen():
            for ev in self._events:
                yield ev

        return _gen()

    async def aget_run_output(self, run_id: str = "", session_id: str = ""):
        return None

    async def aget_last_run_output(self, session_id: str = ""):
        return None


class TestOrchestrateVisualizerIntegration:
    """Drives ``_run_agent_streaming`` with a mocked Agno event tape
    that mimics what the visualizer sub-agent produces: streaming
    CustomEvents from ``_LoggingModel`` plus a final
    ``ToolCallStartedEvent`` carrying the parsed args.

    This is the closest we can get to "the whole thing works"
    without a live LLM — the model layer is stubbed but every
    piece of the visualizer-forwarding pipeline runs for real."""

    @pytest.mark.asyncio
    async def test_progressive_visualization_delta_events(self):
        # Simulate one visualizer sub-agent run: streaming tool_call
        # args, then a completed ToolCallStartedEvent with the full
        # parsed spec. The ``_LoggingModel`` wrapper would produce
        # the CustomEvents in real life; here we inject them directly.
        tape = [
            _RunStartedFake(run_id="r1", session_id="s1"),
            CustomEvent(
                event="tool_call_input_delta",
                tool_call_id="c1",
                tool_name="visualize",
                arguments_partial='{"spec":',
            ),
            CustomEvent(
                event="tool_call_input_delta",
                tool_call_id="c1",
                tool_name="visualize",
                arguments_partial='{"spec": {"root": "r"}',
            ),
            CustomEvent(
                event="tool_call_input_delta",
                tool_call_id="c1",
                tool_name="visualize",
                arguments_partial='{"spec": {"root": "r", "elements": {"r": {"type": "Text", "props": {"text": "AAPL"}}}}}',
            ),
        ]

        # ToolCallStartedEvent carries the fully-parsed tool_args.
        # Construct it via the real class so isinstance() matches.
        ts_event = agent_events.ToolCallStartedEvent(
            agent_id="",
            agent_name="",
            tool=_ToolExecutionFake(
                tool_name="visualize",
                tool_args={
                    "spec": {
                        "root": "r",
                        "elements": {"r": {"type": "Text", "props": {"text": "AAPL"}}},
                    }
                },
            ),
        )
        tape.append(ts_event)

        progress: list[dict] = []
        await _run_agent_streaming(
            agent=_MockAgent(tape),
            task="viz",
            on_progress=lambda ev: progress.append(ev),
            agent_path=["visualizer"],
        )

        deltas = [e for e in progress if e.get("type") == "visualization_delta"]
        assert deltas, (
            f"Expected visualization_delta events; got types: "
            f"{[e.get('type') for e in progress]}"
        )

        # First delta may be dropped by the 50ms throttle if the
        # test wall-clock is generous; the LAST delta must be final.
        finals = [d for d in deltas if d.get("final") is True]
        assert len(finals) == 1
        final = finals[0]
        assert final["spec_id"]
        # The final JSON parses to the exact spec the tool call
        # carried.
        parsed = json.loads(final["json"])
        assert parsed == {
            "root": "r",
            "elements": {"r": {"type": "Text", "props": {"text": "AAPL"}}},
        }

        # spec_id is stable across the whole run — the FE dedups on it.
        spec_ids = {d["spec_id"] for d in deltas}
        assert len(spec_ids) == 1

    @pytest.mark.asyncio
    async def test_non_visualize_tools_dont_emit_deltas(self):
        # Only ``tool_name == "visualize"`` should route to the
        # progressive path — a ``read_file`` sub-agent call must
        # NOT emit visualization_delta.
        tape = [
            _RunStartedFake(run_id="r1", session_id="s1"),
            CustomEvent(
                event="tool_call_input_delta",
                tool_call_id="c1",
                tool_name="read_file",
                arguments_partial='{"path": "/x"}',
            ),
        ]

        progress: list[dict] = []
        await _run_agent_streaming(
            agent=_MockAgent(tape),
            task="read",
            on_progress=lambda ev: progress.append(ev),
            agent_path=["explorer"],
        )

        deltas = [e for e in progress if e.get("type") == "visualization_delta"]
        assert deltas == []

    @pytest.mark.asyncio
    async def test_agent_completed_emitted_when_run_completed_never_arrives(self):
        """Regression: Agno's specialist ``arun`` doesn't yield
        ``RunCompletedEvent`` without ``stream_events=True`` — which
        we deliberately keep OFF to avoid noisy lifecycle events on
        the wire. But the FE relies on ``agent_completed`` to stop
        the sub-agent's spinning team card.

        The user hit this: viz rendered fine, but the team card
        kept spinning after the visualizer finished. Fix: emit
        ``agent_completed`` from a post-loop fallback if the
        in-stream handler didn't already (i.e. real
        RunCompletedEvent arrived).
        """
        # No RunCompletedEvent in the tape — just a RunStarted and
        # a content chunk, like Agno actually yields.
        tape = [
            _RunStartedFake(
                run_id="subagent-r1",
                session_id="s1",
                parent_run_id="TOP",
            ),
            agent_events.RunContentEvent(
                agent_id="",
                agent_name="",
                content="hi",
                run_id="subagent-r1",
                session_id="s1",
            ),
        ]

        progress: list[dict] = []
        await _run_agent_streaming(
            agent=_MockAgent(tape),
            task="viz",
            on_progress=lambda ev: progress.append(ev),
            agent_path=["visualizer"],
        )

        completed_events = [
            e for e in progress if e.get("type") == "agent_completed"
        ]
        assert len(completed_events) == 1, (
            f"Expected exactly one agent_completed emission (post-loop "
            f"fallback), got {len(completed_events)}. Progress types: "
            f"{[e.get('type') for e in progress]}"
        )
        assert completed_events[0]["agent_path"] == "visualizer"
        assert completed_events[0]["is_error"] is False

    @pytest.mark.asyncio
    async def test_agent_completed_not_double_emitted_when_run_completed_fires(
        self,
    ):
        """When the caller opts into ``stream_events=True`` and Agno
        DOES yield a ``RunCompletedEvent``, we emit ``agent_completed``
        from the in-loop handler. The post-loop fallback must NOT
        fire again — the ``agent_completed_emitted`` flag guards
        this."""
        tape = [
            _RunStartedFake(
                run_id="subagent-r1",
                session_id="s1",
                parent_run_id="TOP",
            ),
            # Real RunCompletedEvent — construct via the actual class
            # so the isinstance() check in _handle matches.
            agent_events.RunCompletedEvent(
                agent_id="",
                agent_name="",
                content="done",
                run_id="subagent-r1",
                session_id="s1",
            ),
        ]

        progress: list[dict] = []
        await _run_agent_streaming(
            agent=_MockAgent(tape),
            task="viz",
            on_progress=lambda ev: progress.append(ev),
            agent_path=["visualizer"],
        )

        completed_events = [
            e for e in progress if e.get("type") == "agent_completed"
        ]
        assert len(completed_events) == 1, (
            f"Expected exactly one agent_completed emission (the "
            f"in-loop one), got {len(completed_events)}. "
            f"agent_completed_emitted flag isn't guarding correctly."
        )

    @pytest.mark.asyncio
    async def test_subagent_run_id_registered_and_deregistered_on_completion(self):
        """Regression: user hit ESC on a stuck visualizer sub-agent
        and it kept running. Root cause was Agno's cooperative
        cancel is keyed per-run-id — flagging the TOP-LEVEL run_id
        (which is what ``BackendServer.cancel_run`` had access to)
        doesn't propagate to sub-agents that have their own
        distinct run_ids.

        Fix: ``OrchestrateTools._active_subagent_runs`` — a class
        registry that ``_run_agent_streaming`` populates when it
        latches onto the sub-agent's run_id and cleans up in its
        ``finally`` block. ``BackendServer.cancel_run`` iterates
        the registry and cancels every entry.

        This test verifies the register/deregister lifecycle.
        """
        # Snapshot the registry before we run — some other test
        # might have left entries, and we only care about our own.
        registry = OrchestrateTools._active_subagent_runs
        before = set(registry)

        # Sentinel: as _handle sees the first event with a run_id,
        # it should add "subagent-run-42" to the registry. We
        # verify that by peeking via a passthrough on_progress.
        seen_registered_during_run: list[bool] = []

        def on_progress(_ev):
            # We're inside the run loop when this fires — the
            # registry should contain the sub-agent's run_id.
            seen_registered_during_run.append("subagent-run-42" in registry)

        tape = [
            _RunStartedFake(
                run_id="subagent-run-42",
                session_id="s1",
                parent_run_id="TOP-LEVEL",
            ),
            # A trivial content event so on_progress fires while
            # the run is still active — the RunContentEvent class
            # requires ``agent_id``/``agent_name`` but nothing else
            # in ``_run_agent_streaming`` reads them.
            agent_events.RunContentEvent(
                agent_id="",
                agent_name="",
                content="hi",
                run_id="subagent-run-42",
                session_id="s1",
            ),
        ]

        await _run_agent_streaming(
            agent=_MockAgent(tape),
            task="viz",
            on_progress=on_progress,
            agent_path=["visualizer"],
        )

        # During the run, the sub-agent's run_id was in the registry.
        # This proves the register step ran.
        assert any(seen_registered_during_run), (
            "sub-agent run_id was never registered while the stream "
            "was live — cancel_run couldn't reach it via the registry"
        )

        # After the run completes cleanly, the sub-agent's run_id
        # must be gone — the ``finally`` in ``_run_agent_streaming``
        # deregisters. If it lingered, the set would grow across
        # sessions and eventually hold stale ids.
        assert "subagent-run-42" not in registry, (
            "sub-agent run_id leaked into the registry after the "
            "stream ended — finally-block cleanup didn't run"
        )
        # No collateral damage to pre-existing entries.
        assert registry == before

    @pytest.mark.asyncio
    async def test_subagent_run_id_deregistered_on_exception(self):
        """Even when the stream raises mid-flight, the ``finally``
        must still remove the sub-agent's run_id — otherwise a
        crashed run leaks its id and every future ESC cancels
        it (against a run that no longer exists) forever."""
        class _BoomAgent:
            def arun(self, task, stream=True, stream_events=False):
                async def _gen():
                    yield _RunStartedFake(
                        run_id="boom-run",
                        session_id="s1",
                        parent_run_id="TOP",
                    )
                    raise RuntimeError("simulated mid-stream failure")

                return _gen()

            async def aget_run_output(self, **_kw):
                return None

            async def aget_last_run_output(self, **_kw):
                return None

        registry = OrchestrateTools._active_subagent_runs
        before = set(registry)

        with pytest.raises(RuntimeError, match="simulated"):
            await _run_agent_streaming(
                agent=_BoomAgent(),
                task="boom",
                on_progress=lambda _e: None,
                agent_path=["visualizer"],
            )

        assert "boom-run" not in registry, (
            "sub-agent run_id leaked after mid-stream exception — "
            "the finally-block cleanup is missing or incorrect"
        )
        assert registry == before

    @pytest.mark.asyncio
    async def test_event_log_uses_parent_top_run_id_not_subagent_run_id(self):
        """Regression: ``get_chat_history`` splices visualizer cards
        into the top-level chat history by pairing the Nth logged
        viz for a given ``run_id`` with the Nth ``spawn_agent`` /
        ``spawn_team`` tool turn IN the top-level history — whose
        ``run_id`` is the TOP-LEVEL run, not the visualizer
        sub-agent's own UUID run.

        Before this fix, ``_append_event`` was called with
        ``current_run_id`` (the sub-agent's own run_id from
        ``RunStartedEvent.run_id``), which never matched a
        spawn_agent tool turn's run_id and forced every viz to
        fall through to the tail-append fallback — the card would
        show up in the wrong position on reload.

        The fix: capture ``event.parent_run_id`` from the
        sub-agent's ``RunStartedEvent`` (that's the top-level run
        that spawned it) and use THAT for the event log entry.
        """
        tape = [
            _RunStartedFake(
                run_id="subagent-run",
                session_id="s1",
                parent_run_id="TOP-LEVEL-RUN",
            ),
            agent_events.ToolCallStartedEvent(
                agent_id="",
                agent_name="",
                tool=_ToolExecutionFake(
                    tool_name="visualize",
                    tool_args={
                        "spec": {"root": "r", "elements": {"r": {"type": "Text"}}}
                    },
                ),
            ),
        ]

        captured_calls: list[tuple[str, dict, str]] = []

        async def _fake_append(event_type: str, payload: dict, run_id: str):
            captured_calls.append((event_type, payload, run_id))

        original_appender = OrchestrateTools._append_event
        OrchestrateTools._append_event = staticmethod(_fake_append)  # type: ignore[method-assign]
        try:
            await _run_agent_streaming(
                agent=_MockAgent(tape),
                task="viz",
                on_progress=lambda _ev: None,
                agent_path=["visualizer"],
            )
        finally:
            OrchestrateTools._append_event = original_appender  # type: ignore[method-assign]

        assert len(captured_calls) == 1
        event_type, _payload, appended_run_id = captured_calls[0]
        assert event_type == "visualization_delta"
        # The KEY assertion — the appended entry uses the TOP-LEVEL
        # run_id (which lines up with the spawn_agent tool turn in
        # the main history), NOT the sub-agent's own run_id.
        assert appended_run_id == "TOP-LEVEL-RUN", (
            f"Expected top-level parent run_id 'TOP-LEVEL-RUN', "
            f"got {appended_run_id!r}. If this is 'subagent-run', "
            f"the get_chat_history splicing will misplace the "
            f"visualizer card on reload."
        )
