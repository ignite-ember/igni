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

**Air-gapped** (``IGNI_AIR_GAPPED=1``, which a backend with
``air_gapped: true`` exports to the service it starts): the model is
loaded only from the local Hugging Face cache, with the hub switched
offline for the life of the process, so no checkpoint is ever fetched
— not at load, not when a checkpoint is first asked for. Pre-seed the
cache (``HF_HOME`` or ``HF_HUB_CACHE``, a local path) from a machine
that can reach the hub: :data:`PRESEED_HINT`.

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

from ember_code.core.air_gap import air_gapped_by_environment
from ember_code.core.decisions.schemas import (
    DecisionRequest,
    DecisionsUnavailable,
    Priority,
)

logger = logging.getLogger(__name__)

#: The hub repository bundling every Laya checkpoint (``laya.router.BUNDLE_REPO``).
BUNDLE_REPO = "convaiinnovations/laya"

#: How to give an air-gapped machine the model, said whenever it is missing.
PRESEED_HINT = (
    "On a machine that can reach huggingface.co, run "
    "`HF_HOME=/tmp/laya python -m ember_code.prefetch_models` (with ignite-ember[decisions] "
    "installed), copy /tmp/laya/hub/models--convaiinnovations--laya into this machine's "
    "Hugging Face cache ({cache}), or point HF_HOME or HF_HUB_CACHE at a local copy, then "
    "start the decisions service again."
)

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
                except DecisionsUnavailable:
                    raise
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

        if air_gapped_by_environment():
            return self._build_offline(build)
        if os.environ.get("HF_HUB_OFFLINE") is None and HubCache.has(BUNDLE_REPO):
            os.environ["HF_HUB_OFFLINE"] = "1"
            try:
                return self._quietly(build)
            except Exception as exc:  # noqa: BLE001 — retried online below
                logger.debug("laya offline load failed (%s); fetching", type(exc).__name__)
            finally:
                del os.environ["HF_HUB_OFFLINE"]
        return self._quietly(build)

    def _build_offline(self, build: Callable[[], Any]) -> Any:
        """Air-gapped: the cache or nothing, and the hub offline from here on.

        Offline for the rest of the process rather than for the load alone:
        the router loads each checkpoint the first time it is asked for it,
        and that later load must not reach for the network either. Set on
        the environment (read by transformers) and on ``huggingface_hub``'s
        constant (read at import, so the variable alone is too late).
        """
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        hub.HF_HUB_OFFLINE = True
        cache = hub.HF_HUB_CACHE
        if not HubCache.has(BUNDLE_REPO):
            raise DecisionsUnavailable(
                f"igni is air-gapped, so its decision model ({BUNDLE_REPO}) is loaded only "
                f"from the local cache, and it is not in {cache}. "
                + PRESEED_HINT.format(cache=cache)
            )
        try:
            return self._quietly(build)
        except Exception as exc:  # noqa: BLE001 — named, never retried online
            raise DecisionsUnavailable(
                f"igni is air-gapped and its decision model in {cache} is incomplete "
                f"({type(exc).__name__}). " + PRESEED_HINT.format(cache=cache)
            ) from exc

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


__all__ = ["BUNDLE_REPO", "PRESEED_HINT", "HubCache", "LayaModel", "PriorityGate"]
