"""Tests for ``VisualizeTools`` — the visualizer sub-agent's
render tool.

Note on architecture: the tool's ``visualize`` method itself is a
near-no-op. The visualization payload has ALREADY been streamed to
the FE by the time this method runs — see
``_LoggingModel.process_response_stream`` +
``orchestrate.py``'s ``CustomEvent`` handler for the pipeline that
turns each streaming tool_call arg fragment into a
``visualization_delta`` event, and the ``ToolCallStartedEvent``
branch that emits the final ``final=True`` delta once args are
complete. This tool exists so the sub-agent has an actual tool to
CALL (the whole point of switching from content-stream to tool
call), and so ``VisualizeTools`` shows up in ``ToolRegistry``.

Covers:
- Tool is discoverable via ``ToolRegistry`` under the ``Visualize`` name.
- ``visualize()`` returns a friendly confirmation string for any spec
  shape; no exceptions on unusual input.
- Registry wiring is idempotent (no per-broadcast state to leak).
"""

from __future__ import annotations

import pytest

from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.tools.registry import ToolRegistry
from ember_code.core.tools.visualize import VisualizeTools


class TestVisualizeTool:
    @pytest.mark.asyncio
    async def test_returns_confirmation_string(self):
        # The tool's runtime job is to give the model something to
        # recap; the wire-side rendering is already done by the
        # ``CustomEvent`` interceptor.
        tool = VisualizeTools()
        spec = {
            "root": "r",
            "elements": {
                "r": {"type": "Text", "props": {"text": "hello"}, "children": []},
            },
        }
        result = await tool.visualize(spec, title="Hi")
        assert "Emitted visualization" in result

    @pytest.mark.asyncio
    async def test_handles_empty_title(self):
        tool = VisualizeTools()
        spec = {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        # Empty title should not raise; the FE renders without a
        # heading in that case.
        result = await tool.visualize(spec, title="")
        assert "Emitted visualization" in result

    @pytest.mark.asyncio
    async def test_handles_minimal_spec(self):
        # A degenerate spec (just root + one leaf) still parses at
        # the tool level. Any deeper validation lives on the FE via
        # ``@json-render/core`` — we don't reinvent it here.
        tool = VisualizeTools()
        result = await tool.visualize(
            {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        )
        assert "Emitted visualization" in result

    @pytest.mark.asyncio
    async def test_no_broadcast_is_no_op(self):
        # The legacy broadcast wire is now unused (the streaming
        # interceptor does the delivery). Constructing with
        # ``broadcast=None`` must still succeed and be silent —
        # older callers might still pass one.
        tool = VisualizeTools(broadcast=None)
        result = await tool.visualize(
            {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        )
        assert "Emitted visualization" in result


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
    async def test_registry_wired_tool_still_executes(self):
        # Broadcast-wired vs unwired: both must run to completion.
        # This is a smoke test for the registry integration path,
        # not the delivery mechanism (which is elsewhere).
        reg = ToolRegistry(
            base_dir="/tmp",
            permissions=ToolPermissions(project_dir=None),
            broadcast=lambda _c, _p: None,
        )
        (tool,) = reg.resolve(["Visualize"])
        result = await tool.visualize(
            {"root": "r", "elements": {"r": {"type": "Text", "props": {}}}}
        )
        assert "Emitted visualization" in result
