"""Measure a warm base's igni: what it holds at ``ready``, and what binding costs.

``docs/CLOUD.md`` §9. Starts the backend in-process as a warm base does —
cloud profile, no producer — against a fake OpenAI-compatible model,
waits for ``ready``, then plays the node agent: writes the token and
``producer.env``. Measures

* RSS at ``ready`` (what the warm snapshot holds),
* bind latency: ``producer.env`` renamed into place → igni bound,
* bind → first answered request (a client retrying through the refusals),
* the first turn (a user message → ``stream_end``),

and lists the modules imported after ``ready`` — what
:class:`~igni_cloud.guest.warm.WarmImports` should add.

    igni migrate --database-url postgresql://...   # once, for --pg
    python scripts/measure_warm_base.py --out r.json [--pg URL] [--no-warm]

Backend logs go to standard output (the cloud profile's); the summary
goes to standard error and, in full, to ``--out``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import click
from igni_cloud.guest import WarmImports
from igni_cloud.guest.binding import ProducerBinding
from websockets.asyncio.client import connect

from ember_code.backend.app import BackendApp
from ember_code.backend.logging_setup import configure_logging
from ember_code.core.config.managed_policy import ManagedPolicySource


class FakeModel(BaseHTTPRequestHandler):
    """Answers every chat completion with a streamed "hi"."""

    def log_message(self, *_: Any) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        chunk = {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake",
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}
            ],
        }
        done = dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])
        for c in (chunk, done):
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


class WarmBaseMeasurement:
    def __init__(self, *, database_url: str | None, warm: bool) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="warm-base-"))
        self.run_dir = self.tmp / "run"
        self.project = self.tmp / "project"
        self.run_dir.mkdir()
        self.project.mkdir()
        (self.tmp / "home").mkdir()
        self.database_url = database_url or f"sqlite:///{self.tmp / 'shared.db'}"
        self.warm = warm
        self.bound_at: float | None = None

    def prepare(self) -> None:
        os.environ.update(
            {
                "IGNI_PROFILE": "cloud",
                "IGNI_RUN_DIR": str(self.run_dir),
                "HOME": str(self.tmp / "home"),
            }
        )
        for name in ("IGNI_DATABASE_URL", "IGNI_ACCESS_TOKEN_FILE", "IGNI_ACCESS_TOKEN"):
            os.environ.pop(name, None)
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        managed = self.tmp / "managed-settings.yaml"
        managed.write_text(
            json.dumps(
                {
                    "models": {
                        "default": "fake",
                        "registry": {
                            "fake": {
                                "provider": "openai_like",
                                "model_id": "fake",
                                "url": f"http://127.0.0.1:{server.server_address[1]}/v1",
                                "api_key": "x",
                                "context_window": 32000,
                            }
                        },
                    }
                }
            )
        )
        ManagedPolicySource.platform_path = staticmethod(lambda: managed)  # type: ignore[method-assign]
        if not self.warm:
            WarmImports.run = lambda self: None  # type: ignore[method-assign]
        measurement = self
        bind = ProducerBinding.bind

        async def timed_bind(binding: ProducerBinding, transport: Any = None) -> Any:
            env = await bind(binding, transport)
            measurement.bound_at = time.perf_counter()
            return env

        ProducerBinding.bind = timed_bind  # type: ignore[method-assign]

    @staticmethod
    def rss_mb() -> float:
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())])
        return round(int(out.strip()) / 1024, 1)

    @staticmethod
    def token() -> str:
        def part(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

        claims = {"sub": "u-ada", "org": "org-acme", "exp": int(time.time()) + 3600}
        return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"

    async def run(self) -> dict[str, Any]:
        app = BackendApp(
            socket_path=None,
            ws_port=0,
            project_dir=self.project,
            resume_session_id=None,
            additional_dirs=None,
        )
        started = time.perf_counter()
        task = asyncio.create_task(app.run())
        while not (self.run_dir / "ready").exists():
            await asyncio.sleep(0.005)
        ready_at = time.perf_counter()
        rss_ready = self.rss_mb()
        modules_at_ready = set(sys.modules)
        port = app._transport_builder.ws_transport.port
        (self.run_dir / "token").write_text(self.token())
        (self.run_dir / "producer.env.tmp").write_text(
            f"IGNI_ACCESS_TOKEN_FILE={self.run_dir / 'token'}\nIGNI_ORG=org-acme\n"
            f"IGNI_PRODUCER=person:u-ada\nIGNI_DATABASE_URL={self.database_url}\n"
        )
        async with connect(f"ws://127.0.0.1:{port}", max_size=None) as window:
            await window.recv()  # welcome
            handed_at = time.perf_counter()
            os.replace(self.run_dir / "producer.env.tmp", self.run_dir / "producer.env")
            answered_at = await self._first_answer(window)
            sent_at = time.perf_counter()
            await window.send(json.dumps({"type": "user_message", "id": "u1", "text": "say hi"}))
            last = ""
            while last not in ("stream_end", "run_error"):
                last = json.loads(await asyncio.wait_for(window.recv(), timeout=60)).get("type")
            turn_done_at = time.perf_counter()
        imported_after = sorted(set(sys.modules) - modules_at_ready)
        app._supervisor.request_shutdown()  # type: ignore[union-attr]
        try:
            await asyncio.wait_for(task, timeout=10)
        except Exception:
            task.cancel()
        assert self.bound_at is not None
        return {
            "warm": self.warm,
            "database": self.database_url.split(":", 1)[0],
            "start_to_ready_s": round(ready_at - started, 3),
            "rss_at_ready_mb": rss_ready,
            "bind_latency_ms": round((self.bound_at - handed_at) * 1000, 1),
            "bind_to_first_answer_s": round(answered_at - handed_at, 3),
            "first_turn_s": round(turn_done_at - sent_at, 3),
            "rss_after_turn_mb": self.rss_mb(),
            "last_event": last,
            "imported_after_ready": imported_after,
        }

    @staticmethod
    async def _first_answer(window: Any) -> float:
        attempt = 0
        while True:
            attempt += 1
            request_id = f"r{attempt}"
            await window.send(
                json.dumps(
                    {"type": "rpc_request", "id": request_id, "method": "get_status", "args": {}}
                )
            )
            while True:
                message = json.loads(await window.recv())
                if message.get("id") == request_id:
                    break
            if not message.get("error"):
                return time.perf_counter()
            await asyncio.sleep(0.002)


@click.command()
@click.option("--out", "out", type=click.Path(path_type=Path), required=True)
@click.option("--pg", "database_url", default=None, help="A migrated PostgreSQL database URL.")
@click.option("--no-warm", is_flag=True, default=False, help="Skip WarmImports, for a baseline.")
def main(out: Path, database_url: str | None, no_warm: bool) -> None:
    measurement = WarmBaseMeasurement(database_url=database_url, warm=not no_warm)
    measurement.prepare()
    configure_logging(debug_flag=False)
    result = asyncio.run(measurement.run())
    out.write_text(json.dumps(result, indent=1))
    summary = {k: v for k, v in result.items() if k != "imported_after_ready"}
    summary["imported_after_ready"] = len(result["imported_after_ready"])
    print(json.dumps(summary), file=sys.stderr)
    os._exit(0)


if __name__ == "__main__":
    main()
