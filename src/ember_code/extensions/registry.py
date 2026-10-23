"""Which extensions are installed, and what each may be asked to do.

The backend had no idea extensions existed. ``RpcMethod`` is a closed
enum and :class:`~ember_code.backend.rpc_router.RpcRouter` validates
exhaustively against it, so ``ext.acme.scanner.list`` reached the
dispatcher, missed the table, and came back ``Unknown RPC method``.
Every call a third-party extension's UI could make was refused — an
extension could render igni's own data back at the user and nothing
else.

This is the registry that makes the namespace real: installed
manifests by id, the state each is in, and the authorisation decision
for one call.

**Authorisation is here and not in the client.** The web client
already checks the grant list before forwarding, and the comment
there says the backend re-checks it. That was aspirational — there
was nothing to re-check with. A frame is a renderer; its checks are a
courtesy that produces a good error message, and anything that
decides what may be read has to live where the data is.

Refusals name the fix. The reader is an extension author looking at
their own manifest, and "denied" without the missing line is a
support ticket.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from ember_code.extensions.manifest import ExtensionManifest

logger = logging.getLogger(__name__)

#: The namespace every extension's own methods live under. The id
#: follows, then the method.
NAMESPACE = "ext."


class ExtensionState(StrEnum):
    """What an extension is doing, as its health contribution reports.

    Always paired with a reason when it is not :attr:`READY` — MCP
    reports connectivity, and "licence expired, install the renewed
    key" is not connectivity.
    """

    READY = "READY"
    PREPARING = "PREPARING"
    DISABLED = "DISABLED"
    FAILED = "FAILED"


class ExtensionRefused(Exception):
    """A call that will not be made, and why.

    Carries the message that reaches the extension's author, so there
    is one wording rather than one per call site.
    """


@dataclass(frozen=True, slots=True)
class ExtensionMethod:
    """A parsed ``ext.<id>.<method>``."""

    extension_id: str
    method: str

    @property
    def wire(self) -> str:
        return f"{NAMESPACE}{self.extension_id}.{self.method}"

    @classmethod
    def parse(cls, wire: str) -> ExtensionMethod | None:
        """Split a wire name, or ``None`` if it is not in the namespace.

        **The method is the last segment and the id is everything
        before it.** ``ext.a.b.c`` is otherwise ambiguous — id ``a.b``
        and method ``c``, or id ``a`` and method ``b.c`` — and
        resolving that against the installed set would mean a call's
        meaning changed depending on what else was installed. A method
        name therefore contains no dots, which costs an extension
        nothing and makes the grammar decidable on its own.
        """
        if not wire.startswith(NAMESPACE):
            return None
        rest = wire[len(NAMESPACE) :]
        extension_id, separator, method = rest.rpartition(".")
        if not separator or not extension_id or not method:
            return None
        return cls(extension_id=extension_id, method=method)


@dataclass(slots=True)
class InstalledExtension:
    """One installed extension and its current state."""

    manifest: ExtensionManifest
    state: ExtensionState = ExtensionState.PREPARING
    #: Why it is not :attr:`ExtensionState.READY`, and what to do.
    reason: str = ""

    @property
    def id(self) -> str:
        return self.manifest.id


class ExtensionRegistry:
    """The installed set, and the authorisation decision for a call."""

    def __init__(self) -> None:
        self._installed: dict[str, InstalledExtension] = {}

    def install(
        self,
        manifest: ExtensionManifest,
        *,
        state: ExtensionState = ExtensionState.READY,
        reason: str = "",
    ) -> InstalledExtension:
        """Add or replace an extension.

        Replacing is how an upgrade lands. The manifest carries its
        own grants, so a version asking for more gets more only
        because an administrator installed it — which is the point at
        which they were shown the difference.
        """
        entry = InstalledExtension(manifest=manifest, state=state, reason=reason)
        self._installed[manifest.id] = entry
        logger.info("extension %s installed (%s)", manifest.id, state.value)
        return entry

    def remove(self, extension_id: str) -> bool:
        """Forget an extension. ``True`` if there was one."""
        return self._installed.pop(extension_id, None) is not None

    def get(self, extension_id: str) -> InstalledExtension | None:
        return self._installed.get(extension_id)

    def ids(self) -> list[str]:
        return sorted(self._installed)

    def set_state(self, extension_id: str, state: ExtensionState, reason: str = "") -> None:
        entry = self._installed.get(extension_id)
        if entry is None:
            return
        entry.state = state
        entry.reason = reason

    def authorise(self, *, caller: str | None, method: str) -> ExtensionMethod:
        """Decide whether ``caller`` may call ``method``.

        Returns the parsed call, or raises :class:`ExtensionRefused`
        with a sentence naming the fix.

        The four checks are ordered so the message is about the first
        thing wrong rather than a consequence of it.
        """
        parsed = ExtensionMethod.parse(method)
        if parsed is None:
            raise ExtensionRefused(
                f"{method!r} is not an extension method. "
                f"They are spelled '{NAMESPACE}<extension-id>.<method>', "
                "and the method is the final segment."
            )

        if caller is None:
            # Not an oversight to be lenient about. The namespace
            # exists so an extension can reach its own service, and a
            # request with no caller is either igni's own UI (which
            # has no business here) or a client too old to identify
            # itself (which cannot be trusted to be either).
            raise ExtensionRefused(
                f"'{parsed.wire}' is callable only from an extension's own frame, "
                "and this request did not say which extension it came from."
            )

        if caller != parsed.extension_id:
            # The id is in the method name, so without this an
            # extension could read any other extension's service by
            # spelling its namespace.
            raise ExtensionRefused(
                f"'{caller}' may not call '{parsed.wire}'. "
                "An extension reaches its own namespace and no other."
            )

        entry = self._installed.get(parsed.extension_id)
        if entry is None:
            raise ExtensionRefused(
                f"no extension '{parsed.extension_id}' is installed, "
                f"so '{parsed.wire}' has nowhere to go."
            )

        if entry.state is not ExtensionState.READY:
            detail = f" {entry.reason}" if entry.reason else ""
            raise ExtensionRefused(
                f"'{parsed.extension_id}' is {entry.state.value}, not READY, "
                f"so '{parsed.wire}' cannot be served.{detail}"
            )

        if not entry.manifest.grants_rpc(parsed.wire):
            raise ExtensionRefused(
                f"'{parsed.extension_id}' is not permitted to call '{parsed.wire}'. "
                f"Add it to permissions.rpc in the manifest — "
                f'"{NAMESPACE}{parsed.extension_id}.*" grants the whole namespace — '
                "and reinstall."
            )

        return parsed
