"""Typed wire schemas for the json-render visualization action bus.

Extracted from :mod:`ember_code.backend.server` — the
``VisualizationActionResult`` wire type previously lived inline in
the god-class file. Sibling schemas modules follow this pattern.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VisualizationActionResult(BaseModel):
    """Wire shape for :meth:`VisualizationActionBus.dispatch` —
    the FE's tool result echo of the action name + user-supplied
    params so it can render "you clicked X" in the conversation."""

    ok: bool
    action: str
    params: dict = Field(default_factory=dict)
