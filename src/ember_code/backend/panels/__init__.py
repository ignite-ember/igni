"""Per-panel controllers for the FE panels concern.

The old :class:`PanelsController` in :mod:`server_panels` mixed
five distinct concerns (agents, hooks, skills, slash commands,
output styles) into one class. This subpackage decomposes each
into a focused single-responsibility controller, all sharing only
a :class:`Session` handle. The top-level
:class:`server_panels.PanelsController` retains its shape as a
thin composition facade so ``BackendServer.panels.<x>()`` and the
RPC router keep working.

Downstream code that wants direct access to one controller (e.g.
future per-panel unit tests) can import from here:

    from ember_code.backend.panels import HooksPanelController
"""

from ember_code.backend.panels.agents_panel import AgentsPanelController
from ember_code.backend.panels.hooks_panel import HooksPanelController
from ember_code.backend.panels.output_styles_catalog import OutputStylesCatalog
from ember_code.backend.panels.skills_panel import SkillsPanelController
from ember_code.backend.panels.slash_commands_catalog import SlashCommandsCatalog

__all__ = [
    "AgentsPanelController",
    "HooksPanelController",
    "OutputStylesCatalog",
    "SkillsPanelController",
    "SlashCommandsCatalog",
]
