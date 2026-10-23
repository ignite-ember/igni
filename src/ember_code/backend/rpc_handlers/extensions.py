"""The base side of the extension surface.

One method so far: what is installed. An extension's *own* methods do
not come through here — they are ``ext.<id>.<method>``, routed by
prefix in :mod:`ember_code.extensions.rpc` because they arrive with a
manifest rather than at import time.

This exists so the frontend can draw an extension at all. Until it
did, an installed extension answered calls and had nowhere to put
them: nothing told the client which extensions existed, what state
they were in, or which loopback port their UI was on.
"""

from __future__ import annotations

from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.backend.schemas_extensions import ExtensionListResult, ExtensionSummary
from ember_code.protocol.rpc import RpcMethod


class ExtensionsRpcHandler(RpcHandler):
    """What the client needs to render installed extensions."""

    @rpc(RpcMethod.LIST_EXTENSIONS)
    async def list_extensions(self, args: dict) -> ExtensionListResult:
        """Every installed extension, including the broken ones.

        A ``FAILED`` extension is returned with its reason rather than
        filtered out. An administrator looking for "why is Compliance
        missing" is better served by a row that says why than by an
        absence they have to interpret.
        """
        extensions = self._ctx.extensions
        if extensions is None:
            # A backend built without extension support. An empty list
            # is the truthful answer: nothing is installed, and
            # nothing can be.
            return ExtensionListResult()

        origins = self._ctx.origins
        registry = extensions.registry
        return ExtensionListResult(
            extensions=[
                ExtensionSummary.of(
                    entry,
                    origins.url_for(extension_id) if origins is not None else None,
                )
                for extension_id in registry.ids()
                if (entry := registry.get(extension_id)) is not None
            ]
        )
