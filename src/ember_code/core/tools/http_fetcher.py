"""HTTP fetching for the web-tool subsystem.

Houses :class:`HttpFetcher` — the single OOP owner of "GET a URL,
sniff its content type, extract the body, truncate, and return a
typed result". Extracted from :mod:`web` per the audit so that
:mod:`web` can be a thin Agno-toolkit adapter over a class with a
named subject.

The class explicitly answers three ``oop_offenders`` items on the
prior version of :mod:`web`:

* *utility-module-of-related-helpers* — the free ``_get`` +
  ``_extract_text_from_html`` pair becomes methods on
  :class:`HttpFetcher`.
* *module-level-mutable-state* — the ``_HTTPX_KWARGS`` dict,
  ``_USER_AGENT`` and ``_MAX_JSON_CHARS`` constants become instance
  state owned via :class:`HttpFetcherConfig`.
* *data-and-behavior-separated* — one class now owns the config,
  the GET path, the content-type branching, and the HTML→text
  extraction.

A sibling procedural helper module (see :mod:`search`) is *not* a
defense for keeping this file flat — the audit flagged the
free-helper cluster specifically, and this class exists to close
that finding.
"""

import re

import httpx

from ember_code.core.tools.web_schemas import FetchResult, HttpFetcherConfig


class HttpFetcher:
    """Configured HTTP fetcher returning typed :class:`FetchResult`.

    The class owns:

    * an :class:`HttpFetcherConfig` (constructor-injected — no
      module singletons; tests pass a bespoke config in);
    * a single private GET helper that constructs
      :class:`httpx.AsyncClient` from ``config.httpx_kwargs()`` —
      collapsing the two duplicated ``try`` / ``except`` blocks in
      the previous :mod:`web` module;
    * the four-regex HTML-to-text extraction as a
      :func:`staticmethod` so its subject is named at class scope.

    Exception handling narrows to :class:`httpx.HTTPError`. Other
    exceptions (``KeyError`` / ``AttributeError`` / ``TypeError``
    from a programming bug) intentionally propagate per Pattern 3;
    a bare ``except Exception`` would swallow them.
    """

    def __init__(self, config: HttpFetcherConfig | None = None) -> None:
        self._config = config or HttpFetcherConfig()

    @property
    def config(self) -> HttpFetcherConfig:
        """Expose the frozen config for callers that need to read it."""
        return self._config

    async def fetch_text(self, url: str, max_length: int) -> FetchResult:
        """GET ``url``, extract body text, truncate to ``max_length``.

        Sniffs the response's ``Content-Type`` header:

        * ``json`` — return the raw response text truncated.
        * ``html`` — pass through :meth:`html_to_text` before
          truncation. Truncation happens *after* extraction so the
          agent sees ``max_length`` characters of visible text, not
          of markup. This preserves behavior parity with the prior
          :meth:`WebTools.fetch_url`.
        * anything else — return the raw response text truncated.
        """
        try:
            response = await self._get(url, headers={"User-Agent": self._config.user_agent})
        except httpx.HTTPError as e:
            return FetchResult.failure(url, e)

        content_type = response.headers.get("content-type", "")
        text = response.text
        if "json" in content_type:
            return FetchResult.success(text[:max_length])
        if "html" in content_type:
            text = self.html_to_text(text)
        return FetchResult.success(text[:max_length])

    async def fetch_json_text(self, url: str) -> FetchResult:
        """GET ``url`` with an ``Accept: application/json`` header.

        Truncates to :attr:`HttpFetcherConfig.max_json_chars` — the
        JSON-specific limit is intentionally larger than the HTML
        default so structured payloads survive round-tripping to
        the agent.
        """
        try:
            response = await self._get(
                url,
                headers={
                    "User-Agent": self._config.user_agent,
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as e:
            return FetchResult.failure(url, e)
        return FetchResult.success(response.text[: self._config.max_json_chars])

    async def _get(self, url: str, headers: dict[str, str]) -> httpx.Response:
        """Shared low-level GET.

        Constructs :class:`httpx.AsyncClient` from the config's
        derived kwargs, issues a GET, raises on non-2xx, and
        returns the raw response object so callers can inspect
        headers for content-type branching. Only
        :class:`httpx.HTTPError` is expected here; callers handle
        it and convert to :class:`FetchResult.failure`.
        """
        async with httpx.AsyncClient(**self._config.httpx_kwargs()) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response

    @staticmethod
    def html_to_text(html: str) -> str:
        """Basic HTML-to-text extraction.

        Four regex passes:

        1. Strip ``<script>…</script>`` (DOTALL — multi-line
           scripts).
        2. Strip ``<style>…</style>`` (same).
        3. Replace remaining ``<…>`` tags with a single space.
        4. Collapse whitespace runs and ``.strip()``.

        HTML entities (``&amp;``, ``&nbsp;`` …) intentionally pass
        through literally — the caller downstream may want the
        source form.
        """
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
