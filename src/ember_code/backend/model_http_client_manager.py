"""Owns the model's httpx client lifecycle.

Extracted out of the old ``server_run.close_model_http_client``
free function + module-level ``_HTTP_CLIENT_LIMITS`` constant.
Every attribute is instance state; the class can be substituted
in tests without monkey-patching a module-level symbol.

Why a class:

* The old free function had implicit shared state (the
  ``_HTTP_CLIENT_LIMITS`` module constant) and no way to inject
  a per-team custom limits config. A per-run instance means a
  future ``settings.http_client_limits`` can be honoured without
  touching call sites.
* The ``except Exception: log.debug`` pattern is replaced by a
  typed :class:`HttpClientCloseResult` return so callers /
  tests can pin the failure mode rather than infer it from a
  log line.

The one immutable module-level value (``DEFAULT_LIMITS``) is a
Pydantic-equivalent constant — no mutation, cheap to share.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from agno.team import Team

from ember_code.backend.schemas_run import HttpClientCloseResult

logger = logging.getLogger(__name__)


# Fresh-client params post-close. Modest keepalive footprint so a
# run's post-tail teardown doesn't hold a big connection pool open
# across sessions.
DEFAULT_LIMITS = httpx.Limits(
    max_connections=10,
    max_keepalive_connections=5,
    keepalive_expiry=30,
)


class ModelHttpClientManager:
    """Closes the model's httpx client after a run and installs a
    fresh one so the next run stays usable.

    When an Agno run finishes or is cancelled mid-stream, the
    underlying httpx connection to the API can stay open
    indefinitely. Explicitly closing tears down the TCP connection
    so the server can release concurrency slots. A fresh
    ``httpx.AsyncClient`` is assigned so subsequent runs don't hit
    a "client is closed" error.
    """

    def __init__(self, *, limits: httpx.Limits | None = None):
        self._limits = limits or DEFAULT_LIMITS

    async def close_and_replace(self, team: Team) -> HttpClientCloseResult:
        """Close ``team.model.http_client`` (if present + async) and
        install a fresh one.

        Best-effort — a close failure is not fatal for the caller.
        The fresh client is installed even on close failure so the
        next run doesn't inherit a dead client. Return value tells
        the caller (and tests) whether the close leg succeeded.
        """
        model = self._extract_model(team)
        client = self._extract_client(model)
        result = HttpClientCloseResult(ok=True)
        if isinstance(client, httpx.AsyncClient):
            try:
                await asyncio.wait_for(client.aclose(), timeout=3)
            except Exception as exc:
                logger.debug("Failed to close model HTTP client: %s", exc)
                result = HttpClientCloseResult(ok=False, reason=str(exc))
        # Always ensure a fresh client, even if close failed. The
        # old client's connections will eventually time out on their
        # own.
        if model is not None:
            model.http_client = httpx.AsyncClient(limits=self._limits)
        return result

    @staticmethod
    def _extract_model(team: Team) -> Any:
        """Pluck ``team.model`` or ``None`` — ``getattr`` because Agno
        team instances have optional model wiring in some test
        fixtures."""
        return getattr(team, "model", None)

    @staticmethod
    def _extract_client(model: Any) -> Any:
        """Pluck the model's ``http_client`` attribute or ``None`` —
        older Agno versions may not populate it until first use."""
        return getattr(model, "http_client", None) if model else None
