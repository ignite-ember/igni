"""Laya, asked over HTTP: the backend's client for igni's decisions service.

Laya answers typed questions — choice, score, yes/no — about a short
text. It brings torch and ~1.7 GB of checkpoints, so it does not run in
the backend: it runs in the decisions service
(:mod:`ember_code.decisions_server`), and this client asks it. Two
callers share it:

* the Bypass guard (:mod:`ember_code.core.hooks.bypass_guard`), on the
  approval path, with the ``typed-decisions`` checkpoint it was
  measured on (:meth:`Laya.predict`);
* extensions, through ``host.decide``, with Laya's general checkpoints
  (:meth:`Laya.decide`).

The service puts approvals first, across every backend it serves.

Where the service is:

* ``IGNI_DECISIONS_URL`` set (the cloud: one service per node) —
  :class:`RemoteDecisions`, sending ``IGNI_DECISIONS_TOKEN``;
* otherwise (a desktop) — :class:`LocalDecisions`, which starts the
  service on first use as its own process on a loopback port, with a
  per-run secret, and starts it again if it has idled out.

Anything that keeps an answer from coming back — the service will not
start, cannot be reached, or cannot load the model — is
:class:`~.schemas.DecisionsUnavailable`: never a guess.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from ember_code.core.decisions.schemas import (
    DecideAnswer,
    DecideCall,
    Decision,
    DecisionRequest,
    DecisionsFailure,
    DecisionsUnavailable,
    LayaReading,
    PredictAnswer,
    PredictCall,
    Priority,
)

logger = logging.getLogger(__name__)

Answer = TypeVar("Answer", bound=BaseModel)

URL_ENV = "IGNI_DECISIONS_URL"
TOKEN_ENV = "IGNI_DECISIONS_TOKEN"
SECRET_ENV = "IGNI_DECISIONS_SECRET"


class Connection(BaseModel):
    """Where to send a call, and with what."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    base_url: str
    token: str
    transport: httpx.AsyncBaseTransport | None = None


class DecisionsEndpoint(Protocol):
    """Where the decisions service is. Blocking; called off the event loop."""

    def connection(self) -> Connection: ...

    def lost(self) -> None:
        """The last call could not connect; find the service again next time."""


class RemoteDecisions:
    """A service someone else runs — one per cloud node."""

    def __init__(
        self, url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._connection = Connection(base_url=url.rstrip("/"), token=token, transport=transport)

    def connection(self) -> Connection:
        return self._connection

    def lost(self) -> None:
        return None


class LocalDecisions:
    """The service as this backend's own child process, started on first use."""

    #: How long the service may take to start listening (not to load the model).
    START_S = 30.0
    #: An unused service exits after this long and gives its memory back.
    IDLE_S = 900

    def __init__(self) -> None:
        self._secret = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._home: Path | None = None
        self._url = ""

    def connection(self) -> Connection:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._url = self._start()
            return Connection(base_url=self._url, token=self._secret)

    def lost(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
            self._process = None

    def _start(self) -> str:
        if self._home is not None:
            # The last service has gone; its port file and log go with it.
            shutil.rmtree(self._home, ignore_errors=True)
        home = self._home = Path(tempfile.mkdtemp(prefix="igni-decisions-"))
        ready = home / "port"
        log = (home / "service.log").open("ab")
        env = dict(os.environ, **{SECRET_ENV: self._secret})
        command = [
            sys.executable,
            "-m",
            "ember_code.decisions_server",
            "--port",
            "0",
            "--ready-file",
            str(ready),
            "--idle-seconds",
            str(self.IDLE_S),
            "--watch-stdin",
        ]
        try:
            process = subprocess.Popen(  # noqa: S603 — our own module, by absolute interpreter
                command, stdin=subprocess.PIPE, stdout=log, stderr=log, env=env
            )
        except OSError as exc:
            raise DecisionsUnavailable(f"igni's decisions service would not start ({exc})") from exc
        finally:
            log.close()
        deadline = time.monotonic() + self.START_S
        while not ready.exists():
            if process.poll() is not None:
                raise DecisionsUnavailable(
                    "igni's decisions service exited as it started; see "
                    f"{home / 'service.log'} (is ignite-ember[decisions] installed?)"
                )
            if time.monotonic() > deadline:
                process.kill()
                raise DecisionsUnavailable("igni's decisions service did not start in time")
            time.sleep(0.05)
        self._process = process
        return f"http://127.0.0.1:{int(ready.read_text())}"


class DecisionsEndpoints:
    """Which endpoint this process uses."""

    @staticmethod
    def configured() -> DecisionsEndpoint:
        url = os.environ.get(URL_ENV, "").strip()
        if url:
            return RemoteDecisions(url, os.environ.get(TOKEN_ENV, ""))
        return LocalDecisions()


class Laya:
    """The backend's way to ask Laya: one per process, shared by the guard and extensions."""

    #: A first call may wait for the service to load a checkpoint.
    TIMEOUT = httpx.Timeout(300.0, connect=10.0)

    _shared: Laya | None = None
    _shared_lock = threading.Lock()

    def __init__(self, endpoint: DecisionsEndpoint | None = None) -> None:
        self._endpoint = endpoint if endpoint is not None else DecisionsEndpoints.configured()

    @classmethod
    def shared(cls) -> Laya:
        with cls._shared_lock:
            if cls._shared is None:
                cls._shared = cls()
            return cls._shared

    async def predict(
        self,
        state: dict[str, str],
        questions: dict[str, Any],
        *,
        model: str,
        priority: Priority = Priority.APPROVAL,
    ) -> dict[str, Any]:
        """Laya's raw answer from one checkpoint — for a caller that measured its questions on it."""
        call = PredictCall(state=state, questions=questions, model=model, priority=priority)
        answer = await self._ask("/v1/predict", call, PredictAnswer)
        return answer.result

    async def decide(self, request: DecisionRequest) -> list[Decision]:
        """Every context's decision, from Laya's general checkpoints, behind the approvals."""
        answer = await self._ask("/v1/decide", DecideCall(request=request), DecideAnswer)
        reading = LayaReading(request.questions)
        return [reading.decision(result) for result in answer.results]

    async def _ask(self, path: str, call: BaseModel, shape: type[Answer]) -> Answer:
        body = call.model_dump_json()
        for attempt in (1, 2):
            connection = await self._connection()
            try:
                async with httpx.AsyncClient(
                    base_url=connection.base_url,
                    transport=connection.transport,
                    timeout=self.TIMEOUT,
                    headers={
                        "authorization": f"Bearer {connection.token}",
                        "content-type": "application/json",
                    },
                ) as client:
                    response = await client.post(path, content=body)
            except httpx.ConnectError as exc:
                # A local service that idled out between our check and the call.
                self._endpoint.lost()
                if attempt == 2:
                    raise DecisionsUnavailable(
                        f"igni's decisions service could not be reached ({exc})"
                    ) from exc
                continue
            except httpx.HTTPError as exc:
                raise DecisionsUnavailable(
                    f"igni's decisions service did not answer ({type(exc).__name__})"
                ) from exc
            return self._read(response, shape)
        raise AssertionError("unreachable")

    async def _connection(self) -> Connection:
        # Starting a local service blocks for up to a few seconds.
        return await asyncio.to_thread(self._endpoint.connection)

    @staticmethod
    def _read(response: httpx.Response, shape: type[Answer]) -> Answer:
        if response.status_code == 200:
            try:
                return shape.model_validate_json(response.content)
            except ValidationError as exc:
                raise DecisionsUnavailable("igni's decisions service answered nonsense") from exc
        try:
            failure = DecisionsFailure.model_validate_json(response.content)
            detail = failure.detail
        except ValidationError:
            detail = f"HTTP {response.status_code}"
        raise DecisionsUnavailable(detail)


__all__ = [
    "Connection",
    "DecisionsEndpoint",
    "DecisionsEndpoints",
    "Laya",
    "LocalDecisions",
    "Priority",
    "RemoteDecisions",
]
