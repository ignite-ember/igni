"""Agents / skills / hooks panel handlers for :class:`EmberApp`.

Extracted from ``tui/app.py``. Small panels grouped together
because their handler bodies are near-identical (RPC fetch,
build wire models, mount widget, react to events).

Free functions taking ``app: EmberApp`` as first arg:

* Agents: :func:`show_agents_panel`, :func:`build_agent_list`,
  :func:`refresh_agents_panel`, :func:`on_agent_promote`,
  :func:`on_agent_discard`, :func:`on_agents_panel_closed`.
* Skills: :func:`show_skills_panel`, :func:`build_skill_list`,
  :func:`on_skill_run`, :func:`on_skills_panel_closed`.
* Hooks: :func:`show_hooks_panel`, :func:`on_hooks_reload`,
  :func:`on_hooks_panel_closed`.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember_code.frontend.tui.widgets import (
    AgentInfo,
    AgentsPanelWidget,
    HookInfo,
    HooksPanelWidget,
    PromptInput,
    SkillInfo,
    SkillsPanelWidget,
)

if TYPE_CHECKING:
    from ember_code.frontend.tui.app import EmberApp

logger = logging.getLogger(__name__)


# ── Agents panel ──────────────────────────────────────────────


async def build_agent_list(app: "EmberApp") -> list[AgentInfo]:
    """Fetch agent details — the client already parses the wire
    dicts into :class:`AgentInfo`, so this is just a delegate."""
    return await app._backend.get_agent_details()


async def show_agents_panel(app: "EmberApp") -> None:
    """Fetch agent details and mount the panel."""
    agents = await build_agent_list(app)
    panel = AgentsPanelWidget(agents=agents)
    app.mount(panel)
    panel.focus()


async def refresh_agents_panel(app: "EmberApp") -> None:
    """Rebuild the agent list and push to the mounted panel."""
    try:
        panel = app.query_one(AgentsPanelWidget)
    except Exception:
        return
    panel.refresh_agents(await build_agent_list(app))


async def on_agent_promote(app: "EmberApp", name: str) -> None:
    """Promote an ephemeral agent to persistent; refresh panel."""
    result = await app._backend.promote_ephemeral_agent(name)
    app._conversation.append_info(result.text)
    await refresh_agents_panel(app)


async def on_agent_discard(app: "EmberApp", name: str) -> None:
    """Discard an ephemeral agent; refresh panel."""
    result = await app._backend.discard_ephemeral_agent(name)
    app._conversation.append_info(result.text)
    await refresh_agents_panel(app)


def on_agents_panel_closed(app: "EmberApp") -> None:
    """Restore focus to the prompt input."""
    app.query_one("#user-input", PromptInput).focus()


# ── Skills panel ──────────────────────────────────────────────


async def build_skill_list(app: "EmberApp") -> list[SkillInfo]:
    """Fetch skill details — client parses the wire dicts to
    :class:`SkillInfo`, so this is a 1-line delegate (matches
    :func:`build_agent_list`)."""
    return await app._backend.get_skill_details()


async def show_skills_panel(app: "EmberApp") -> None:
    """Fetch skill details and mount the panel."""
    skills = await build_skill_list(app)
    panel = SkillsPanelWidget(skills=skills)
    app.mount(panel)
    panel.focus()


async def on_skill_run(app: "EmberApp", name: str) -> None:
    """Close the skills panel then route the skill through the
    normal slash-command path so its output streams into the
    conversation the same way a typed ``/skill-name`` would."""
    # Close the panel before firing the skill so its output
    # streams into the conversation without being visually
    # shadowed.
    try:
        panel = app.query_one(SkillsPanelWidget)
        panel.remove()
    except Exception:
        pass
    await app._controller.process_message(f"/{name}")


def on_skills_panel_closed(app: "EmberApp") -> None:
    """Restore focus to the prompt input."""
    app.query_one("#user-input", PromptInput).focus()


# ── Hooks panel ───────────────────────────────────────────────


async def show_hooks_panel(app: "EmberApp") -> None:
    """Open the hooks panel. One RPC pulls the flat hook list;
    the widget groups by event on the client side."""
    hooks = await app._backend.get_hooks_details()
    panel = HooksPanelWidget(hooks=hooks)
    app.mount(panel)
    # Widget self-focuses in ``on_mount`` so we don't need a
    # parent-side ``.focus()`` (which would race with the
    # async mount).


async def on_hooks_reload(app: "EmberApp") -> None:
    """Reload hooks + refresh the panel with a busy label
    while the RPC is in flight."""
    try:
        panel = app.query_one(HooksPanelWidget)
    except Exception:
        return
    panel.set_busy("Reloading hooks…")
    try:
        result = await app._backend.reload_hooks()
        app._conversation.append_info(result.text)
        panel.set_hooks(await app._backend.get_hooks_details())
    finally:
        panel.set_busy(None)


def on_hooks_panel_closed(app: "EmberApp") -> None:
    """Restore focus to the prompt input."""
    app.query_one("#user-input", PromptInput).focus()
