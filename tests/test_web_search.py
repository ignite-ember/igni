"""Tests for ``WebSearchSpec`` — backend override + confirm-mode wiring.

Two layers:

- ``test_build_*``: prove the spec overrides the agno default of
  ``backend="duckduckgo"`` (which is broken in ddgs 9.x) with
  ``backend="auto"`` (which works).
- ``test_web_search_invokes_ddgs_with_auto_backend``: prove the
  override actually flows through to the ``ddgs.text`` call site, so a
  regression of the override (or of the agno forwarding) is caught
  even if the spec still constructs the toolkit with the right
  backend attribute.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ember_code.core.tools.tool_spec import ToolBuildContext, WebSearchSpec


@pytest.fixture
def ctx(tmp_path: Path) -> ToolBuildContext:
    """Minimal ``ToolBuildContext`` for ``WebSearchSpec.build`` —
    only ``base_dir`` is read by the WebSearch build path."""
    return ToolBuildContext(base_dir=tmp_path)


@pytest.mark.parametrize("confirm", [True, False])
def test_build_passes_backend_auto(ctx: ToolBuildContext, confirm: bool) -> None:
    """``WebSearchSpec`` must construct ``DuckDuckGoTools`` with
    ``backend="auto"`` so ``web_search`` routes through the working
    ``ddgs.text(backend="auto")`` path. ``ddgs.text(backend="duckduckgo")``
    raises ``DDGSException("No results found.")`` regardless of query
    on ddgs 9.x — the override is the fix for that bug.
    """
    spec = WebSearchSpec()
    toolkit = spec.build(ctx, confirm=confirm)
    # ``DuckDuckGoTools`` stores the backend on ``self.backend`` and
    # forwards it to ``ddgs.text`` / ``ddgs.news``. Pinning the field
    # here catches both regressions of the override and accidental
    # changes to the agno default.
    assert toolkit.backend == "auto"


def test_build_confirm_true_registers_confirmation_tools(ctx: ToolBuildContext) -> None:
    """Confirm mode should still register the legacy
    ``duckduckgo_search`` / ``duckduckgo_news`` function names so the
    BE's confirmation gate matches them. The fix must not drop this."""
    spec = WebSearchSpec()
    toolkit = spec.build(ctx, confirm=True)
    assert "duckduckgo_search" in toolkit.requires_confirmation_tools
    assert "duckduckgo_news" in toolkit.requires_confirmation_tools


def test_build_confirm_false_no_confirmation_tools(ctx: ToolBuildContext) -> None:
    """Without confirm mode, no tool should require confirmation —
    the legacy names exist only for the confirm-gate match above."""
    spec = WebSearchSpec()
    toolkit = spec.build(ctx, confirm=False)
    assert not getattr(toolkit, "requires_confirmation_tools", [])


def test_web_search_invokes_ddgs_with_auto_backend(ctx: ToolBuildContext) -> None:
    """End-to-end: invoking ``web_search`` must reach ``ddgs.text``
    with ``backend="auto"``. The previous parameter-only test would
    pass even if agno swallowed the override before calling
    ``ddgs.text`` — this one proves the actual call path matches.
    """
    spec = WebSearchSpec()
    toolkit = spec.build(ctx, confirm=False)

    # Mock the DDGS context manager so ``web_search`` can call
    # ``ddgs.text(...)`` without a network round-trip. We capture the
    # kwargs so we can assert on what the toolkit actually forwarded.
    fake_ddgs = MagicMock()
    fake_ddgs.text.return_value = [{"title": "stub", "href": "x", "body": ""}]
    with patch("agno.tools.websearch.DDGS") as ddgs_ctor:
        ddgs_ctor.return_value.__enter__.return_value = fake_ddgs
        ddgs_ctor.return_value.__exit__.return_value = False
        toolkit.web_search("vibethinker 3b", max_results=5)

    fake_ddgs.text.assert_called_once()
    forwarded = fake_ddgs.text.call_args.kwargs
    # The bug class: backend MUST be "auto", never "duckduckgo".
    assert forwarded["backend"] == "auto"
    assert forwarded["query"] == "vibethinker 3b"
    assert forwarded["max_results"] == 5
