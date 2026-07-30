"""Context window resolution for model providers.

Extracted from :mod:`ember_code.core.config.models` so the model
registry has one job (mapping names → Agno model instances) and
the "how big is this model's context" concern lives on its own.

Resolution order:

1. Explicit ``context_window`` on the :class:`ModelRegistryEntry` —
   user overrides always win.
2. Dynamic fetch from the provider's OpenAI-compatible
   ``/models/{id}`` endpoint (async only). Cached per model_id.
3. Fallback to :data:`DEFAULT_CONTEXT_WINDOW` (128k) so the caller
   never sees ``None``.

Design notes:

* Public signatures accept ``ModelRegistryEntry | None`` only. Dict
  coercion belongs to :meth:`ModelRegistry._resolve_entry`, which is
  the single owner of "raw registry row → typed entry".
* API-key resolution is delegated to
  :meth:`ModelRegistryEntry.resolve_api_key` so this module never
  re-implements the ``api_key`` / ``api_key_env`` / cloud-token
  precedence rules.
* The HTTP fetch returns a typed :class:`_FetchOutcome` (module-
  private — only :class:`ContextWindowResolver` consumes it) that
  distinguishes network failures from decode failures from missing
  keys. Callers of the public API still see a plain ``int``; the
  outcome exists to make the categorised failure logging testable.
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx
from pydantic import BaseModel

from ember_code.core.config.model_entry import ModelRegistryEntry

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOW = 128_000


FetchReason = Literal[
    "ok",
    "http_error",
    "bad_status",
    "decode_error",
    "missing_key",
]


class _FetchOutcome(BaseModel):
    """Typed result of one ``/models/{id}`` fetch.

    Kept module-private (leading underscore) because only
    :class:`ContextWindowResolver` consumes it. If a future caller
    needs to react to specific failure categories, promote this
    to ``schemas/models.py``.
    """

    value: int | None = None
    reason: FetchReason

    @classmethod
    def ok(cls, value: int) -> _FetchOutcome:
        return cls(value=value, reason="ok")

    @classmethod
    def failure(cls, reason: FetchReason) -> _FetchOutcome:
        return cls(value=None, reason=reason)


class ContextWindowResolver:
    """Resolves the context window size for a model.

    Instance state is a single ``dict[str, int]`` cache of
    successful async fetches. Sync ``resolve`` never hits the
    network — it consults the cache + entry override only.

    The optional ``cloud_token`` constructor argument is passed
    through to :meth:`ModelRegistryEntry.resolve_api_key` on every
    fetch so the resolver reuses the caller's already-derived
    Ember Cloud token rather than re-reading credentials from disk
    or the environment.
    """

    def __init__(self, cloud_token: str | None = None) -> None:
        self._cache: dict[str, int] = {}
        self._cloud_token = cloud_token

    def resolve(
        self,
        model_id: str,
        entry: ModelRegistryEntry | None = None,
    ) -> int:
        """Return the context window size for *model_id* (synchronous)."""
        if entry is not None:
            hint = entry.context_window_hint()
            if hint is not None:
                return hint
        if model_id in self._cache:
            return self._cache[model_id]
        return DEFAULT_CONTEXT_WINDOW

    async def aresolve(
        self,
        model_id: str,
        entry: ModelRegistryEntry | None = None,
    ) -> int:
        """Return the context window size, with async API fallback."""
        if entry is not None:
            hint = entry.context_window_hint()
            if hint is not None:
                return hint
        if model_id in self._cache:
            return self._cache[model_id]

        if entry is not None and entry.url:
            outcome = await self._fetch_from_api(
                model_id=model_id,
                base_url=entry.url,
                api_key=entry.resolve_api_key(cloud_token=self._cloud_token) or "",
            )
            if outcome.value is not None:
                self._cache[model_id] = outcome.value
                return outcome.value

        return DEFAULT_CONTEXT_WINDOW

    async def _fetch_from_api(
        self,
        model_id: str,
        base_url: str,
        api_key: str = "",
    ) -> _FetchOutcome:
        """Fetch context window from an OpenAI-compatible ``/models/{id}``
        endpoint.

        Returns a :class:`_FetchOutcome` categorising the outcome —
        ``ok`` with a value, or one of the failure reasons so the
        caller (and tests) can distinguish network trouble from a
        204 with no context-window field from a decode error. The
        public API still normalises everything to ``int`` via
        :data:`DEFAULT_CONTEXT_WINDOW`; the categorised outcome
        exists so the debug log names what actually happened.
        """
        url = f"{base_url.rstrip('/')}/models/{model_id}"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.debug(
                    "Context-window fetch for %s returned status %d",
                    model_id,
                    resp.status_code,
                )
                return _FetchOutcome.failure("bad_status")
            data = resp.json()
            for key in ("context_window", "context_length", "max_model_len"):
                if key in data:
                    return _FetchOutcome.ok(int(data[key]))
            logger.debug(
                "Context-window fetch for %s missing known keys in payload",
                model_id,
            )
            return _FetchOutcome.failure("missing_key")
        except httpx.HTTPError as e:
            logger.debug(
                "Context-window fetch for %s failed (network): %s",
                model_id,
                e,
            )
            return _FetchOutcome.failure("http_error")
        except (ValueError, KeyError, TypeError) as e:
            # ``ValueError`` covers JSON decode errors (``resp.json()``
            # raises ``json.JSONDecodeError`` which subclasses
            # ``ValueError``) and ``int(non_numeric)``. ``KeyError`` and
            # ``TypeError`` cover payload shapes where the key is
            # present but not coercible.
            logger.debug(
                "Context-window fetch for %s failed (decode): %s",
                model_id,
                e,
            )
            return _FetchOutcome.failure("decode_error")
