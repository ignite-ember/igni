"""The ``ext.<id>.*`` RPC namespace.

One entry point: :meth:`ExtensionRpc.dispatch`, called by
:class:`~ember_code.backend.message_dispatcher.MessageDispatcher` when
a method is not in the base table and is in the extension namespace.

Why a fallthrough and not an enum member: ``RpcMethod`` is closed on
purpose, and closing it is what gives the base its "every method is
registered or boot fails" guarantee. Extension methods are not known
at import time — they arrive with a manifest — so they cannot be enum
members without giving that up for every method igni has. They get a
prefix instead, and the prefix is checked by a registry rather than a
table.

The order inside :meth:`dispatch` matters: authorise, then route.
A refusal must not depend on whether the extension's service happens
to be reachable, or an author debugging a permission problem gets a
connection error and goes looking in the wrong place.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ember_code.extensions.faults import ExtensionFaults
from ember_code.extensions.registry import (
    NAMESPACE,
    ExtensionRefused,
    ExtensionRegistry,
    ExtensionState,
)
from ember_code.extensions.service import ExtensionService, ExtensionServiceError

logger = logging.getLogger(__name__)


class ExtensionRpc:
    """Authorise a namespaced call, then route it to its service."""

    def __init__(self, registry: ExtensionRegistry, faults: ExtensionFaults | None = None) -> None:
        self._registry = registry
        self._faults = faults if faults is not None else ExtensionFaults.shared()
        self._faults.attach(registry)
        self._services: dict[str, ExtensionService] = {}
        self._on_failure: Callable[[str], None] | None = None

    @property
    def registry(self) -> ExtensionRegistry:
        return self._registry

    def watch(self, on_failure: Callable[[str], None]) -> None:
        """Be told when a call to an extension fails.

        The health monitor uses this to probe immediately rather than
        waiting out its interval. Without it the panel keeps saying
        READY for up to that long after the user has already watched
        something fail, which is the app contradicting them.

        Called synchronously and expected not to block — a caller
        here is in the middle of answering an RPC.
        """
        self._on_failure = on_failure

    def bind(self, extension_id: str, service: ExtensionService) -> None:
        """Attach the thing that answers for ``extension_id``."""
        self._services[extension_id] = service

    def unbind(self, extension_id: str) -> None:
        self._services.pop(extension_id, None)

    @staticmethod
    def owns(method: str) -> bool:
        """Whether this method belongs to the extension namespace.

        A plain prefix test, deliberately: anything under ``ext.``
        is answered here, including a malformed name. Letting a
        malformed one fall back to the base table would produce
        ``Unknown RPC method``, which is the message that hid this
        whole gap for as long as it did.
        """
        return method.startswith(NAMESPACE)

    async def invoke_command(self, extension_id: str, command: str, text: str) -> Any:
        """Run a slash command an extension declared.

        **A different grant from `permissions.rpc`**, and deliberately
        so. That list is what an extension's own *UI* may call, and
        this is the *user* typing a command igni offered them —
        igni is the caller, not a frame, so there is no frame to
        authenticate. What authorises it is that the command appears
        in `contributes.commands` on a manifest an administrator
        installed.

        The state check is the same one every call gets: a command
        belonging to an extension that is not READY is refused with
        its reason rather than hanging on a dead service.
        """
        entry = self._registry.get(extension_id)
        if entry is None:
            raise ExtensionRefused(f"no extension '{extension_id}' is installed.")
        if entry.state is not ExtensionState.READY:
            detail = f" {entry.reason}" if entry.reason else ""
            slash = f"/{command.lstrip('/')}"
            raise ExtensionRefused(
                f"'{extension_id}' is {entry.state.value}, not READY, so "
                f"'{slash}' cannot run.{detail}"
            )

        bare = command.lstrip("/").strip().lower()
        declared = {
            entry_command.command.lstrip("/").lower()
            for entry_command in entry.manifest.contributes.commands
        }
        if bare not in declared:
            raise ExtensionRefused(
                f"'{extension_id}' does not declare '/{bare}'. Add it to "
                "contributes.commands in the manifest and reinstall."
            )

        service = self._services.get(extension_id)
        if service is None:
            raise ExtensionRefused(
                f"'{extension_id}' is installed but has no service bound, so igni cannot reach it."
            )
        try:
            # The command is the tool name, so `/compliance` runs the
            # tool `compliance`. One rule, and it is the same one
            # `ext.<id>.<method>` already uses.
            return await self._call(extension_id, service, bare, {"args": text})
        except ExtensionServiceError as exc:
            if self._on_failure is not None:
                self._on_failure(extension_id)
            raise ExtensionRefused(str(exc)) from exc

    async def dispatch(self, *, caller: str | None, method: str, args: dict[str, Any]) -> Any:
        """Serve one call, or raise with a sentence naming the fix."""
        parsed = self._registry.authorise(caller=caller, method=method)

        service = self._services.get(parsed.extension_id)
        if service is None:
            # Installed, READY, and granted, but nothing is bound.
            # That is an igni bug rather than a manifest problem, and
            # the message says so rather than sending the author back
            # to their permissions.
            raise ExtensionRefused(
                f"'{parsed.extension_id}' is installed but has no service bound, "
                "so igni cannot reach it. This is a fault in igni, not in the "
                "extension's manifest."
            )

        logger.debug("extension rpc %s -> %s", method, parsed.method)
        try:
            return await self._call(parsed.extension_id, service, parsed.method, args)
        except ExtensionServiceError as exc:
            # The service answered badly or not at all. Whether it is
            # gone is a question for the health monitor — a failed
            # call is a symptom, and a tool that raises is not a dead
            # service.
            if self._on_failure is not None:
                self._on_failure(parsed.extension_id)
            raise ExtensionRefused(str(exc)) from exc

    async def _call(
        self, extension_id: str, service: ExtensionService, method: str, args: dict[str, Any]
    ) -> Any:
        """One call, counted: a timeout or a crash toward faulty, an answer clearing it."""
        try:
            result = await service.call(method, args)
        except ExtensionServiceError as exc:
            if exc.unanswered:
                self._faults.failed(extension_id, f"its call {method}: {exc}")
            else:
                self._faults.answered(extension_id)
            raise
        self._faults.answered(extension_id)
        return result
