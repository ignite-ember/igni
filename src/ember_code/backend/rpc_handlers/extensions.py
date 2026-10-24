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
from ember_code.extensions.store import ExtensionStore
from ember_code.extensions.web_home import ExtensionWebHome
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
        store = ExtensionStore()
        web_home = ExtensionWebHome(store)
        return ExtensionListResult(
            extensions=[
                ExtensionSummary.of(
                    entry,
                    origins.url_for(extension_id) if origins is not None else None,
                    store.settings(entry.manifest),
                    web_home.url_for(entry.manifest),
                )
                for extension_id in registry.ids()
                if (entry := registry.get(extension_id)) is not None
            ]
        )

    @rpc(RpcMethod.SET_EXTENSION_SETTINGS)
    async def set_extension_settings(self, args: dict) -> ExtensionListResult:
        """Store settings an administrator changed.

        Returns the whole list rather than the one row: the caller is
        a settings surface that has just invalidated its own view of
        everything, and one round trip is simpler than a merge it
        would have to get right.

        The values reach the extension as launch params on its next
        mount. Not pushed to a running frame — `params` can carry
        them, but an extension that reconfigured itself mid-render is
        a harder promise than this is worth until somebody asks.
        """
        extensions = self._ctx.extensions
        if extensions is None:
            raise ValueError("this backend has no extension support")

        extension_id = str(args.get("extension_id") or "")
        entry = extensions.registry.get(extension_id)
        if entry is None:
            raise ValueError(f"no extension '{extension_id}' is installed")

        values = args.get("values")
        if not isinstance(values, dict):
            raise ValueError("values must be an object of setting key to value")

        ExtensionStore().save_settings(entry.manifest, values)
        return await self.list_extensions({})
