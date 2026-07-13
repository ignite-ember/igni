"""Session boot-time background warmups.

Extracted from :mod:`ember_code.core.session.core` — three
fire-and-forget tasks that run in parallel with session
construction so the user doesn't wait on heavy warmups on the
main path:

* :func:`start_knowledge_background` — open the ChromaDB
  client for the knowledge index (heavy transitive deps).
* :func:`start_codeindex_background` — sweep stale chroma
  dirs, kick the resolver, run an initial sync, evict idle
  commit chromas, start the HEAD watcher.
* :func:`start_marketplace_refresh_background` — refresh
  every registered plugin marketplace catalog + auto-register
  defaults on brand-new installs.

Every function is a no-op when no event loop is running yet
(``asyncio.get_running_loop`` raises ``RuntimeError``); the
Session's caller retries once the loop is up. All failures
are logged at WARNING and swallowed — session boot must not
be gated on a slow / offline external dependency.

:func:`ensure_knowledge_started` is the sibling async helper
that guarantees the chroma client is open before knowledge
ops run — cheap once :func:`start_knowledge_background` has
warmed it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ember_code.core.utils.display import print_info

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


def start_knowledge_background(session: "Session") -> None:
    """Open the chroma client + collections on a background task.

    Without this warmup, the first ``/knowledge`` press blocks
    while ``KnowledgeIndex.start()`` imports chromadb and opens
    the on-disk persistent client. Running it eagerly off the
    event loop lets the session finish booting while the
    warmup happens in parallel.
    """
    if session.knowledge is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # No running loop yet — caller will trigger us once one exists.

    async def _warmup() -> None:
        try:
            await session.knowledge.start()
        except Exception as exc:
            logger.debug("Knowledge warmup failed (%s); will retry lazily", exc)

    loop.create_task(_warmup())


async def ensure_knowledge_started(session: "Session") -> None:
    """Guarantee the chroma client is open before knowledge ops run.

    Cheap once :func:`start_knowledge_background` has warmed it —
    ``KnowledgeIndex.start()`` is idempotent and re-entry-safe
    via its internal lock.
    """
    if session.knowledge is None:
        return
    await session.knowledge.start()


def start_codeindex_background(session: "Session") -> None:
    """Fire an initial sync, evict stale commit chromas, and start
    the HEAD watcher — all fire-and-forget on the running loop.

    ``CodeIndex.clean()`` drops every commit that isn't HEAD,
    isn't a branch tip, and hasn't been touched in the last 30
    days. We run it once per session after the initial sync so
    the cutoff applies to a freshly-refreshed manifest.
    """

    async def _bootstrap() -> None:
        # Sweep orphaned chroma dirs from prior sessions BEFORE we
        # open any client. ``CodeIndex.clean`` defers the
        # filesystem ``rmtree`` until startup so it doesn't pull
        # the rug out from under a live chromadb client (same
        # trap that bit ``forget_commit`` in v0.5.8). The first
        # safe chance to finish that eviction is right here,
        # before ``sync_now`` constructs the first PersistentClient.
        try:
            swept = session.code_index.sweep_stale_dirs()
            if swept:
                logger.info(
                    "Reclaimed %d orphaned chroma dir(s): %s",
                    len(swept),
                    ", ".join(s[:8] for s in swept[:5]) + ("…" if len(swept) > 5 else ""),
                )
        except Exception as exc:
            logger.debug("sweep_stale_dirs failed (%s); continuing", exc)

        # Resolve the repository against the cloud once on
        # startup. ``sync_now`` short-circuits when HEAD is
        # already indexed locally (the common reattach case), so
        # without an explicit kick the resolver never runs and
        # the panel shows install_state="unknown" forever.
        try:
            resolver = session.code_index_sync.resolver
            if resolver is not None:
                await resolver.resolve()
        except Exception as exc:
            logger.debug("codeindex resolver kick failed (%s)", exc)
        await session.code_index_sync.sync_now()
        # If that initial sync populated the chroma (most common
        # case: fresh checkout, prior session wiped, first
        # install), the agent built earlier in __init__ has the
        # wrong system prompt — recheck and rebuild if so.
        try:
            session.refresh_codeindex_availability()
        except Exception as exc:
            logger.debug(
                "refresh_codeindex_availability after initial sync failed (%s)",
                exc,
            )
        try:
            dropped = await session.code_index.clean()
            if dropped:
                logger.info(
                    "Auto-clean dropped %d idle commit chroma(s): %s",
                    len(dropped),
                    ", ".join(s[:8] for s in dropped[:5]) + ("…" if len(dropped) > 5 else ""),
                )
        except Exception as exc:
            logger.debug("Auto-clean failed (%s); continuing", exc)
        await session.code_index_sync.start_watcher()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # No running loop yet — caller will trigger us once one exists.
    loop.create_task(_bootstrap())


def start_marketplace_refresh_background(session: "Session") -> None:
    """Refresh every registered plugin marketplace catalog + auto-
    register defaults, in the background.

    Mirrors :func:`start_codeindex_background` — fire-and-forget
    on the running loop, no throttle, per-marketplace timeout,
    all failures logged and swallowed. Net effect: by the time
    the user reaches for ``/plugin install`` (seconds to minutes
    later) the catalog is current. Session start is unaffected
    even if every marketplace is unreachable.
    """
    from ember_code.core.plugins.marketplaces import (
        load_registry,
        refresh_marketplace,
    )

    async def _refresh_all() -> None:
        # Auto-register the canonical defaults (Anthropic's
        # official marketplace, mainly) before refreshing so a
        # brand-new install sees plugins on first open without
        # the user having to run ``/plugin marketplace add``.
        # Idempotent — ``add_marketplace`` updates in place when
        # a marketplace by the same name already exists.
        from ember_code.core.plugins.marketplaces import (
            DEFAULT_MARKETPLACES,
            add_marketplace,
        )

        registry = load_registry(session.settings.storage.data_dir)
        registered_names = {m.name for m in registry.marketplaces}
        for default_name, default_url in DEFAULT_MARKETPLACES:
            if default_name in registered_names:
                continue
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        add_marketplace,
                        default_url,
                        data_dir=session.settings.storage.data_dir,
                    ),
                    timeout=15.0,
                )
                logger.info(
                    "Auto-registered default marketplace: %s",
                    default_name,
                )
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning(
                    "Auto-registering default marketplace '%s' "
                    "failed: %s — user can add manually later.",
                    default_name,
                    e,
                )

        # Refresh whatever is now registered (defaults + any
        # user-added marketplaces). Re-read the registry since
        # the auto-register step may have appended entries.
        registry = load_registry(session.settings.storage.data_dir)
        for entry in registry.marketplaces:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        refresh_marketplace,
                        entry.name,
                        data_dir=session.settings.storage.data_dir,
                    ),
                    timeout=10.0,
                )
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning(
                    "Marketplace refresh for '%s' failed: %s",
                    entry.name,
                    e,
                )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_refresh_all())


# ── MCP first-connect + rebuild ─────────────────────────────


async def ensure_mcp(session: "Session") -> None:
    """Connect user-configured MCP servers and rebuild agents.

    Reads from .mcp.json / .ember/.mcp.json. No auto-detection —
    only servers the user explicitly configured are connected.
    Runs once on first message. INFO-level log lines bracket
    each connect so the timeline is reconstructable from
    ``~/.ember/debug.log`` when diagnosing a "MCP says connected
    but the agent doesn't see the tools" race.
    """
    if session._mcp_initialized:
        return
    session._mcp_initialized = True

    available = session.mcp_manager.list_servers()
    if not available:
        logger.info("MCP init: no configured servers; skipping connect loop")
        return

    logger.info("MCP init: connecting %d server(s): %s", len(available), available)
    clients: dict = {}
    for name in available:
        t0 = asyncio.get_event_loop().time()
        client = await session.mcp_manager.connect(name)
        elapsed = asyncio.get_event_loop().time() - t0
        if client is not None:
            # Tool count surfaces the most common silent-failure
            # mode: server-side gating on auth that returns zero
            # tools. We let the connect succeed but flag the
            # empty case explicitly.
            tool_count = len(getattr(client, "functions", None) or {})
            logger.info(
                "MCP init: connected '%s' in %.2fs (%d tool(s))",
                name,
                elapsed,
                tool_count,
            )
            clients[name] = client
        else:
            error = session.mcp_manager.get_error(name)
            logger.info(
                "MCP init: connection to '%s' failed after %.2fs: %s",
                name,
                elapsed,
                error or "unknown error",
            )
            print_info(f"MCP '{name}' connection failed: {error or 'unknown error'}")

    if not clients:
        logger.info("MCP init: no clients to attach; team rebuild skipped")
        return

    # Rebuild agents with MCP tools included, then rebuild main team.
    logger.info(
        "MCP init: rebuilding agents + main team with %d MCP client(s)",
        len(clients),
    )
    session.pool.build_agents(mcp_clients=clients)
    session.main_team = session._build_main_agent()
    logger.info("MCP init: agents + main team rebuilt — tools active")


def rebuild_mcp(session: "Session") -> None:
    """Rebuild agents and main agent with current MCP client set.

    Called after toggling individual MCP servers on/off.
    """
    connected = session.mcp_manager.list_connected()
    clients = {name: session.mcp_manager._clients[name] for name in connected}
    session.pool.build_agents(mcp_clients=clients if clients else None)
    session.main_team = session._build_main_agent()
