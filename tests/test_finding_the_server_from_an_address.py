"""A person knows their email address, not their server's URL.

Asking for a URL asks for something most people do not have. The
domain in the address is already the name of the organisation whose
igni they should reach, so the domain answers it — and the record
lives in **that organisation's own zone**, not in a lookup service
somebody else runs. A service in the middle would learn every user's
address before they had chosen anything, and would stand in the path
of every login including the air-gapped ones.

Whoever controls the domain decides where its people sign in. That is
the right party — and it is also why the fetch is treated as a request
to a host we were *told* about rather than one we chose.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ember_code.core.auth import discovery
from ember_code.core.auth.discovery import Discovered, discover, domain_of


def _answering(handler) -> None:
    """Point discovery at a transport instead of the network."""
    original = httpx.AsyncClient

    class _Client(original):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    discovery.httpx.AsyncClient = _Client  # type: ignore[misc]
    return original


def _record(**fields) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(fields).encode())


@pytest.fixture(autouse=True)
def _restore():
    original = httpx.AsyncClient
    yield
    discovery.httpx.AsyncClient = original  # type: ignore[misc]


class TestReadingTheAddress:
    @pytest.mark.parametrize(
        "email,expected",
        [
            ("sam@acme.example", "acme.example"),
            ("  Sam@ACME.Example  ", "acme.example"),
            ("sam+tag@acme.example", "acme.example"),
            ("sam@mail.acme.co.uk", "mail.acme.co.uk"),
        ],
    )
    def test_the_domain_is_what_matters(self, email, expected):
        assert domain_of(email) == expected

    @pytest.mark.parametrize(
        "email",
        [
            "",
            "sam",
            "sam@",
            "@acme.example",
            "sam@localhost",
            "sam@acme.example/../../evil.test",
            "sam@acme example",
            "sam@acme..example",
            "sam@.acme.example",
        ],
    )
    def test_anything_that_is_not_a_domain_is_refused(self, email):
        # The value is interpolated into a URL. A domain carrying a
        # slash must not become a request somewhere else, and
        # sanitising it into something plausible would be worse than
        # refusing.
        assert domain_of(email) == ""


class TestWhatTheDomainSays:
    @pytest.mark.asyncio
    async def test_a_published_record_is_the_answer(self):
        seen: list[str] = []

        def handler(request):
            seen.append(str(request.url))
            return _record(api_url="https://igni.acme.example")

        _answering(handler)

        found = await discover("sam@acme.example")

        assert found.found
        assert found.api_url == "https://igni.acme.example"
        assert found.source == "well-known"
        assert seen == ["https://acme.example/.well-known/igni.json"]

    @pytest.mark.asyncio
    async def test_a_portal_url_is_carried_when_given(self):
        _answering(
            lambda r: _record(
                api_url="https://igni.acme.example",
                portal_url="https://portal.acme.example",
            )
        )

        found = await discover("sam@acme.example")

        assert found.portal_url == "https://portal.acme.example"

    @pytest.mark.asyncio
    async def test_a_trailing_slash_is_not_carried_into_the_config(self):
        _answering(lambda r: _record(api_url="https://igni.acme.example/"))

        assert (await discover("sam@acme.example")).api_url == "https://igni.acme.example"

    @pytest.mark.asyncio
    async def test_a_domain_with_no_record_is_not_an_error(self):
        # The common case. The screen offers the address field; it
        # does not report a fault.
        _answering(lambda r: httpx.Response(404))

        found = await discover("sam@acme.example")

        assert not found.found
        assert "no igni record" in found.reason


class TestItIsCarefulAboutWhatItIsTold:
    @pytest.mark.asyncio
    async def test_a_plaintext_server_is_refused(self):
        # Whoever can see the traffic would otherwise choose the server
        # that receives sign-ins for the whole company.
        _answering(lambda r: _record(api_url="http://igni.acme.example"))

        found = await discover("sam@acme.example")

        assert not found.found
        assert "not an https URL" in found.reason

    @pytest.mark.asyncio
    async def test_a_local_deployment_may_speak_plaintext(self):
        # The one exception, for developing against your own box.
        _answering(lambda r: _record(api_url="http://localhost:7777"))

        assert (await discover("sam@acme.example")).api_url == "http://localhost:7777"

    @pytest.mark.asyncio
    async def test_a_redirect_is_not_followed(self):
        # A redirect is a different host answering a question asked of
        # this one.
        _answering(lambda r: httpx.Response(302, headers={"location": "https://evil.test/r.json"}))

        assert not (await discover("sam@acme.example")).found

    @pytest.mark.asyncio
    async def test_the_lookup_itself_is_https(self):
        seen: list[str] = []

        def handler(request):
            seen.append(request.url.scheme)
            return _record(api_url="https://igni.acme.example")

        _answering(handler)
        await discover("sam@acme.example")

        assert seen == ["https"]

    @pytest.mark.asyncio
    async def test_an_unsafe_portal_url_does_not_sink_the_lookup(self):
        # The api_url is usable alone and the portal is derived when
        # absent, so one bad field is dropped rather than fatal.
        _answering(
            lambda r: _record(api_url="https://igni.acme.example", portal_url="http://evil.test")
        )

        found = await discover("sam@acme.example")

        assert found.api_url == "https://igni.acme.example"
        assert found.portal_url == ""

    @pytest.mark.asyncio
    async def test_a_record_that_is_too_large_is_refused(self):
        _answering(lambda r: httpx.Response(200, content=b"x" * (discovery.MAX_BYTES + 1)))

        assert not (await discover("sam@acme.example")).found


class TestItNeverRaises:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "boom",
        [
            httpx.ConnectError("no route"),
            httpx.ReadTimeout("slow"),
            httpx.ConnectTimeout("slow"),
        ],
    )
    async def test_a_domain_that_does_not_answer_is_a_reason_not_a_crash(self, boom):
        def handler(request):
            raise boom

        _answering(handler)

        found = await discover("sam@acme.example")

        assert isinstance(found, Discovered)
        assert not found.found
        assert "did not answer" in found.reason

    @pytest.mark.asyncio
    async def test_a_record_that_is_not_json_is_a_reason(self):
        _answering(lambda r: httpx.Response(200, content=b"<html>nope</html>"))

        assert "not valid JSON" in (await discover("sam@acme.example")).reason

    @pytest.mark.asyncio
    async def test_a_record_without_an_api_url_is_a_reason(self):
        _answering(lambda r: _record(something_else="x"))

        assert "does not name an api_url" in (await discover("sam@acme.example")).reason

    @pytest.mark.asyncio
    async def test_a_bad_address_never_reaches_the_network(self):
        def handler(request):
            raise AssertionError(f"asked {request.url} about a non-address")

        _answering(handler)

        assert "does not look like an email" in (await discover("not-an-address")).reason


class TestWritingTheServerDown:
    """The fallback path: an address typed by hand.

    A deployment that publishes no record is still a deployment, so
    the screen offers a field. What arrives that way gets exactly the
    checks a discovered URL gets — "the user typed it" is not a reason
    to write a plaintext host into the file that decides where
    sign-in codes are sent.
    """

    @staticmethod
    def _controller(tmp_path, monkeypatch):
        from types import SimpleNamespace

        from ember_code.backend import server_auth
        from ember_code.core.config.user_config_store import UserConfigStore

        store = UserConfigStore(tmp_path / "config.yaml")
        monkeypatch.setattr(server_auth, "UserConfigStore", lambda *a, **k: store)
        controller = server_auth.AuthController.__new__(server_auth.AuthController)
        controller._settings = SimpleNamespace(api_url="", portal_url="")
        controller._portal = None
        return controller, store

    def test_an_https_server_is_written_down(self, tmp_path, monkeypatch):
        controller, store = self._controller(tmp_path, monkeypatch)

        answer = controller.set_server("https://igni.acme.example")

        assert answer["api_url"] == "https://igni.acme.example"
        assert store.read()["api_url"] == "https://igni.acme.example"

    def test_the_running_process_learns_too(self, tmp_path, monkeypatch):
        # A config file the running process disagrees with is the
        # confusing half of this operation.
        controller, _ = self._controller(tmp_path, monkeypatch)

        controller.set_server("https://igni.acme.example", "https://portal.acme.example")

        assert controller._settings.api_url == "https://igni.acme.example"
        assert controller._settings.portal_url == "https://portal.acme.example"

    def test_a_plaintext_server_is_refused(self, tmp_path, monkeypatch):
        controller, store = self._controller(tmp_path, monkeypatch)

        answer = controller.set_server("http://igni.acme.example")

        assert "not an https address" in answer["error"]
        assert store.read() == {}

    def test_nothing_typed_is_refused(self, tmp_path, monkeypatch):
        controller, store = self._controller(tmp_path, monkeypatch)

        assert "error" in controller.set_server("   ")
        assert store.read() == {}

    def test_an_existing_config_keeps_its_other_keys(self, tmp_path, monkeypatch):
        # This is somebody's config file, not ours to rewrite.
        controller, store = self._controller(tmp_path, monkeypatch)
        store.write({"models": {"default": "MiniMax-M2.7"}})

        controller.set_server("https://igni.acme.example")

        config = store.read()
        assert config["models"] == {"default": "MiniMax-M2.7"}
        assert config["api_url"] == "https://igni.acme.example"

    def test_a_local_deployment_may_be_named(self, tmp_path, monkeypatch):
        controller, store = self._controller(tmp_path, monkeypatch)

        assert controller.set_server("http://127.0.0.1:7777")["api_url"] == "http://127.0.0.1:7777"
