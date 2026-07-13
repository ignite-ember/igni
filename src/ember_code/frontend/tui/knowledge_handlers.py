"""Knowledge panel event handlers for :class:`EmberApp`.

Extracted from ``tui/app.py``. Same pattern as
``codeindex_handlers.py`` etc.

Free functions taking ``app: EmberApp`` as first arg:

* :func:`show_knowledge_panel` — status RPC + mount + focus
  the search input.
* :func:`on_knowledge_search` — embed + ANN round-trip with a
  busy label so the panel doesn't look frozen.
* :func:`on_knowledge_add` — URL / path / inline ingest with
  the same busy-label treatment (ingest can take seconds).
* :func:`on_knowledge_panel_closed` — restore focus.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from textual.widgets import Input as _Input

from ember_code.frontend.tui.widgets import (
    KnowledgePanelWidget,
    PromptInput,
)

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


async def show_knowledge_panel(app: "EmberApp") -> None:
    """Fetch status, mount panel, focus the search input.

    Focuses the search input so the user can type immediately —
    the panel IS the search UI; opening it puts the cursor where
    the next keystroke is meaningful.
    """
    status = await app._backend.get_knowledge_status()
    panel = KnowledgePanelWidget(status=status)
    app.mount(panel)
    try:
        panel.query_one("#kb-input").focus()
    except Exception:
        panel.focus()


async def on_knowledge_search(app: "EmberApp", query: str) -> None:
    """Fire an embed + ANN search, refresh results.

    Flips the status line to "Searching…" so the panel doesn't
    look frozen during the round-trip. ``try/finally`` so an
    RPC failure still restores the static status.
    """
    try:
        panel = app.query_one(KnowledgePanelWidget)
    except Exception:
        return
    preview = query if len(query) <= 40 else query[:40] + "…"
    panel.set_busy(f"Searching for '{preview}'…")
    try:
        panel.set_results(await app._backend.knowledge_search(query))
    finally:
        panel.set_busy(None)


async def on_knowledge_add(app: "EmberApp", source: str) -> None:
    """Ingest a URL / path / inline text into the knowledge base.

    Ingest can take seconds (URL fetch + chunk + embed). Flip
    the status line so the user sees activity. URLs and long
    paths are trimmed in the label. On success, refreshes the
    panel status header + clears the KB input for the next add.
    """
    try:
        panel = app.query_one(KnowledgePanelWidget)
    except Exception:
        panel = None

    preview = source if len(source) <= 50 else source[:50] + "…"
    if panel is not None:
        panel.set_busy(f"Ingesting {preview}…")
    try:
        result = await app._backend.knowledge_add(source)
        app._conversation.append_info(result.text)
        # Refresh status header — doc count likely changed.
        if panel is not None:
            try:
                status = await app._backend.get_knowledge_status()
                panel.set_status(status)
                # Clear the input for the next add.
                panel.query_one("#kb-input", _Input).value = ""
            except Exception:
                pass
    finally:
        if panel is not None:
            panel.set_busy(None)


def on_knowledge_panel_closed(app: "EmberApp") -> None:
    """Restore focus to the prompt input."""
    app.query_one("#user-input", PromptInput).focus()
