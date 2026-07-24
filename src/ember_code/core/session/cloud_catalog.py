"""Cloud model-catalog refresh.

Extracted from :meth:`Session.refresh_cloud_models` — the
best-effort fetch of the Ember Cloud key pool's catalogue and
merge into ``settings.models.registry``.

Silent-failure semantics preserved: no token → 0, transport
error → 0, no models → 0. Safe to call multiple times —
same-name entries are skipped so a user-edited registry
survives, and re-fetches are idempotent.
"""

from __future__ import annotations

import logging

from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.config.cloud_models import (
    CloudModelCatalogClient,
    FetchReason,
    MergeResult,
)
from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


class CloudModelCatalog:
    """Best-effort refresher of the local model registry from the
    cloud key pool.

    Constructor takes :class:`Settings`; :meth:`refresh` returns
    the number of newly-added models (``0`` on any silent-failure
    path). The single-method design matches the sole call site —
    :meth:`Session.__init__` — which just wants the side effect
    on ``settings.models.registry``.

    Session-policy concerns (credentials lookup, default-model
    auto-pick) stay here — the underlying
    :class:`CloudModelCatalogClient` deliberately doesn't know
    about the credentials file or the ``settings.models.default``
    field.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def refresh(self) -> int:
        """Fetch and merge; return the added-count.

        Silently no-ops when:

        * the user isn't logged in (no cloud token),
        * ``api_url`` is unreachable / times out / non-200,
        * the server returns a payload with unexpected shape,
        * any other transport error.

        Uses the reified :class:`FetchReason` on the fetch result to
        log the specific cause at debug — previously all failure
        modes collapsed into the same silent skip.
        """
        settings = self._settings
        token = CloudCredentials(settings.auth.credentials_file).access_token
        client = CloudModelCatalogClient(settings.api_url, token)
        fetch_result = client.fetch()
        if not fetch_result.ok:
            logger.debug(
                "cloud_models: refresh skipped (reason=%s, detail=%s)",
                fetch_result.reason.value,
                fetch_result.detail,
            )
            merge_result = MergeResult()
        else:
            merge_result = client.merge_into(settings.models.registry, entries=fetch_result.entries)
        added = merge_result.added
        if added:
            logger.info(
                "Merged %d cloud model(s) into the local registry (%d skipped, user-pinned)",
                added,
                merge_result.skipped_existing,
            )
        elif fetch_result.ok and fetch_result.reason == FetchReason.OK:
            logger.debug(
                "cloud_models: %d cloud entries already present, none added",
                merge_result.skipped_existing,
            )
        # Auto-pick the first entry as the default if nothing else
        # has set it. Lets a brand-new install reach a usable state
        # right after login without a hardcoded fallback name —
        # whatever the server returns first is the choice.
        if not settings.models.default and settings.models.registry:
            settings.models.default = next(iter(settings.models.registry))
            logger.info(
                "Auto-selected default model from cloud discovery: %s",
                settings.models.default,
            )
        return added
