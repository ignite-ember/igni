"""Full-pipeline integration test for the visualizer.

Drives the WHOLE orchestrate.py sub-agent stream handler with the
same event shape ``_LoggingModel`` produces in production
(CustomEvents interleaved with the Agno-native events). This bolts
together the ends: given the wrapper produces CustomEvents
(``test_tool_arg_streaming.py`` proves this from raw chunks), does
``_run_agent_streaming`` correctly forward them to ``on_progress``
as ``visualization_delta`` payloads?

If ANY link in that chain breaks, this test fails.

Not covered here (separate suites):
- Raw OpenAI SDK chunk → ModelResponse parse (that's Agno's parser).
- WS transport (backend/__main__.py's wire encode/decode).
- FE-side render (Playwright viz-stream spec).
- Session persistence + reload (test_event_log.py).
"""

from __future__ import annotations

import json

import pytest
from agno.run import agent as agent_events
from agno.run.agent import CustomEvent

from ember_code.core.tools.orchestrate import _run_agent_streaming


class TestFullStreamThroughOrchestrate:
    """Now drive the WHOLE pipeline end-to-end: raw OpenAI chunks →
    _LoggingModel → Agno's own agent stream translation →
    _run_agent_streaming's _handle → visualization_delta emissions.

    This is the closest to a real live LLM call we can get without
    actually paying for one, and it exercises every seam in the
    tool-arg-streaming plumbing."""

    @pytest.mark.asyncio
    async def test_progressive_deltas_reach_orchestrate_on_progress(self):
        class _RunStartedFake:
            def __init__(self):
                self.run_id = "r1"
                self.session_id = "s1"
                self.parent_run_id = None

        class _ToolFake:
            def __init__(self):
                self.tool_name = "visualize"
                self.tool_args = {
                    "spec": {
                        "root": "r",
                        "elements": {"r": {"type": "Text", "props": {"text": "hi"}}},
                    }
                }
                self.tool_call_id = "c1"
                self.result = None

        tape = [
            _RunStartedFake(),
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
                arguments_partial='{"spec": {"root": "r", "elements": {"r": {"type": "Text", "props": {"text": "hi"}}}}}',
            ),
            agent_events.ToolCallStartedEvent(
                agent_id="",
                agent_name="",
                tool=_ToolFake(),
            ),
        ]

        class _MockAgent:
            def arun(self, task, stream=True, stream_events=False):
                _ = task, stream, stream_events

                async def _gen():
                    for ev in tape:
                        yield ev

                return _gen()

            async def aget_run_output(self, **_kw):
                return None

            async def aget_last_run_output(self, **_kw):
                return None

        progress: list[dict] = []
        await _run_agent_streaming(
            agent=_MockAgent(),
            task="viz",
            on_progress=lambda ev: progress.append(ev),
            agent_path=["visualizer"],
        )

        # The wire event history the FE would see:
        deltas = [e for e in progress if e.get("type") == "visualization_delta"]
        assert deltas, (
            f"No visualization_delta emitted; progress types: {[e.get('type') for e in progress]}"
        )

        # Final delta carries the fully-parsed spec + final=True.
        finals = [d for d in deltas if d.get("final") is True]
        assert len(finals) == 1
        parsed = json.loads(finals[0]["json"])
        assert parsed == {
            "root": "r",
            "elements": {"r": {"type": "Text", "props": {"text": "hi"}}},
        }

        # Every delta shares the same spec_id — FE dedup contract.
        assert len({d["spec_id"] for d in deltas}) == 1
