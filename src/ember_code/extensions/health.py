"""Is this extension still the thing the registry says it is?

``ExtensionState`` was written once, at install, and never again. That
was survivable while nothing could install; it stopped being
survivable the moment one could. A service that dies ten minutes
later leaves the registry saying ``READY``, so ``list_extensions``
tells the client ``READY``, the panel draws, and the first call comes
back as a connection error instead of a state with a reason — which
is precisely the shape the health contract exists to avoid. The UI
already knows how to render ``FAILED`` with a reason; it simply never
saw one after install.

Two triggers, because they catch different things:

* **A periodic probe**, which notices a service that died quietly —
  nobody was calling it, so nothing failed, and the next person to
  open the panel would have been the detector.
* **A failed call**, which asks for a probe immediately. Waiting out
  the interval after a user has already seen something go wrong means
  the panel contradicts them for up to that long.

**Recovery is attempted, and only when the service is already dead.**
``MCPClientManager.disconnect_one`` drops its reference without
terminating the process — a deliberate choice there, because MCP's
``__aexit__`` raises across task boundaries — so restarting something
still running would leak it until igni exits. Restarting something
already dead leaks nothing, which is why the probe decides and this
does not restart on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from ember_code.extensions.installer import server_name
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service import McpExtensionService

logger = logging.getLogger(__name__)

#: How often to ask, when nothing has gone wrong.
DEFAULT_INTERVAL_S = 30.0

#: How long a service has to answer a probe before it counts as gone.
#: Generous: a probe is a tool listing, and a busy sidecar answering
#: slowly is not a dead one.
PROBE_TIMEOUT_S = 10.0

#: Shortest gap between restart attempts for one extension. A service
#: that fails on startup fails every time, and retrying it every
#: probe would spawn a process a minute forever.
RESTART_BACKOFF_S = 60.0


class ExtensionHealth:
    """Keeps each extension's recorded state true."""

    def __init__(
        self,
        *,
        registry: ExtensionRegistry,
        rpc: ExtensionRpc,
        mcp: Any,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._registry = registry
        self._rpc = rpc
        self._mcp = mcp
        self._interval = interval_s
        self._task: asyncio.Task | None = None
        self._last_restart: dict[str, float] = {}
        # Probes for one extension are serialised: a failed call and
        # the periodic sweep can land together, and two concurrent
        # restarts of one service is two subprocesses.
        self._locks: dict[str, asyncio.Lock] = {}
        # Running probe tasks, held so the event loop does not
        # garbage-collect one mid-flight — the documented way to lose
        # a task silently. An instance attribute, not a class one: as
        # a class attribute every monitor in the process would share
        # one set.
        self._pending: set[asyncio.Task] = set()

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Begin probing. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="extension-health")

    @property
    def pending_probes(self) -> int:
        """How many probes are in flight. For tests and for a
        shutdown that wants to know whether it waited."""
        return len(self._pending)

    async def stop(self) -> None:
        """Stop probing, including probes already in flight.

        The in-flight ones matter as much as the loop. A probe
        triggered by a failed call can be partway through a restart —
        disconnecting and reconnecting an MCP client — when the
        backend shuts down, and leaving it running means it races
        whatever tears the manager down next. That race hangs rather
        than crashes, which is the worst way for it to show up.
        """
        task, self._task = self._task, None
        pending = list(self._pending)
        self._pending.clear()

        for running in [t for t in (task, *pending) if t is not None]:
            running.cancel()
        for running in [t for t in (task, *pending) if t is not None]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await running

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self.probe_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A probe sweep that raises must not take the loop
                # with it: the next sweep is the recovery path for
                # whatever went wrong here.
                logger.exception("extension health sweep failed")

    # ── Probing ────────────────────────────────────────────────────

    async def probe_all(self) -> None:
        """Check every installed extension."""
        for extension_id in self._registry.ids():
            await self.probe(extension_id)

    async def probe(self, extension_id: str) -> ExtensionState | None:
        """Check one, update its state, and return it.

        ``None`` when there is no such extension. A ``DISABLED``
        extension is left alone — that is somebody's decision, not a
        fault, and probing it would argue with them.
        """
        entry = self._registry.get(extension_id)
        if entry is None:
            return None
        if entry.state is ExtensionState.DISABLED:
            return entry.state

        lock = self._locks.setdefault(extension_id, asyncio.Lock())
        async with lock:
            reason = await self._ask(extension_id)
            if reason is None:
                if entry.state is not ExtensionState.READY:
                    logger.info("extension %s is answering again", extension_id)
                    self._registry.set_state(extension_id, ExtensionState.READY)
                return ExtensionState.READY

            if entry.state is ExtensionState.READY:
                logger.warning("extension %s stopped answering: %s", extension_id, reason)
            self._registry.set_state(extension_id, ExtensionState.FAILED, reason)

            if await self._restart(extension_id):
                return ExtensionState.READY
            return ExtensionState.FAILED

    async def _ask(self, extension_id: str) -> str | None:
        """Ping the service. Returns a reason on failure.

        A tool listing rather than a dedicated health tool: every MCP
        server answers it, so it works against a service whose author
        never read our contract, and it exercises the same session a
        real call would.
        """
        client = self._mcp.get_client(server_name(extension_id))
        if client is None:
            return "its service is not connected."
        try:
            session = await asyncio.wait_for(client.get_session_for_run(), timeout=PROBE_TIMEOUT_S)
            await asyncio.wait_for(session.list_tools(), timeout=PROBE_TIMEOUT_S)
        except TimeoutError:
            return f"its service did not answer within {PROBE_TIMEOUT_S:.0f}s."
        except Exception as exc:
            return f"its service stopped answering: {exc}"
        return None

    async def _restart(self, extension_id: str) -> bool:
        """Try to bring a dead service back. ``True`` if it answered.

        Only reached once :meth:`_ask` has said it is gone, which is
        what makes dropping the connection safe — see the module
        docstring.
        """
        now = time.monotonic()
        last = self._last_restart.get(extension_id)
        if last is not None and now - last < RESTART_BACKOFF_S:
            return False
        self._last_restart[extension_id] = now

        name = server_name(extension_id)
        logger.info("restarting extension %s", extension_id)
        try:
            await self._mcp.disconnect_one(name)
            result = await self._mcp.connect(name)
        except Exception as exc:
            self._registry.set_state(
                extension_id, ExtensionState.FAILED, f"its service could not be restarted: {exc}"
            )
            return False

        if not getattr(result, "ok", False):
            reason = getattr(result, "reason", "") or "no reason given"
            self._registry.set_state(
                extension_id,
                ExtensionState.FAILED,
                f"its service did not restart: {reason}",
            )
            return False

        # The binding holds a server name rather than a session, so it
        # survives a reconnect — but rebinding is cheap and makes that
        # independence explicit rather than something a future change
        # could quietly break.
        self._rpc.bind(extension_id, McpExtensionService(manager=self._mcp, server_name=name))
        self._registry.set_state(extension_id, ExtensionState.READY)
        logger.info("extension %s restarted", extension_id)
        return True

    # ── The call path's hook ───────────────────────────────────────

    def on_call_failed(self, extension_id: str) -> None:
        """A call to this extension failed; find out whether it is gone.

        Fire-and-forget on purpose. The caller is answering an RPC and
        has its own error to return; making it wait on a probe would
        add this timeout to a failure the user is already seeing.
        """
        if self._registry.get(extension_id) is None:
            return
        task = asyncio.create_task(self._probe_quietly(extension_id), name=f"probe-{extension_id}")
        # Held so the loop does not garbage-collect a running task,
        # which is the documented way to lose one silently.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _probe_quietly(self, extension_id: str) -> None:
        try:
            await self.probe(extension_id)
        except Exception:
            logger.exception("probe of %s failed", extension_id)
