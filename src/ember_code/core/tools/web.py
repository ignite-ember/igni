"""Web tools — thin Agno-toolkit adapter over :class:`HttpFetcher`.

The heavy lifting (config, GET, content-type branching, HTML→text
extraction) lives on :class:`HttpFetcher` in :mod:`http_fetcher`.
This module exists only to bridge :class:`HttpFetcher`'s typed
:class:`FetchResult` return to Agno's string-return tool contract.

TLS verification stays ON. Disabling it silently would strip
integrity checks from every ``fetch_url`` / ``fetch_json`` call,
opening the tool to MITM interception when the user is on a
corporate/hostile network. If an internal endpoint uses a private
CA, the caller should set ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``.
"""

from typing import Any

from agno.tools import Toolkit

from ember_code.core.tools.http_fetcher import BroadcastFn, HttpFetcher
from ember_code.core.tools.web_schemas import HttpFetcherConfig


class WebTools(Toolkit):
    """Fetch and extract content from URLs."""

    def __init__(
        self,
        broadcast: BroadcastFn | None = None,
        config: HttpFetcherConfig | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name="ember_web", **kwargs)
        # Config is injectable rather than constructed inline: the
        # toolkit hard-wired ``HttpFetcherConfig()``, so the frozen
        # model's whole point — tests and callers passing a bespoke
        # config instead of monkey-patching globals — was unreachable
        # from the only place that builds a fetcher in production.
        self._fetcher = HttpFetcher(config or HttpFetcherConfig(), broadcast=broadcast)
        self.register(self.fetch_url)
        self.register(self.fetch_json)

    async def fetch_url(self, url: str, max_length: int | None = None) -> str:
        """Fetch URL content and extract text.

        Args:
            url: The URL to fetch.
            max_length: Maximum content length to return. Omit to use
                the fetcher's configured limit.

        Returns:
            Extracted text content from the URL.
        """
        # No literal default here: it duplicated
        # ``HttpFetcherConfig.max_html_chars``, and since the config
        # field was read by nothing the duplicate was the only value
        # that applied — a config handed to ``WebTools`` was ignored.
        result = await self._fetcher.fetch_text(url, max_length)
        return result.text_or_error()

    async def fetch_json(self, url: str) -> str:
        """Fetch and return JSON from a URL.

        Args:
            url: The URL to fetch JSON from.

        Returns:
            JSON string or error message.
        """
        result = await self._fetcher.fetch_json_text(url)
        return result.text_or_error()
