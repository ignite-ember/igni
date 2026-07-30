"""Pydantic wire/data models for the web-tool subsystem.

Extracted from :mod:`web` per the sibling schemas convention
(mirrors :mod:`custom_loader_schemas`, :mod:`shell_orphan_schemas`,
and :mod:`process_store_schemas` in this exact directory). Every
config / result model the web-fetching stack hands across a module
boundary lives here so Rule 1 stays discoverable at one path.

Consumers:

* :class:`HttpFetcherConfig` — frozen configuration for
  :class:`~ember_code.core.tools.http_fetcher.HttpFetcher`. Replaces
  the previous module-level mutable ``_HTTPX_KWARGS`` dict plus the
  loose ``_USER_AGENT`` / ``_MAX_JSON_CHARS`` constants in
  :mod:`web`. Exposes :meth:`httpx_kwargs` so the raw dict shape
  needed by :class:`httpx.AsyncClient` is *derived* from the model
  rather than stored alongside it.
* :class:`FetchResult` — typed carrier for the outcome of an HTTP
  fetch. The single place the ``"Error fetching {url}: {e}"`` error
  string format lives, killing the duplication between the previous
  ``_get`` helper and :meth:`WebTools.fetch_url`. Callers at the
  Agno-toolkit boundary invoke :meth:`text_or_error` to stringify
  for the tool-call return contract.
"""

from pydantic import BaseModel, ConfigDict


class HttpFetcherConfig(BaseModel):
    """Frozen configuration for :class:`HttpFetcher`.

    Every knob previously lived as a module-level constant or a raw
    mutable dict in :mod:`web`. Consolidating on a frozen Pydantic
    model means:

    * Rule 1 — a typed schema instead of a loose ``dict``.
    * AP1 — no module-level mutable state; the config is owned by
      the fetcher instance that holds it on ``self``.
    * Tests can inject a bespoke config into
      :class:`HttpFetcher.__init__` instead of monkey-patching
      module globals.

    ``max_html_chars`` is the fallback truncation limit used by
    :meth:`HttpFetcher.fetch_text` when the Agno caller does not
    override ``max_length`` on :meth:`WebTools.fetch_url`.
    ``max_json_chars`` is the JSON-specific limit used by
    :meth:`HttpFetcher.fetch_json_text` (matches the previous
    ``_MAX_JSON_CHARS`` value verbatim).
    """

    model_config = ConfigDict(frozen=True)

    timeout: float = 30
    follow_redirects: bool = True
    user_agent: str = "EmberCode/0.1.0"
    max_html_chars: int = 10_000
    max_json_chars: int = 20_000

    def httpx_kwargs(self) -> dict:
        """Return the kwargs dict expected by :class:`httpx.AsyncClient`.

        Derived — not stored — so the loose ``_HTTPX_KWARGS`` dict
        no longer needs to sit at module scope. Callers double-star
        the returned dict at the httpx boundary.
        """
        return {"timeout": self.timeout, "follow_redirects": self.follow_redirects}


class FetchResult(BaseModel):
    """Typed carrier for the outcome of an HTTP fetch.

    Replaces the previous "sniff the returned string for the token
    ``Error``" contract with an explicit ``ok`` flag. The tool
    boundary (:class:`WebTools`) is where a :class:`FetchResult` is
    finally collapsed to a ``str`` — Agno's registered-function
    return contract requires a string — via :meth:`text_or_error`.

    The two classmethods :meth:`success` and :meth:`failure` are
    the *only* constructors used elsewhere in the subsystem, which
    guarantees the error-message format lives at exactly one call
    site.
    """

    ok: bool
    text: str = ""
    error: str = ""

    @classmethod
    def success(cls, text: str) -> "FetchResult":
        """Build a successful result carrying the response body."""
        return cls(ok=True, text=text)

    @classmethod
    def failure(cls, url: str, exc: Exception) -> "FetchResult":
        """Build a failed result with a pre-formatted error string.

        Matches the previous ``"Error fetching {url}: {e}"`` format
        used by ``_get`` verbatim so the wire contract with the
        agent stays byte-identical.
        """
        return cls(ok=False, error=f"Error fetching {url}: {exc}")

    def text_or_error(self) -> str:
        """Collapse to the Agno-toolkit string return.

        Returns :attr:`text` when :attr:`ok`, otherwise the
        pre-formatted :attr:`error` string.
        """
        return self.text if self.ok else self.error
