"""VisualizeTools — agent-facing tool for pushing a json-render UI spec
to attached clients.

The visualizer sub-agent (``bundled_agents/visualizer.md``) owns the
json-render schema in its system prompt and produces a spec conforming
to it. This tool is the one-way door that carries the spec across the
BE→FE boundary — the FE renders the spec via ``@json-render/react``.

Design notes:

- **No server-side validation.** ``@json-render/core`` ships
  ``validateSpec`` / ``autoFixSpec``; the FE's ``Renderer`` also
  falls through to a placeholder for unknown components. Duplicating
  validation in Python invents a schema we don't own and drifts from
  the library. We forward whatever the model produced and let the
  client be the source of truth on shape.
- **No idempotency guard.** Streaming means multiple ``visualize``
  invocations per run are legitimate — each is a patch/refinement of
  the same card. Deduping happens on the FE via a stable
  ``spec_id`` (the sub-agent's ``run_id`` when we have one, else a
  toolkit-scoped uuid). One card per stream, updated in place.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from agno.tools import Toolkit

logger = logging.getLogger(__name__)


BroadcastFn = Callable[[str, dict], None]


class VisualizeTools(Toolkit):
    """Single-method toolkit: ``visualize(spec, title)``.

    Each toolkit instance carries a stable ``spec_id`` used by the FE
    to deduplicate: multiple calls with the same id update one card
    instead of appending. Sub-agents get a fresh instance per spawn,
    so ``spec_id`` naturally maps to "one card per visualizer run".
    """

    def __init__(self, broadcast: BroadcastFn | None = None) -> None:
        super().__init__(name="ember_visualize")
        self._broadcast: BroadcastFn | None = broadcast
        # Stable id for the FE's card-dedup logic. Regenerated per
        # toolkit instance, so each sub-agent spawn gets a new one and
        # doesn't collide with previously-emitted cards.
        self._spec_id: str = uuid.uuid4().hex[:12]
        self.register(self.visualize)

    def wire(self, broadcast: BroadcastFn) -> None:
        """Attach or replace the broadcast callable. Used by the
        orchestrator when it spawns a sub-agent whose tools were built
        by a session-less path (``AgentPool.build_agent``) and only
        gain a live broadcast at spawn time."""
        self._broadcast = broadcast

    async def visualize(
        self,
        spec: dict,
        title: str = "",
    ) -> str:
        """Emit a json-render spec to every attached client.

        The client mounts the spec via ``@json-render/react`` inside a
        dedicated chat item, so the visualization lands inline in the
        conversation next to the reply. Payload channel is
        ``"visualization"``.

        Args:
            spec: A json-render spec of the form
                ``{"root": "<id>", "elements": {"<id>": {"type": ..., "props": ..., "children": [...]}}}``.
                Passed to the FE verbatim — no server-side validation;
                ``@json-render/core``'s ``validateSpec`` /
                ``Renderer`` fallback handle malformed input.
            title: Short human-readable title shown above the rendered
                spec (e.g. ``"AAPL — Monthly Close"``). Optional.

        Returns a one-line confirmation so the model has something to
        recap. Never puts the spec back into the model's context —
        keep the payload one-way.
        """
        if self._broadcast is None:
            logger.debug("visualize: no broadcast wired — dropping payload")
            return "Emitted visualization (no attached clients)."

        payload: dict[str, Any] = {"spec": spec, "spec_id": self._spec_id}
        if title:
            payload["title"] = title
        try:
            self._broadcast("visualization", payload)
        except Exception as exc:
            logger.warning("visualize broadcast raised: %s", exc)
            return f"Error: broadcast failed — {exc}"

        return "Emitted visualization."
