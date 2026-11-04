"""Where an extension lives on the web.

An extension introduced through igni server belongs to a service, and
the service's URL — the distribution record's ``service_url`` — is the
service's ``client_uri``, which RFC 7591 defines as the client's home
page. That is where the person goes to see the extension's own site:
igni shows it as "Open on the web" beside the extension, and the host
object model reports it as ``web_url``.

A development install has no distribution record; its ``serviceUrl``
setting is the same thing when it is set, and nothing when it is not.
"""

from __future__ import annotations

from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.store import ExtensionStore

#: The setting a development install names its service with — the one
#: :mod:`ember_code.extensions.installer` fetches its tokens for.
SERVICE_URL_SETTING = "serviceUrl"


class ExtensionWebHome:
    """Answers "where is this extension on the web" from the store."""

    def __init__(self, store: ExtensionStore) -> None:
        self._store = store

    def url_for(self, manifest: ExtensionManifest) -> str | None:
        record = self._store.distribution_of(manifest.id)
        if record is not None and record.service_url:
            return self._web(record.service_url)
        value = self._store.settings(manifest).get(SERVICE_URL_SETTING)
        return self._web(str(value)) if value else None

    @staticmethod
    def _web(url: str) -> str | None:
        """Only an http(s) URL is a home page; anything else is not shown."""
        text = url.strip()
        if text.startswith(("https://", "http://")):
            return text
        return None


__all__ = ["SERVICE_URL_SETTING", "ExtensionWebHome"]
