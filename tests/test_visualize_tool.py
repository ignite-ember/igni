"""Tests for ``VisualizeTools`` — the json-render one-way emitter.

Covers:
- ``VisualizeTools.visualize`` broadcasts on the ``visualization``
  channel with the correct payload shape, including a stable
  ``spec_id`` for FE-side dedup.
- ``VisualizeTools.visualize`` no-ops (doesn't raise) when no
  broadcast is wired — headless / test contexts stay quiet.
- Multiple calls in one instance share the ``spec_id`` — that's the
  on-ramp for streaming: the FE updates one card in place.
- The ``Visualize`` tool is discoverable via ``ToolRegistry`` and
  correctly forwards the registry's ``broadcast`` into the toolkit.
- End-to-end AAPL demo: a real spec broadcasts as expected.

Note: no server-side validation tests here — validation is intentionally
delegated to ``@json-render/core``'s ``validateSpec`` / ``Renderer``
fallback on the client. Reimplementing schema validation in Python
would drift from the library.
"""

from __future__ import annotations

import pytest

from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.tools.registry import ToolRegistry
from ember_code.core.tools.visualize import VisualizeTools


# ── Broadcast contract ────────────────────────────────────────────


class TestVisualizeTool:
    @pytest.mark.asyncio
    async def test_valid_spec_broadcasts_on_visualization_channel(self):
        pushes: list[tuple[str, dict]] = []
        tool = VisualizeTools(broadcast=lambda ch, payload: pushes.append((ch, payload)))
        spec = {
            "root": "r",
            "elements": {
                "r": {"type": "Text", "props": {"text": "hello"}, "children": []},
            },
        }
        result = await tool.visualize(spec, title="Hi")
        assert "Emitted visualization" in result
        assert len(pushes) == 1
        channel, payload = pushes[0]
        assert channel == "visualization"
        assert payload["title"] == "Hi"
        assert payload["spec"] == spec
        assert isinstance(payload["spec_id"], str) and payload["spec_id"]

    @pytest.mark.asyncio
    async def test_repeated_calls_share_spec_id(self):
        # Streaming path: multiple calls per sub-agent run must land
        # on the same FE card. The toolkit's per-instance spec_id
        # guarantees that — the FE dedups on it.
        pushes: list[tuple[str, dict]] = []
        tool = VisualizeTools(broadcast=lambda ch, payload: pushes.append((ch, payload)))
        spec1 = {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        spec2 = {"root": "r", "elements": {"r": {"type": "Card", "props": {}}}}
        await tool.visualize(spec1)
        await tool.visualize(spec2, title="second")
        assert len(pushes) == 2
        assert pushes[0][1]["spec_id"] == pushes[1][1]["spec_id"]

    @pytest.mark.asyncio
    async def test_distinct_instances_get_distinct_spec_ids(self):
        # Fresh toolkit = fresh spec_id, so two visualizer sub-agent
        # runs in the same session don't collide on the FE.
        t1 = VisualizeTools(broadcast=lambda _c, _p: None)
        t2 = VisualizeTools(broadcast=lambda _c, _p: None)
        pushes: list[tuple[str, dict]] = []
        t1._broadcast = lambda c, p: pushes.append((c, p))  # noqa: SLF001
        t2._broadcast = lambda c, p: pushes.append((c, p))  # noqa: SLF001
        spec = {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        await t1.visualize(spec)
        await t2.visualize(spec)
        assert pushes[0][1]["spec_id"] != pushes[1][1]["spec_id"]

    @pytest.mark.asyncio
    async def test_no_broadcast_returns_friendly_message(self):
        # Headless / tests / disconnected clients: no broadcast wired.
        # Must NOT raise — the tool degrades to a no-op with a message
        # so the model doesn't loop retrying.
        tool = VisualizeTools(broadcast=None)
        spec = {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        result = await tool.visualize(spec)
        assert "no attached clients" in result.lower()

    @pytest.mark.asyncio
    async def test_broadcast_omits_empty_title(self):
        pushes: list[tuple[str, dict]] = []
        tool = VisualizeTools(broadcast=lambda ch, payload: pushes.append((ch, payload)))
        spec = {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        await tool.visualize(spec, title="")
        assert "title" not in pushes[0][1]

    @pytest.mark.asyncio
    async def test_broadcast_failure_surfaces_as_error(self):
        def boom(_ch, _payload):
            raise RuntimeError("kaboom")

        tool = VisualizeTools(broadcast=boom)
        spec = {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        result = await tool.visualize(spec)
        assert result.startswith("Error:")
        assert "kaboom" in result


# ── Registry integration ──────────────────────────────────────────


class TestRegistryWiring:
    def test_visualize_appears_in_available_tools(self):
        reg = ToolRegistry(
            base_dir="/tmp",
            permissions=ToolPermissions(project_dir=None),
        )
        assert "Visualize" in reg.available_tools

    def test_resolve_returns_visualize_tools_instance(self):
        reg = ToolRegistry(
            base_dir="/tmp",
            permissions=ToolPermissions(project_dir=None),
        )
        tools = reg.resolve(["Visualize"])
        assert len(tools) == 1
        assert isinstance(tools[0], VisualizeTools)

    @pytest.mark.asyncio
    async def test_registry_threaded_broadcast_reaches_tool(self):
        pushes: list[tuple[str, dict]] = []

        def bcast(ch, payload):
            pushes.append((ch, payload))

        reg = ToolRegistry(
            base_dir="/tmp",
            permissions=ToolPermissions(project_dir=None),
            broadcast=bcast,
        )
        (tool,) = reg.resolve(["Visualize"])
        spec = {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        await tool.visualize(spec)
        assert len(pushes) == 1
        assert pushes[0][0] == "visualization"


# ── End-to-end demo ───────────────────────────────────────────────


class TestAAPLDemo:
    """The canonical Apple stock demo — the same spec the
    visualizer.md example shows. Kept as a test so a schema drift
    breaks CI, not the demo in prod."""

    AAPL_2023_MONTHLY = [
        {"x": "Jan", "y": 143.00},
        {"x": "Feb", "y": 147.41},
        {"x": "Mar", "y": 164.90},
        {"x": "Apr", "y": 169.68},
        {"x": "May", "y": 177.25},
        {"x": "Jun", "y": 193.97},
        {"x": "Jul", "y": 196.45},
        {"x": "Aug", "y": 187.87},
        {"x": "Sep", "y": 171.21},
        {"x": "Oct", "y": 170.77},
        {"x": "Nov", "y": 189.95},
        {"x": "Dec", "y": 192.53},
    ]

    def _spec(self):
        return {
            "root": "root",
            "elements": {
                "root": {
                    "type": "Card",
                    "props": {"title": "AAPL — Monthly Close", "subtitle": "2023"},
                    "children": ["chart"],
                },
                "chart": {
                    "type": "LineGraph",
                    "props": {
                        "yPrefix": "$",
                        "xLabel": "Month",
                        "yLabel": "Close",
                        "data": self.AAPL_2023_MONTHLY,
                    },
                    "children": [],
                },
            },
        }

    @pytest.mark.asyncio
    async def test_aapl_spec_broadcasts_end_to_end(self):
        pushes: list[tuple[str, dict]] = []
        tool = VisualizeTools(broadcast=lambda c, p: pushes.append((c, p)))
        result = await tool.visualize(self._spec(), title="AAPL 2023")
        assert "Emitted visualization" in result
        assert pushes and pushes[0][0] == "visualization"
        payload = pushes[0][1]
        assert payload["title"] == "AAPL 2023"
        assert len(payload["spec"]["elements"]["chart"]["props"]["data"]) == 12
