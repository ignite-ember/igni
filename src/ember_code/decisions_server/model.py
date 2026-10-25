"""Laya, loaded once in the decisions service, behind a gate that lets approvals go first.

Laya (``convaiinnovations/laya``, Apache-2.0) answers typed questions —
choice, score, yes/no — about a short text in one forward pass. Two
kinds of caller share it:

* the Bypass guard, on the approval path, with the ``typed-decisions``
  checkpoint it was measured on (:class:`PredictCall`);
* extensions, through ``host.decide``, with Laya's general
  checkpoints: ``english`` for English text, ``multilingual`` for the
  rest — Laya's own routing, which never picks ``typed-decisions``.

One model answers at a time, approvals first, across every backend the
service serves: an extension's batch never stands between a person and
an approval. Checkpoints load on first use, with download chatter kept
off stdout. A model that will not load is
:class:`~ember_code.core.decisions.schemas.DecisionsUnavailable` —
never a guess.

This module is the only one that imports ``laya`` (and with it torch);
the backend never imports it.
"""

from __future__ import annotations

import contextlib
import heapq
import io
import itertools
import logging
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from huggingface_hub import constants as hub

from ember_code.core.decisions.schemas import (
    DecisionRequest,
    DecisionsUnavailable,
    Priority,
)

logger = logging.getLogger(__name__)

#: The hub repository bundling every Laya checkpoint (``laya.router.BUNDLE_REPO``).
BUNDLE_REPO = "convaiinnovations/laya"

#: Checkpoints that may be resident at once: the Bypass guard's, and the
#: two general ones extensions are routed between.
MAX_LOADED = 3


class PriorityGate:
    """One holder at a time; the lowest priority waiting goes next, in arrival order."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._busy = False
        self._waiting: list[tuple[int, int]] = []
        self._seq = itertools.count()

    @contextlib.contextmanager
    def hold(self, priority: Priority) -> Iterator[None]:
        with self._cond:
            ticket = (int(priority), next(self._seq))
            heapq.heappush(self._waiting, ticket)
            while self._busy or self._waiting[0] != ticket:
                self._cond.wait()
            heapq.heappop(self._waiting)
            self._busy = True
        try:
            yield
        finally:
            with self._cond:
                self._busy = False
                self._cond.notify_all()


class LayaModel:
    """The service's Laya router and the gate in front of it. Blocking; call it off the loop."""

    def __init__(self) -> None:
        self._router: Any = None
        self._lock = threading.Lock()
        self.gate = PriorityGate()

    @property
    def loaded(self) -> bool:
        return self._router is not None

    def predict(
        self, state: dict[str, str], questions: dict[str, Any], model: str, priority: Priority
    ) -> dict[str, Any]:
        """Laya's raw answer from one checkpoint."""
        router = self.load()
        with self.gate.hold(priority):
            try:
                return dict(router.predict(state, questions, model=model))
            except Exception as exc:  # noqa: BLE001 — a checkpoint loads on first use
                raise self._unavailable(exc) from exc

    def decide(self, request: DecisionRequest) -> list[dict[str, Any]]:
        """Laya's raw answer per context, from its general checkpoints, behind the approvals."""
        router = self.load()
        questions = request.laya_questions()
        batch = [{"state": context, "questions": questions} for context in request.contexts]
        with self.gate.hold(Priority.EXTENSION):
            try:
                return [dict(result) for result in router.predict_batch(batch)]
            except ValueError as exc:
                # Laya names the question it cannot read; the request
                # passed igni's checks, so this is the model's limit.
                raise DecisionsUnavailable(f"Laya could not answer: {exc}") from exc
            except Exception as exc:  # noqa: BLE001 — a checkpoint loads on first use
                raise self._unavailable(exc) from exc

    @staticmethod
    def _unavailable(exc: Exception) -> DecisionsUnavailable:
        """The router loads each checkpoint the first time it is asked for it."""
        logger.warning("laya: could not run (%s: %s)", type(exc).__name__, exc)
        return DecisionsUnavailable(
            f"igni's decision model could not be loaded or run ({type(exc).__name__})"
        )

    def load(self) -> Any:
        if self._router is not None:
            return self._router
        with self._lock:
            if self._router is None:
                try:
                    self._router = self._build()
                except Exception as exc:  # noqa: BLE001 — any load failure is "unavailable"
                    logger.warning("laya: could not load (%s)", type(exc).__name__)
                    raise DecisionsUnavailable(
                        f"igni's decision model could not be loaded ({type(exc).__name__})"
                    ) from exc
        return self._router

    def _build(self) -> Any:
        """The router; tried offline first when the bundle is cached.

        A warm load then does not wait on a network probe. A cached
        bundle need not hold every checkpoint's subfolder, so a failed
        offline load falls back to fetching.
        """
        from laya import Router  # noqa: PLC0415 — torch-heavy, load on first use

        def build() -> Any:
            return Router(max_loaded=MAX_LOADED)

        if os.environ.get("HF_HUB_OFFLINE") is None and HubCache.has(BUNDLE_REPO):
            os.environ["HF_HUB_OFFLINE"] = "1"
            try:
                return self._quietly(build)
            except Exception as exc:  # noqa: BLE001 — retried online below
                logger.debug("laya offline load failed (%s); fetching", type(exc).__name__)
            finally:
                del os.environ["HF_HUB_OFFLINE"]
        return self._quietly(build)

    @staticmethod
    def _quietly(build: Callable[[], Any]) -> Any:
        devnull = os.open(os.devnull, os.O_WRONLY)
        saved = os.dup(1)
        bars = os.environ.get("HF_HUB_DISABLE_PROGRESS_BARS")
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        try:
            os.dup2(devnull, 1)
            with contextlib.redirect_stdout(io.StringIO()):
                return build()
        finally:
            os.dup2(saved, 1)
            os.close(saved)
            os.close(devnull)
            if bars is None:
                del os.environ["HF_HUB_DISABLE_PROGRESS_BARS"]
            else:
                os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = bars


class HubCache:
    """Whether a hub repository has been downloaded to this machine."""

    @staticmethod
    def has(repo: str) -> bool:
        snapshots = Path(hub.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--")) / "snapshots"
        return snapshots.is_dir() and any(snapshots.iterdir())


__all__ = ["BUNDLE_REPO", "HubCache", "LayaModel", "PriorityGate"]
