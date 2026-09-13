"""Backend process entry point.

Usage: python -m ember_code.backend --socket /tmp/ember-code/<uuid>.sock

Starts a :class:`BackendApp`, listens on the given Unix socket
and/or WebSocket port, and processes FE messages until shutdown.
The real work lives in :mod:`ember_code.backend.app` — this module
is the CLI + the module-load side effects (logging config).

For backward compatibility with existing tests that reach in for
``_build_rpc_table``, ``_handle_message``, ``_serialize``, or the
wire schemas that used to live inline, this module re-exports
those symbols as thin shims. New code should import from the
themed modules directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import click

# Backward-compat re-exports: keep the old import paths working for
# in-tree tests. New callers should import from the themed modules.
from ember_code.backend.app import BackendApp
from ember_code.backend.login_coordinator import LoginCoordinator
from ember_code.backend.message_dispatcher import MessageDispatcher, _serialize
from ember_code.backend.push_bridge import PushNotificationBridge
from ember_code.backend.rpc_router import RpcRouter
from ember_code.backend.schemas_rpc import (
    AttachSessionResult,
    BackendReadyLine,
    DirListResult,
    DisplayConfigResult,
    FileCompletion,
    GetClientStateResult,
    LifecyclePhase,
    LoginStarted,
    ModelRegistryResult,
    PickDirResult,
    RunShellResult,
    SessionPoolConfig,
    SkillDefinition,
    UpdateAvailable,
    WriteClientStateResult,
)

__all__ = [
    "AttachSessionResult",
    "BackendApp",
    "BackendReadyLine",
    "DirListResult",
    "DisplayConfigResult",
    "FileCompletion",
    "GetClientStateResult",
    "LifecyclePhase",
    "LoginStarted",
    "ModelRegistryResult",
    "PickDirResult",
    "RunShellResult",
    "SessionPoolConfig",
    "SkillDefinition",
    "UpdateAvailable",
    "WriteClientStateResult",
    "_build_rpc_table",
    "_handle_message",
    "_serialize",
    "main",
]

logger = logging.getLogger(__name__)


def _attach_package_log_handler(log_path: Path) -> None:
    """Give ``ember_code`` its own file handler, off the root logger.

    ``basicConfig`` alone is not enough here, and this module already
    knew it: see the note in :mod:`ember_code.transport.websocket`
    about downstream imports (httpx, litellm, …) resetting root
    handlers between startup and first use. When that happens the
    root file handler goes with them and the log simply stops — which
    it did, five lines in, while the BE ran on for minutes.

    So this mirrors what the chunk trace does: a handler on the
    package logger, flushed per record so ``tail -f`` is honest, and
    idempotent so a second call cannot double every line.
    """

    class _FlushingFileHandler(logging.FileHandler):
        def emit(self, record: logging.LogRecord) -> None:
            super().emit(record)
            self.flush()

    pkg = logging.getLogger("ember_code")
    if any(getattr(h, "_ember_debug_log", False) for h in pkg.handlers):
        return
    handler = _FlushingFileHandler(str(log_path))
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    handler.setLevel(logging.DEBUG)
    handler._ember_debug_log = True  # type: ignore[attr-defined]
    pkg.addHandler(handler)
    pkg.setLevel(logging.DEBUG)


@click.command()
@click.option("--socket", "socket_path", default=None, help="Unix socket path")
@click.option(
    "--ws-port",
    "ws_port",
    type=int,
    default=None,
    help="Listen on a loopback WebSocket port instead of a Unix socket "
    "(0 = auto-assign; the bound port is printed in the ready line). "
    "Used by GUI clients (Tauri / VSCode / JetBrains webviews).",
)
@click.option("--project-dir", type=click.Path(exists=True), default=".")
@click.option("--resume-session", "resume_session_id", default=None)
@click.option("--additional-dirs", multiple=True, default=())
@click.option("--debug", is_flag=True, default=False)
def main(
    socket_path: str | None,
    ws_port: int | None,
    project_dir: str,
    resume_session_id: str | None,
    additional_dirs: tuple[str, ...],
    debug: bool,
) -> None:
    """Start the Ember Code backend server."""
    if socket_path is None and ws_port is None:
        raise click.UsageError("at least one of --socket or --ws-port is required")
    # ``--debug`` is unreachable for the backend the desktop app
    # spawns: the app builds the argv, and nothing in the UI edits it.
    # So the only BE anyone actually runs is the one that cannot be
    # asked for logs — which is how a failing knowledge attach came to
    # report nothing anywhere. Its stdout goes to the app (which reads
    # it for the ready line and drops the rest), so "no log file" means
    # no record at all.
    #
    # The env var is the same switch by a route that survives being a
    # child process: the app already passes ``EMBER_NEO4J_RUNTIME``
    # down this way, and a child inherits the environment it cannot
    # inherit a command line from.
    if debug or os.environ.get("EMBER_DEBUG_LOG"):
        log_path = Path(
            os.environ.get("EMBER_DEBUG_LOG_PATH") or (Path.home() / ".ember" / "debug.log")
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=str(log_path),
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            force=True,
        )
        _attach_package_log_handler(log_path)

    extra_dirs = [Path(d) for d in additional_dirs] if additional_dirs else None
    # Canonicalise the project dir so two clients pointing at the
    # "same" folder via slightly different paths (``/tmp`` vs
    # ``/private/tmp`` on macOS, symlink resolution, trailing slash)
    # both land on the same ``.ember/state.db`` and see identical
    # session lists. ``strict=False`` lets us keep going if the
    # directory doesn't yet exist — startup will create it.
    resolved_project = Path(project_dir).resolve(strict=False)
    app = BackendApp(
        socket_path=socket_path,
        ws_port=ws_port,
        project_dir=resolved_project,
        resume_session_id=resume_session_id,
        additional_dirs=extra_dirs,
    )
    asyncio.run(app.run())


# ── Legacy shims (tests still import these) ─────────────────────


def _build_rpc_table(
    backend: Any, transport: Any, login_state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Test seam — build the RPC dispatch table without booting the
    full :class:`BackendApp`. Used by ``tests/test_*_rpc*.py`` and
    the plan / todo / process / output-styles / search-chat suites
    to invoke individual RPC handlers directly.

    ``login_state`` is accepted for backward compatibility; the
    live login task now lives on :class:`LoginCoordinator`.
    """
    # Bridge without a loop — the sync RPC handlers this test
    # surface reaches don't dispatch pushes.
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    push = PushNotificationBridge(transport=transport, loop=loop, queue=[])
    login = LoginCoordinator(backend=backend, push_bridge=push)
    router = RpcRouter(backend=backend, transport=transport, login=login, push=push)
    return router.build_table()


async def _handle_message(
    message: Any,
    backend: Any,
    transport: Any,
    rpc_table: dict,
    queue: list[str],
    login_state: dict[str, Any] | None = None,
) -> None:
    """Legacy signature preserved for
    ``tests/test_multi_session_integration.py`` (drives the real
    per-message dispatcher through the production path).

    The new dispatcher is :class:`MessageDispatcher`. We build one
    per call — construction is cheap, and the tests each call this
    once per message.
    """
    dispatcher = MessageDispatcher(
        backend=backend,
        transport=transport,
        rpc_table=rpc_table,
        queue=queue,
        login=None,  # login coordinator not required for the paths tests exercise
    )
    await dispatcher.dispatch(message)


if __name__ == "__main__":
    main()
