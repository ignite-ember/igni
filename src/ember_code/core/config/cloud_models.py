"""Discover models available in the Ember Cloud key pool.

The operator adds AI keys on the portal (per-org overrides or the
global pool); the server exposes the catalogue at
``GET /v1/chat/models`` as ``{"models": [{"id": "..."}, ...]}`` —
just the model identifiers. The server intentionally does **not**
return upstream provider URLs: the CLI talks to ember-server's
``/chat/completions`` proxy, which routes to whichever
``(key, base_url)`` pair the pool picks. Leaking the upstream URL
to the client would just expose routing internals.

All cloud-discovered entries are wired to route through
``{api_url}/v1`` on the local side. Older server deploys that
still send a ``base_url`` field are tolerated — the client
deliberately ignores it (see :class:`CloudModelEntry.model_config`,
``extra='allow'``).

User-defined entries always win — never overwrite an existing key in
``settings.models.registry``. Same-name entries from cloud become
no-ops, which lets users pin a custom config (different timeout,
provider override) without it getting clobbered on the next startup.

Failure modes are all soft — reified as :class:`FetchReason` on the
returned :class:`FetchResult` so callers can log the specific cause
(no token vs. HTTP error vs. decode error vs. bad shape) rather
than collapsing every degradation into the same info-line silence.
"""

from __future__ import annotations

import enum
import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.config.model_entry import ModelRegistryEntry

logger = logging.getLogger(__name__)


# ── Wire schemas ─────────────────────────────────────────────────────


class CloudModelEntry(BaseModel):
    """One model entry from the server's ``/v1/chat/models`` response.

    Only ``id`` is required. ``extra='allow'`` tolerates older
    server deploys that still emit a legacy ``base_url`` field —
    the client deliberately ignores it (routing always goes through
    ember-server's chat proxy, never upstream directly).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Wire key is ``id``; expose as ``model_id`` in Python so
    # downstream construction of :class:`ModelRegistryEntry` reads
    # naturally as ``from_cloud_discovery(model_id=entry.model_id, ...)``.
    model_id: str = Field(alias="id")


class CloudCatalogResponse(BaseModel):
    """Full ``/v1/chat/models`` response envelope."""

    model_config = ConfigDict(extra="allow")

    models: list[CloudModelEntry] = Field(default_factory=list)


# ── Result types (reified soft-fail modes) ───────────────────────────


class FetchReason(str, enum.Enum):
    """Why a fetch produced no entries — or ``OK`` when it succeeded."""

    OK = "ok"
    NO_TOKEN = "no_token"
    HTTP_ERROR = "http_error"
    DECODE_ERROR = "decode_error"
    BAD_SHAPE = "bad_shape"


class FetchResult(BaseModel):
    """Outcome of :meth:`CloudModelCatalogClient.fetch`.

    ``ok`` is ``True`` only when the server returned a well-shaped
    catalogue. Every other outcome is a soft-fail: the CLI must
    keep booting on whatever's already in local config.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool
    reason: FetchReason
    entries: list[ModelRegistryEntry] = Field(default_factory=list)
    # Free-form context for logging (HTTP status code, exception
    # class name, etc.) — never load-bearing for control flow.
    detail: str | None = None


class MergeResult(BaseModel):
    """Outcome of :meth:`CloudModelCatalogClient.merge_into`.

    Split ``added`` vs ``skipped_existing`` so callers can log both
    numbers — the previous free-function returned a single ``int``
    which collapsed "no new models" and "everything was already
    user-pinned" into the same signal.
    """

    added: int = 0
    skipped_existing: int = 0


# ── Client ───────────────────────────────────────────────────────────


class CloudModelCatalogClient:
    """Fetch and merge the Ember Cloud key-pool catalogue.

    Owns the two implicit subject arguments — ``api_url`` and the
    cloud token — as instance state so the fetch and merge steps
    share one source of truth for the proxy routing URL. The old
    module-level free functions (``fetch_cloud_models`` /
    ``merge_into_registry``) are collapsed into this class; there
    are no thin wrappers left behind.

    Synchronous: :meth:`Session.__init__` is sync and runs at every
    CLI invocation, so we can't add an asyncio dependency here. The
    hot path is cached upstream so the call is cheap.
    """

    # Tight timeout — this runs synchronously on session start, so
    # blocking the CLI for 15s on a flaky network would be visibly
    # painful. Class attribute (not module-private const) so tests
    # can override without patching a private module symbol.
    FETCH_TIMEOUT_SECONDS: float = 3.0

    def __init__(self, api_url: str, cloud_token: str | None) -> None:
        self._api_url = api_url
        self._cloud_token = cloud_token

    # ── Public API ────────────────────────────────────────────────

    def fetch(self) -> FetchResult:
        """Fetch the cloud catalogue.

        Returns a :class:`FetchResult` whose ``reason`` distinguishes
        the four soft-fail modes:

        * :attr:`FetchReason.NO_TOKEN` — user isn't logged in.
        * :attr:`FetchReason.HTTP_ERROR` — non-200 status (401/503/…).
        * :attr:`FetchReason.DECODE_ERROR` — transport / JSON parse
          failed (network error, timeout, malformed body).
        * :attr:`FetchReason.BAD_SHAPE` — 200 with a body that doesn't
          match the expected schema.
        * :attr:`FetchReason.OK` — parsed successfully; entries may
          still be empty if the server has no models to advertise.
        """
        if not self._cloud_token:
            logger.debug("cloud_models: no token, skipping fetch")
            return FetchResult(ok=False, reason=FetchReason.NO_TOKEN)

        url = f"{self._api_url.rstrip('/')}/v1/chat/models"
        try:
            with httpx.Client(timeout=self.FETCH_TIMEOUT_SECONDS) as client:
                resp = client.get(
                    url,
                    headers={"Authorization": f"Bearer {self._cloud_token}"},
                )
        except Exception as exc:
            logger.debug("cloud_models: fetch failed (%s) — skipping merge", exc)
            return FetchResult(
                ok=False,
                reason=FetchReason.DECODE_ERROR,
                detail=exc.__class__.__name__,
            )

        if resp.status_code != 200:
            logger.debug("cloud_models: %s returned %s — skipping merge", url, resp.status_code)
            return FetchResult(
                ok=False,
                reason=FetchReason.HTTP_ERROR,
                detail=f"HTTP {resp.status_code}",
            )

        try:
            catalog = self._parse_response(resp)
        except Exception as exc:
            logger.debug("cloud_models: unexpected payload shape (%s) — skipping merge", exc)
            return FetchResult(
                ok=False,
                reason=FetchReason.BAD_SHAPE,
                detail=exc.__class__.__name__,
            )

        entries = [
            ModelRegistryEntry.from_cloud_discovery(
                model_id=cloud_entry.model_id,
                proxy_url=self._proxy_url,
            )
            for cloud_entry in catalog.models
            if cloud_entry.model_id
        ]
        return FetchResult(ok=True, reason=FetchReason.OK, entries=entries)

    def merge_into(
        self,
        registry: dict[str, ModelRegistryEntry | dict[str, Any]],
        entries: list[ModelRegistryEntry] | None = None,
    ) -> MergeResult:
        """Merge cloud-discovered entries into ``registry`` in place.

        When ``entries`` is ``None`` the client fetches first — this
        is the one-shot path for callers that only need the side
        effect and don't care about the specific fetch outcome.
        When ``entries`` is provided (e.g. by a caller that already
        called :meth:`fetch` to inspect :class:`FetchReason` for
        logging), the merge runs directly on that list without
        re-fetching.

        Every entry is a :class:`ModelRegistryEntry` already wired to
        route through ``{api_url}/v1`` (the Ember Cloud chat proxy)
        via :meth:`ModelRegistryEntry.from_cloud_discovery`. Same-name
        entries are skipped — user/project config always wins.
        """
        if entries is None:
            fetch_result = self.fetch()
            if not fetch_result.ok:
                return MergeResult()
            entries = fetch_result.entries
        return self._apply_entries(registry, entries)

    # ── Internals ─────────────────────────────────────────────────

    @property
    def _proxy_url(self) -> str:
        """The ember-server chat proxy URL. Every cloud-discovered
        entry is wired here — never at an upstream provider URL."""
        return f"{self._api_url.rstrip('/')}/v1"

    def _parse_response(self, resp: httpx.Response) -> CloudCatalogResponse:
        """Parse the JSON body through :class:`CloudCatalogResponse`.

        Uses ``model_validate_json`` so we validate once at the
        boundary — no defensive re-checks downstream. Any shape
        mismatch (payload not a dict, ``models`` not a list, entries
        without ``id``) surfaces as a ``ValidationError`` caught by
        the fetch caller.
        """
        return CloudCatalogResponse.model_validate_json(resp.content)

    @staticmethod
    def _apply_entries(
        registry: dict[str, ModelRegistryEntry | dict[str, Any]],
        entries: list[ModelRegistryEntry],
    ) -> MergeResult:
        added = 0
        skipped = 0
        for entry in entries:
            name = entry.model_id
            if not name:
                continue
            if name in registry:
                skipped += 1
                continue
            registry[name] = entry
            added += 1
        return MergeResult(added=added, skipped_existing=skipped)
