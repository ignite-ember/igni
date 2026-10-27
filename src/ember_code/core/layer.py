"""The seams a deployment layer plugs into, and the one place a layer is chosen.

``docs/CLOUD.md`` §9. igni is one product: the desktop igni is the core,
and the cloud is a **layer** on top of it — a separate package
(``igni-cloud``) that replaces a handful of the core's choices. The core
never asks where it runs; it asks the current :class:`Layer`, whose
methods answer with the desktop's own choice unless a layer says
otherwise:

* where logs go (:meth:`Layer.log_files`, :meth:`Layer.log_handler`);
* where the person's credentials come from (:meth:`Layer.credentials`);
* whether settings are read from files under ``~/.igni`` and the
  workspace (:meth:`Layer.reads_local_settings`);
* whether state must be in a shared database (:meth:`Layer.requires_database`);
* whether what igni server's listing carries is kept in files
  (:meth:`Layer.keeps_listing_in_files`);
* how an MCP server is approved (:meth:`Layer.mcp_approvals`);
* whether the platform's calls need its control connection
  (:meth:`Layer.platform_calls_need_control`), and whether igni fetches
  the person's service events itself (:meth:`Layer.fetches_own_events`);
* what a backend does around its boot and serving (:meth:`Layer.backend`).

**Choosing one.** :class:`Layers` reads ``IGNI_PROFILE`` — here and
nowhere else — once, the first time any seam is asked. Unset (or
``desktop``), the core's own :class:`Layer`; any other name is the entry
point of that name in the ``igni.layers`` group, which a layer's package
declares (``igni-cloud`` declares ``cloud``). An entry point and not an
explicit ``igni-cloud serve`` command so that every way igni starts —
the backend, ``igni migrate``, the CLI — gets the same layer from one
setting the image already has, and a desktop install, which never sets
it, never imports a line of cloud code. A name nothing declares is an
error, said once, loudly: a cloud image without its layer must not run
as a desktop.

Storage that is not one SQLite file per project is a different seam, by
SQL dialect rather than by deployment — :mod:`ember_code.core.db.providers`.
"""

from __future__ import annotations

import logging
import os
import threading
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from ember_code.core.auth.credentials import CredentialSource
    from ember_code.core.mcp.approval import McpApprovalPolicy

logger = logging.getLogger(__name__)


class BackendHooks:
    """What a layer does around a backend's boot and serving. The desktop does nothing.

    Called by :class:`~ember_code.backend.app.BackendApp`, in order:
    :meth:`starting`, :meth:`listening` once the transport listens,
    :meth:`bind` before any settings or database are opened,
    :meth:`serving` once the backend serves, :meth:`stop` at exit.
    """

    #: Whose rows a shared database holds, if the layer decides it.
    tenant: Any = None

    def starting(self) -> None:
        pass

    def listening(self) -> None:
        pass

    async def bind(self, transport: Any) -> bool:
        """Wait for whatever the layer needs before igni may open its state.

        ``True`` when something was bound — the backend then changes into
        its project directory again, which may have been mounted since.
        """
        return False

    def serving(
        self,
        *,
        model: Any,
        transport: Any,
        orchestrator: Any,
        notifications: Any = None,
        on_rebind: Any = None,
        api_url: Any = None,
    ) -> None:
        pass

    async def stop(self) -> None:
        pass


class Layer:
    """The desktop's choices. A layer subclasses it and overrides what it changes."""

    name: ClassVar[str] = "desktop"

    def log_files(self) -> bool:
        """Logs are files under ``~/.igni``; ``False`` sends them to :meth:`log_handler`."""
        return True

    def log_handler(self) -> logging.Handler:
        """Where every log goes when :meth:`log_files` is ``False``."""
        raise NotImplementedError("the desktop writes log files")

    def credentials(self) -> CredentialSource | None:
        """Where credentials come from instead of ``credentials.json``; ``None`` is the file."""
        return None

    def reads_local_settings(self) -> bool:
        """Settings files under ``~/.igni`` and the workspace's ``.igni`` count."""
        return True

    def requires_database(self) -> bool:
        """State must be in a shared database (``IGNI_DATABASE_URL``)."""
        return False

    def keeps_listing_in_files(self) -> bool:
        """The extension priority and group igni server lists are kept in files for the next start."""
        return True

    def mcp_approvals(self) -> McpApprovalPolicy | None:
        """How MCP servers are approved instead of the desktop's file and prompt; ``None`` is those."""
        return None

    def platform_calls_need_control(self) -> bool:
        """The platform's calls (``deliver_service_events``, ``vm_sign_in``) are taken
        only from a control connection (``X-Igni-Gateway: control``)."""
        return False

    def fetches_own_events(self) -> bool:
        """igni pulls the person's service events itself, with their session, while online."""
        return True

    def backend(self) -> BackendHooks:
        return BackendHooks()


class UnknownLayer(RuntimeError):
    """``IGNI_PROFILE`` names a layer no installed package declares."""


class Layers:
    """The process's layer: chosen once, from ``IGNI_PROFILE``."""

    #: The one setting that chooses it.
    ENV: ClassVar[str] = "IGNI_PROFILE"
    #: Where a layer's package declares it.
    GROUP: ClassVar[str] = "igni.layers"

    _lock: ClassVar[threading.Lock] = threading.Lock()
    _current: ClassVar[Layer | None] = None

    @classmethod
    def current(cls) -> Layer:
        layer = cls._current
        if layer is not None:
            return layer
        with cls._lock:
            if cls._current is None:
                cls._current = cls._load()
            return cls._current

    @classmethod
    def install(cls, layer: Layer) -> None:
        """Use ``layer`` from now on (a layer's own tests do)."""
        with cls._lock:
            cls._current = layer

    @classmethod
    def reset(cls) -> None:
        """Choose again on the next ask (tests)."""
        with cls._lock:
            cls._current = None

    @classmethod
    def requested(cls) -> str:
        return os.environ.get(cls.ENV, "").strip().lower() or Layer.name

    @classmethod
    def _load(cls) -> Layer:
        name = cls.requested()
        if name == Layer.name:
            return Layer()
        found = [ep for ep in entry_points(group=cls.GROUP) if ep.name == name]
        if not found:
            raise UnknownLayer(
                f"{cls.ENV}={name!r}: no installed package declares that layer "
                f"(entry point group {cls.GROUP!r}); install it, or unset {cls.ENV}"
            )
        layer = found[0].load()()
        if not isinstance(layer, Layer):
            raise UnknownLayer(f"{cls.ENV}={name!r}: {found[0].value} is not a Layer")
        logger.info("running with the %s layer", name)
        return layer


__all__ = ["BackendHooks", "Layer", "Layers", "UnknownLayer"]
