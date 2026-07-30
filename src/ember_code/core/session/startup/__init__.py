"""Session boot-time background warmups.

Sub-package split out of the old ``startup_ops.py`` monolith
(iter 145) — every warmup family (``knowledge``, ``codeindex``,
``marketplace``, ``mcp``) is a subclass of
:class:`SessionStartupPhase` in its own module.
:class:`SessionStartupCoordinator` composes one instance of each
phase and exposes the same public API Session / backend
already call.

Coverage:

* :class:`KnowledgeWarmupPhase` — open the ChromaDB client for
  the knowledge index (heavy transitive deps).
* :class:`CodeIndexWarmupPhase` — sweep stale chroma dirs, kick
  the resolver, run an initial sync, evict idle commit chromas,
  start the HEAD watcher.
* :class:`MarketplaceWarmupPhase` — refresh every registered
  plugin marketplace catalog + auto-register defaults on
  brand-new installs.
* :class:`McpInitPhase` — once-per-session first connect for
  user-configured MCP servers + agent rebuild. Sole owner of
  the ``_initialized`` flag.

Every background warmup is a no-op when no event loop is
running yet (``asyncio.get_running_loop`` raises
``RuntimeError``); the session's caller retries once the loop
is up. All failures are logged at WARNING / DEBUG and swallowed
— session boot must not be gated on a slow / offline external
dependency.
"""

from ember_code.core.session.startup.base import SessionStartupPhase
from ember_code.core.session.startup.codeindex import CodeIndexWarmupPhase
from ember_code.core.session.startup.coordinator import SessionStartupCoordinator
from ember_code.core.session.startup.knowledge import KnowledgeWarmupPhase
from ember_code.core.session.startup.marketplace import MarketplaceWarmupPhase
from ember_code.core.session.startup.mcp import McpInitPhase

__all__ = [
    "CodeIndexWarmupPhase",
    "KnowledgeWarmupPhase",
    "MarketplaceWarmupPhase",
    "McpInitPhase",
    "SessionStartupCoordinator",
    "SessionStartupPhase",
]
