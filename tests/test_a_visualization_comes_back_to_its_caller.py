"""What the agent that commissioned a chart actually receives.

The visualizer streams its spec to the client as a card and replies
in prose — "Rendered a 6-element 'Database Migration' card". For an
orchestrator showing the user something, that is the whole job and
the prose is enough.

It is not enough for the archivist. A document carrying a
``json-render`` fence renders the chart in ``ArtifactPanel``; a
document carrying a *description* of a chart renders nothing. So the
spec has to come back as data, and it comes back through the activity
log — the channel ``RUN ERROR`` already uses to lift something out of
a sub-agent run and into the parent's result.

The property under test is the one that makes it worth doing:
**verbatim**. A caller that copies the block through cannot get the
shape wrong. A caller re-serialising forty elements from a summary
invents numbers, which is the failure the visualizer's own prompt
spends a paragraph forbidding.
"""

from __future__ import annotations

import json

from ember_code.core.tools.orchestrate_events import VIS_SPEC_PREFIX
from ember_code.core.tools.orchestrate_spawn import AgentSpawn

SPEC = {
    "root": "kpi",
    "elements": {
        "kpi": {
            "type": "Metric",
            "props": {"label": "Rows migrated", "value": 4820},
            "children": [],
        }
    },
    "state": {"total": 12500},
}


def _activity_with_spec(spec: dict) -> list[str]:
    return [
        "  tool visualize(...)",
        f"{VIS_SPEC_PREFIX}{json.dumps(spec)}",
    ]


class TestTheSpecComesBack:
    def test_it_is_fenced_as_json_render(self):
        out = AgentSpawn._rendered_specs(_activity_with_spec(SPEC))

        assert "```json-render" in out

    def test_the_json_survives_the_round_trip_unchanged(self):
        """Not "contains the numbers" — parsed back and compared.

        A fence that is subtly re-encoded is worse than no fence: it
        renders, and it renders something slightly other than what
        was drawn.
        """
        out = AgentSpawn._rendered_specs(_activity_with_spec(SPEC))
        body = out.split("```json-render\n")[1].split("\n```")[0]

        assert json.loads(body) == SPEC

    def test_two_charts_come_back_as_two_fences(self):
        second = {"root": "t", "elements": {}, "state": {}}
        activity = _activity_with_spec(SPEC) + [f"{VIS_SPEC_PREFIX}{json.dumps(second)}"]

        assert AgentSpawn._rendered_specs(activity).count("```json-render") == 2

    def test_a_run_that_drew_nothing_adds_nothing(self):
        """Every ordinary spawn goes through this path. It must not
        grow a trailing section saying no chart was drawn."""
        assert AgentSpawn._rendered_specs(["  tool read_file(...)"]) == ""

    def test_a_run_error_is_not_mistaken_for_a_spec(self):
        assert AgentSpawn._rendered_specs(["  ⚠ RUN ERROR: boom"]) == ""


class TestItIsStillTheOldResult:
    """The hand-back is additive. Everything that read this string
    before — and the corpus is full of traces that do — still finds
    what it expects."""

    class _Spawn(AgentSpawn):
        def __init__(self):  # noqa: D107 — bypasses the real wiring
            self._agent_name = "visualizer"
            self._agent_desc = "Renders structured data as UI"
            self._agent_tools = "visualize"
            self._task = "chart the migration"
            self._sandbox = None

    def test_the_header_and_response_are_untouched(self):
        out = self._Spawn().format_result(
            elapsed=1.0, result="Rendered a 1-element card.", activity=_activity_with_spec(SPEC)
        )

        assert out.startswith("[Agent: visualizer]")
        assert "Response:\nRendered a 1-element card." in out

    def test_the_spec_follows_the_response(self):
        out = self._Spawn().format_result(
            elapsed=1.0, result="Done.", activity=_activity_with_spec(SPEC)
        )

        assert out.index("Response:") < out.index("```json-render")

    def test_a_run_error_still_warns(self):
        out = self._Spawn().format_result(
            elapsed=1.0,
            result="partial",
            activity=_activity_with_spec(SPEC) + ["  ⚠ RUN ERROR: boom"],
        )

        assert "WARNING: This sub-agent terminated with a run error" in out
        assert "```json-render" in out
