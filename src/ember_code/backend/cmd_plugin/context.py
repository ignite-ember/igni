"""Shared value bag every verb / router receives at construction.

Replaces the god-coordinator's ``_handler`` / ``_session`` /
``_cmd_module`` / ``_data_dir`` bundle with a single frozen
dataclass carrying the three collaborators a verb actually needs:
the :class:`Session` (for hot-reload + plugin_loader / plugin_state),
the resolved plugin data directory (snapshotted at command entry
from ``session.plugin_data_dir``), and the
:class:`PluginBackendGateway` seam every git-/marketplace-backed
call routes through.

Constructed per-command by the :class:`SlashCommand` at
``run(handler, args)``, then handed to the router → verb chain.
Never cached across commands — matches the pre-refactor "read
fresh from settings on every command entry" pattern that lets a
mid-session settings reload propagate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ember_code.backend.cmd_plugin.gateway import PluginBackendGateway
    from ember_code.core.session import Session


@dataclass(frozen=True)
class SlashCommandContext:
    """Immutable per-command context handed to every verb.

    ``@dataclass(frozen=True)`` (not Pydantic) so we can hold the
    :class:`Session` — a live object with unserialisable state —
    without wrestling with ``arbitrary_types_allowed``.
    """

    session: Session
    plugin_data_dir: str
    gateway: PluginBackendGateway


__all__ = ["SlashCommandContext"]
