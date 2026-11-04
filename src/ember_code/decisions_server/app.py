"""The decisions service's HTTP surface: predict, decide, health — behind one shared secret.

``POST /v1/predict`` (:class:`PredictCall` → :class:`PredictAnswer`)
is the Bypass guard's raw question set on one checkpoint;
``POST /v1/decide`` (:class:`DecideCall` → :class:`DecideAnswer`) is an
extension's ``host.decide``. A model that cannot load or run answers
``503`` with a :class:`DecisionsFailure`; a request without the secret
answers ``401``. ``GET /v1/health`` says whether the model is loaded.

The model runs off the event loop; the gate inside
:class:`~ember_code.decisions_server.model.LayaModel` orders callers.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ember_code.core.decisions.schemas import (
    DecideAnswer,
    DecideCall,
    DecisionRequest,
    DecisionsFailure,
    DecisionsUnavailable,
    PredictAnswer,
    PredictCall,
    Priority,
)


class DecisionModel(Protocol):
    @property
    def loaded(self) -> bool: ...

    def predict(
        self, state: dict[str, str], questions: dict[str, Any], model: str, priority: Priority
    ) -> dict[str, Any]: ...

    def decide(self, request: DecisionRequest) -> list[dict[str, Any]]: ...


class Activity:
    """When the service last answered, and how many calls are in flight — for idling out."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.last = time.monotonic()

    def idle_for(self) -> float:
        return 0.0 if self.in_flight else time.monotonic() - self.last


class DecisionsApp:
    """The Starlette app, over a model and a secret."""

    def __init__(self, model: DecisionModel, secret: str) -> None:
        if not secret:
            raise ValueError("the decisions service needs a secret")
        self._model = model
        self._expected = f"Bearer {secret}".encode()
        self.activity = Activity()
        self.asgi = Starlette(
            routes=[
                Route("/v1/predict", self._predict, methods=["POST"]),
                Route("/v1/decide", self._decide, methods=["POST"]),
                Route("/v1/health", self._health, methods=["GET"]),
            ]
        )

    async def _predict(self, request: Request) -> Response:
        return await self._answer(request, PredictCall, self._run_predict)

    async def _decide(self, request: Request) -> Response:
        return await self._answer(request, DecideCall, self._run_decide)

    async def _health(self, request: Request) -> Response:
        if not self._authorised(request):
            return self._refuse()
        return JSONResponse({"ok": True, "loaded": self._model.loaded})

    async def _run_predict(self, call: PredictCall) -> BaseModel:
        result = await asyncio.to_thread(
            self._model.predict, call.state, call.questions, call.model, call.priority
        )
        return PredictAnswer(result=result)

    async def _run_decide(self, call: DecideCall) -> BaseModel:
        results = await asyncio.to_thread(self._model.decide, call.request)
        return DecideAnswer(results=results)

    async def _answer(self, request: Request, shape: type[BaseModel], run: Any) -> Response:
        if not self._authorised(request):
            return self._refuse()
        try:
            call = shape.model_validate_json(await request.body())
        except ValidationError as exc:
            return JSONResponse({"detail": exc.errors(include_url=False)}, status_code=422)
        self.activity.in_flight += 1
        try:
            answer = await run(call)
        except DecisionsUnavailable as exc:
            failure = DecisionsFailure(error="unavailable", detail=str(exc))
            return JSONResponse(failure.model_dump(), status_code=503)
        finally:
            self.activity.in_flight -= 1
            self.activity.last = time.monotonic()
        return JSONResponse(answer.model_dump(mode="json"))

    def _authorised(self, request: Request) -> bool:
        given = request.headers.get("authorization", "").encode()
        return hmac.compare_digest(given, self._expected)

    @staticmethod
    def _refuse() -> Response:
        failure = DecisionsFailure(error="unauthorized", detail="the decisions secret is required")
        return JSONResponse(failure.model_dump(), status_code=401)


__all__ = ["Activity", "DecisionModel", "DecisionsApp"]
