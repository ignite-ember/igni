"""``HttpFetcherConfig.max_html_chars`` is documented as the fetch_text
truncation limit — so it has to be the thing that truncates.

The field existed, its docstring described it as "the fallback truncation
limit used by ``HttpFetcher.fetch_text`` when the Agno caller does not
override ``max_length``", and nothing read it. ``fetch_text`` took a
required ``max_length`` and sliced on that; the only default that ever
applied was a bare ``10000`` literal on ``WebTools.fetch_url``. Its
sibling ``max_json_chars`` *is* read, which is what makes the asymmetry
a bug rather than a style choice.

``WebTools`` also hard-wired ``HttpFetcherConfig()``, so even a caller
holding a bespoke config had no way to get it into the fetcher.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.tools.http_fetcher import HttpFetcher
from ember_code.core.tools.web import WebTools
from ember_code.core.tools.web_schemas import HttpFetcherConfig


def _mock_client(text: str, content_type: str = "text/plain"):
    response = MagicMock()
    response.text = text
    response.headers = {"content-type": content_type}
    response.raise_for_status = MagicMock()

    instance = AsyncMock()
    instance.get = AsyncMock(return_value=response)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    return instance


@pytest.fixture
def patched_client():
    with patch("ember_code.core.tools.http_fetcher.httpx.AsyncClient") as MockClient:
        MockClient.return_value = _mock_client("x" * 20_000)
        yield MockClient


async def test_the_configured_limit_truncates_when_the_caller_does_not_say(patched_client):
    fetcher = HttpFetcher(HttpFetcherConfig(max_html_chars=50))

    result = await fetcher.fetch_text("https://example.com")

    assert len(result.text) == 50, (
        "max_html_chars was ignored — the 10000-char literal on fetch_url was "
        "the only limit that ever applied"
    )


async def test_an_explicit_max_length_still_wins(patched_client):
    fetcher = HttpFetcher(HttpFetcherConfig(max_html_chars=50))

    result = await fetcher.fetch_text("https://example.com", max_length=120)

    assert len(result.text) == 120


async def test_the_toolkit_honours_a_config_handed_to_it(patched_client):
    tools = WebTools(config=HttpFetcherConfig(max_html_chars=64))

    assert len(await tools.fetch_url("https://example.com")) == 64


async def test_the_default_limit_is_unchanged_at_10000(patched_client):
    """Behaviour parity: the literal that used to live on fetch_url."""
    assert len(await WebTools().fetch_url("https://example.com")) == 10_000
