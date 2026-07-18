"""Main-agent build sub-package.

Public surface:

* :class:`MainAgentBuilder` — coordinator class. Constructor
  takes a :class:`Session` plus injectable collaborators
  (``agent_cls`` / ``registry_cls`` / ``permissions_cls`` /
  ``compression_cls`` / ``model_registry_cls`` /
  ``reasoning_factory`` / ``guardrails_factory`` /
  ``prompt_loader``); the caller in
  :meth:`ember_code.core.session.core.Session._build_main_agent`
  passes the module-top symbols of :mod:`session.core`, which
  preserves the ``patch("ember_code.core.session.core.<Symbol>",
  …)`` test contract.
* :class:`AgentBuildSpec` — typed model for the 25-kwarg
  ``Agent(...)`` call.
* :class:`PlanModeNudge` — Pydantic renderer for the plan-mode
  instructions block.
"""

from __future__ import annotations

from .agent_build_spec import AgentBuildSpec
from .coordinator import MainAgentBuilder
from .plan_mode_nudge import PlanModeNudge

__all__ = [
    "AgentBuildSpec",
    "MainAgentBuilder",
    "PlanModeNudge",
]
