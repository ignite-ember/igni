"""Run the decisions service: ``python -m ember_code.decisions_server``.

Two ways it runs:

* **Beside a desktop backend.** The backend starts it on first use
  (:class:`~ember_code.core.decisions.laya.LocalDecisions`) on a
  loopback port, with ``--ready-file`` (where the bound port is written)
  and ``--watch-stdin`` (it exits when the backend's end of its stdin
  closes, so it never outlives the backend), and ``--idle-seconds`` so
  an unused model gives its memory back.
* **Once per node in the cloud.** ``--host 0.0.0.0 --port 8730`` with no
  idle limit; every backend on the node points ``IGNI_DECISIONS_URL``
  at it.

The secret callers must send is ``IGNI_DECISIONS_SECRET``; it refuses
to start without one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from pathlib import Path

import click
import uvicorn

from ember_code.core.activity import background
from ember_code.decisions_server.app import DecisionsApp
from ember_code.decisions_server.model import LayaModel

SECRET_ENV = "IGNI_DECISIONS_SECRET"


class DecisionsServer:
    """One uvicorn server over :class:`DecisionsApp`, with the ways it stops itself."""

    #: How often the idle limit is checked.
    IDLE_POLL_S = 5.0

    def __init__(self, app: DecisionsApp, *, idle_seconds: float) -> None:
        self._app = app
        self._idle_seconds = idle_seconds
        self._server = uvicorn.Server(
            uvicorn.Config(app.asgi, log_level="warning", access_log=False, lifespan="off")
        )

    def serve(self, sock: socket.socket) -> None:
        asyncio.run(self._serve(sock))

    def stop(self) -> None:
        self._server.should_exit = True

    def watch_stdin(self) -> None:
        """Stop when whoever started us goes away (their end of our stdin closes)."""

        def drain() -> None:
            while sys.stdin.buffer.read(4096):
                pass
            self.stop()

        background.thread(drain, name="decisions-stdin")

    async def _serve(self, sock: socket.socket) -> None:
        idle = (
            background.daemon(self._idle_out(), name="decisions-idle")
            if self._idle_seconds > 0
            else None
        )
        try:
            await self._server.serve(sockets=[sock])
        finally:
            if idle is not None:
                idle.cancel()

    async def _idle_out(self) -> None:
        while not self._server.should_exit:
            await asyncio.sleep(self.IDLE_POLL_S)
            if self._app.activity.idle_for() >= self._idle_seconds:
                self.stop()


class ReadyFile:
    """Where the bound port is written, whole, once the socket is listening."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, port: int) -> None:
        partial = self._path.with_suffix(".partial")
        partial.write_text(str(port))
        os.replace(partial, self._path)


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=0, show_default=True, type=int, help="0 picks a free port.")
@click.option("--ready-file", type=click.Path(path_type=Path), default=None)
@click.option("--idle-seconds", default=0.0, show_default=True, help="0 never idles out.")
@click.option("--watch-stdin", is_flag=True, help="Exit when stdin closes.")
def main(
    host: str, port: int, ready_file: Path | None, idle_seconds: float, watch_stdin: bool
) -> None:
    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        raise click.UsageError(f"{SECRET_ENV} must be set")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    app = DecisionsApp(LayaModel(), secret)
    server = DecisionsServer(app, idle_seconds=idle_seconds)
    sock = socket.create_server((host, port))
    if ready_file is not None:
        ReadyFile(ready_file).write(sock.getsockname()[1])
    if watch_stdin:
        server.watch_stdin()
    server.serve(sock)


if __name__ == "__main__":
    main()
