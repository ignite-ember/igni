"""Explicit, named Agno-compatibility shim.

Replaces the pre-refactor import-time monkeypatch in the deleted
``agno_events.py``. The bug: Agno's team HITL streaming code calls
``run_response.agent_id`` on a :class:`TeamRunOutput` — which only
has ``team_id``. The workaround is to add ``agent_id`` / ``agent_name``
class attributes so attribute access resolves rather than
``AttributeError``-crashing the event creation.

The audit's AP6 finding: "invisible side effect at import time" —
importing the old module patched Agno globally, and any code path
that imported anything from that module (even ``TOOL_NAMES``) forced
the patch. We move it here behind a named class + explicit
``.apply()`` call so:

* The patch has a clear, greppable call site
  (:mod:`ember_code.protocol.__init__` — fired the first time any
  protocol symbol is imported, which is early enough that any
  team run is downstream of it).
* ``.apply()`` is idempotent (``._applied`` class flag) so
  repeated import cycles are harmless.
* A test that skips the protocol package can still opt in with a
  single call.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class AgnoCompatibilityShim:
    """One-shot patcher for Agno's TeamRunOutput agent_* attrs.

    Class-level state so a re-import doesn't re-patch; the shim is
    a singleton by convention (no instances exist — every operation
    is a classmethod).
    """

    _applied: bool = False

    @classmethod
    def is_applied(cls) -> bool:
        """Return whether :meth:`apply` has already run."""
        return cls._applied

    @classmethod
    def apply(cls) -> None:
        """Install the ``TeamRunOutput.agent_id / agent_name`` shim.

        Idempotent — subsequent calls short-circuit on the
        ``_applied`` flag so mistakenly wiring this into multiple
        boot paths is safe. On any import failure (Agno not
        installed, e.g. in a CLI-only smoke test) the call is a
        no-op — the shim only matters when Agno is actually
        driving a team run.
        """
        if cls._applied:
            return
        try:
            from agno.run.team import TeamRunOutput
        except ImportError:
            # Agno not installed in this environment — nothing to
            # patch, nothing will crash on the un-patched class.
            cls._applied = True
            return

        if not hasattr(TeamRunOutput, "agent_id"):
            TeamRunOutput.agent_id = None  # type: ignore[attr-defined]
        if not hasattr(TeamRunOutput, "agent_name"):
            TeamRunOutput.agent_name = None  # type: ignore[attr-defined]

        # Post-apply invariant: the two attrs must resolve. If a
        # future Agno release makes them descriptors that reject
        # None or raises inside the setter, we'd rather surface
        # the change here than see a mysterious AttributeError
        # deep in a team run.
        assert hasattr(TeamRunOutput, "agent_id"), (
            "AgnoCompatibilityShim: agent_id attribute still missing after patch"
        )
        assert hasattr(TeamRunOutput, "agent_name"), (
            "AgnoCompatibilityShim: agent_name attribute still missing after patch"
        )

        cls._applied = True
        logger.debug("AgnoCompatibilityShim applied: patched TeamRunOutput.agent_id/agent_name")


__all__ = ["AgnoCompatibilityShim"]
