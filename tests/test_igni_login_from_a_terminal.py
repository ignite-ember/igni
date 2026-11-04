"""``igni login`` signs in from a terminal, and from a script.

Signing in was a slash command inside the app (``/login``); a terminal
had no way to do it, so ``igni ext sync`` and ``igni -m`` could not be
signed in without the app. ``--token-stdin`` is the scripted form.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from ember_code.backend.schemas_rpc import LoginResult
from ember_code.cli import cli
from ember_code.core.auth.credentials import CredentialsStore
from ember_code.core.auth.portal_client import PortalClient
from ember_code.core.auth.schemas import UserInfo, ValidateResult

login_cli = importlib.import_module("ember_code.cli.login")


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    value = SimpleNamespace(
        api_url="https://igni.test",
        portal_url="",
        auth=SimpleNamespace(credentials_file=str(tmp_path / "credentials.json")),
        storage=SimpleNamespace(data_dir=str(tmp_path / "data")),
    )
    monkeypatch.setattr(login_cli, "_settings", lambda: value)
    return value


def _validated(monkeypatch, *, ok: bool = True) -> None:
    async def validate(self, token: str) -> ValidateResult:
        if not ok:
            return ValidateResult(ok=False, reason="http_error", status_code=401)
        return ValidateResult(ok=True, user=UserInfo(email="ada@acme.test"), status_code=200)

    monkeypatch.setattr(PortalClient, "validate_token", validate)


def test_a_token_on_stdin_is_checked_and_kept(settings, monkeypatch):
    _validated(monkeypatch)
    result = CliRunner().invoke(cli, ["login", "--token-stdin"], input="tok-123\n")
    assert result.exit_code == 0, result.output
    assert "signed in to https://igni.test as ada@acme.test" in result.output
    stored = CredentialsStore(settings.auth.credentials_file).load()
    assert stored.creds is not None and stored.creds.access_token == "tok-123"


def test_a_token_igni_refuses_is_not_kept(settings, monkeypatch):
    _validated(monkeypatch, ok=False)
    result = CliRunner().invoke(cli, ["login", "--token-stdin"], input="nope\n")
    assert result.exit_code != 0
    assert "401" in result.output
    assert not Path(settings.auth.credentials_file).exists()


def test_login_with_no_server_names_the_setting(settings):
    settings.api_url = ""
    result = CliRunner().invoke(cli, ["login", "--token-stdin"], input="tok\n")
    assert result.exit_code != 0
    assert "api_url" in result.output


def test_the_browser_flow_prints_where_to_sign_in(settings, monkeypatch):
    async def login(self, on_status=None):
        on_status("Waiting for login in browser...\nIf the browser didn't open, go to:\nhttps://x")
        return LoginResult(ok=True, email="ada@acme.test")

    monkeypatch.setattr(login_cli.AuthController, "login", login)
    result = CliRunner().invoke(cli, ["login"])
    assert result.exit_code == 0, result.output
    assert "https://x" in result.output
    assert "ada@acme.test" in result.output
