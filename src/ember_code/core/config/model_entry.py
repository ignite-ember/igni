"""Typed registry entry for the model registry.

Owns API-key resolution (see :meth:`ModelRegistryEntry.resolve_api_key`)
and Ember Cloud-gateway detection (see
:meth:`ModelRegistryEntry.matches_cloud_gateway`). Both live as Pydantic
methods on the model — the entry IS the subject, so the behavior rides
with the data instead of in a free helper taking the entry-as-dict.

``resolve_api_key`` takes the cloud token by injection rather than
instantiating :class:`CloudCredentials` itself — the caller (the
provider builder) already has the resolved token from the registry
initialization step.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# Sentinel value on ``ModelRegistryEntry.api_key`` that means "resolve
# to the stored Ember Cloud access token at call time". Kept as a
# named constant so the same magic string doesn't get spelled twice
# (previously duplicated between ``resolve_api_key`` and the
# cloud-discovery construction path in ``cloud_models.py``).
CLOUD_TOKEN_SENTINEL = "cloud_token"

# Production Ember Cloud gateway hostname — used by the migration
# tier to identify rows that WE own and should replace with the
# latest shipping defaults. Kept as a module-level constant next to
# ``CLOUD_TOKEN_SENTINEL`` for consistency with the file's other
# domain constant.
CLOUD_GATEWAY_HOST = "api.ignite-ember.sh"


class ModelRegistryEntry(BaseModel):
    """One entry in ``settings.models.registry``.

    Everything the registry needs to know about a specific model
    lives here. Providers other than ``openai_like`` (currently only
    ``gemini``) use a subset of these fields — the extras are
    ignored during kwarg construction on the provider side.
    """

    # Allow arbitrary extra keys so we tolerate cloud-server payload
    # additions (``source: 'cloud'`` from ``cloud_models`` etc.)
    # without breaking config load.
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model_id: str
    provider: str = "openai_like"
    url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    api_key_cmd: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int = 60
    vision: bool = False
    context_window: int | None = None

    # ── Context-window hint ────────────────────────────────────────

    def context_window_hint(self) -> int | None:
        """Return the explicit ``context_window`` as an ``int`` or
        ``None`` when the entry has no override.

        Trivial normalization method — gives
        :class:`ContextWindowResolver` a semantically named accessor
        instead of re-implementing the ``int(...) if ... else None``
        dance in three places. Behaviour lives on the entry (Rule 6)
        rather than in a free helper.
        """
        return int(self.context_window) if self.context_window else None

    # ── API-key resolution ─────────────────────────────────────────

    def resolve_api_key(self, cloud_token: str | None = None) -> str | None:
        """Resolve the API key using the four-tier fallback:

        1. Literal ``api_key`` on the entry — sentinel ``"cloud_token"``
           is replaced with the injected cloud token.
        2. Environment variable named by ``api_key_env``.
        3. Shell command specified by ``api_key_cmd``.
        4. Ember Cloud token fallback for URLs pointing at the cloud
           gateway (previously a duplicate check inside
           ``ModelRegistry._resolve_api_key``).

        The cloud token is passed IN rather than looked up here so
        the entry doesn't depend on :class:`CloudCredentials` — a
        cleaner test surface and one credential-file read per
        registry, not per entry.
        """
        # Literal value on the entry — honor the cloud_token sentinel.
        if self.api_key is not None:
            if self.api_key == CLOUD_TOKEN_SENTINEL:
                return cloud_token
            return self.api_key
        env_key = self._api_key_from_env()
        if env_key:
            return env_key
        cmd_key = self._api_key_from_cmd()
        if cmd_key:
            return cmd_key
        if self.matches_cloud_gateway():
            return cloud_token
        return None

    def _api_key_from_env(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env) or None

    def _api_key_from_cmd(self) -> str | None:
        if not self.api_key_cmd:
            return None
        # Bounded timeout so a hung ``api_key_cmd`` (e.g. a keychain
        # prompt that never returns) can't wedge session bring-up.
        try:
            result = subprocess.run(
                shlex.split(self.api_key_cmd),
                capture_output=True,
                text=True,
                timeout=15.0,
            )
        except subprocess.TimeoutExpired:
            return None
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None

    # ── Cloud-gateway detection ────────────────────────────────────

    def matches_cloud_gateway(self) -> bool:
        """True when this entry's URL points at the Ember Cloud
        gateway. Combined with ``api_key == 'cloud_token'`` this is
        the routing signal that the entry expects the stored login
        credentials rather than a per-entry key.

        Substring check on ``ignite-ember.sh`` is intentional — it
        includes dev/staging subdomains (``dev-api.ignite-ember.sh``)
        so cloud-token credential routing works there too. For the
        production-only migration decision (where hitting a staging
        row would be wrong) use the hostname-exact staticmethod
        :meth:`is_cloud_gateway_url` instead.
        """
        if not self.url:
            return False
        return "ignite-ember.sh" in self.url

    @staticmethod
    def is_cloud_gateway_url(url: str) -> bool:
        """True only when ``url`` points at the production cloud host.

        Hostname-exact (not substring) so dev/staging overrides like
        ``dev-api.ignite-ember.sh`` are treated as user-managed and
        survive the migration step. This is the migration-decision
        predicate — pair it with :meth:`matches_cloud_gateway` for
        the substring predicate used by credential routing.
        """
        try:
            return urlparse(url).hostname == CLOUD_GATEWAY_HOST
        except (ValueError, TypeError):
            # ``urlparse`` raises ValueError for malformed inputs and
            # TypeError when handed a non-string — anything else is a
            # programming bug and should propagate.
            return False

    def needs_cloud_token(self) -> bool:
        """True when the entry expects the stored Ember Cloud token
        — either via the ``cloud_token`` sentinel on ``api_key`` OR
        via the URL matching the cloud gateway with no explicit
        credentials of its own."""
        if self.api_key == CLOUD_TOKEN_SENTINEL:
            return True
        no_explicit_key = self.api_key is None and not self.api_key_env and not self.api_key_cmd
        return no_explicit_key and self.matches_cloud_gateway()

    # ── Cloud-discovery construction ───────────────────────────────

    @classmethod
    def from_cloud_discovery(
        cls,
        *,
        model_id: str,
        proxy_url: str,
    ) -> ModelRegistryEntry:
        """Build the canonical entry shape for a cloud-discovered model.

        All cloud entries route through ember-server's chat proxy
        (``{api_url}/v1``) — never at the upstream provider — and
        rely on the :data:`CLOUD_TOKEN_SENTINEL` on ``api_key`` so
        :meth:`resolve_api_key` swaps in the stored Ember Cloud
        access token at call time.

        The ``source='cloud'`` tag rides in via ``model_config`` extras
        (this model has ``extra='allow'``) so future code — a
        "managed by cloud" badge in the picker, say — can
        distinguish cloud-discovered rows from user-defined ones
        without adding a schema field just for the marker.
        """
        return cls.model_validate(
            {
                "provider": "openai_like",
                "model_id": model_id,
                "url": proxy_url,
                "api_key": CLOUD_TOKEN_SENTINEL,
                "source": "cloud",
            }
        )
