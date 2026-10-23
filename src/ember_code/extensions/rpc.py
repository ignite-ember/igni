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
from typing import Any

from ember_code.extensions.registry import (
    NAMESPACE,
    ExtensionRefused,
    ExtensionRegistry,
)
from ember_code.extensions.service import ExtensionService, ExtensionServiceError

logger = logging.getLogger(__name__)


class ExtensionRpc:
    """Authorise a namespaced call, then route it to its service."""

    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry
        self._services: dict[str, ExtensionService] = {}

    @property
    def registry(self) -> ExtensionRegistry:
        return self._registry

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
            return await service.call(parsed.method, args)
        except ExtensionServiceError as exc:
            raise ExtensionRefused(str(exc)) from exc
