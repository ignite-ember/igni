"""``igni login`` — sign in to igni server from a terminal.

The app signs in with ``/login``; a terminal had nothing, so ``igni ext
sync`` and ``igni -m`` runs could only use a sign-in the app had made.

* ``igni login`` runs the same flow as ``/login`` —
  :meth:`~ember_code.backend.server_auth.AuthController.login`, the
  portal's ``/cli-auth`` page and a callback on this machine — and
  prints the address to open, for a machine with no browser of its own.
* ``igni login --token-stdin`` keeps an access token handed to it on
  stdin, once igni server has accepted it (``/v1/portal/me``): for a
  script or CI job that already holds one. Never as an argument, which
  would leave it in the shell's history and the process list.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any

import click

from ember_code.backend.server_auth import AuthController
from ember_code.core.auth.credentials import Credentials, CredentialsStore
from ember_code.core.auth.portal_client import PortalClient
from ember_code.core.auth.schemas import JwtClaims
from ember_code.core.config.endpoint import ApiUrlNotConfigured, require_api_url


def _settings() -> Any:
    from ember_code.core.config.settings import load_settings

    return load_settings()


class _NoSession:
    """The one call AuthController makes on a session after signing in."""

    def replace_cloud_credentials(self, creds: object) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None


@click.command("login")
@click.option(
    "--token-stdin",
    is_flag=True,
    help="Read an access token from stdin and keep it, once igni server accepts it.",
)
@click.option(
    "--timeout",
    type=float,
    default=300.0,
    show_default=True,
    help="Seconds to wait for the browser sign-in.",
)
def login(token_stdin: bool, timeout: float) -> None:
    """Sign in to igni server.

    Opens the portal's sign-in page and waits for it to call back to
    this machine; the address is printed too, for a machine with no
    browser. With --token-stdin, keeps an access token read from stdin
    instead (e.g. `printf %s "$TOKEN" | igni login --token-stdin`).
    """
    settings = _settings()
    try:
        api_url = require_api_url(settings.api_url, operation="Signing in")
    except ApiUrlNotConfigured as exc:
        raise click.ClickException(str(exc)) from exc

    if token_stdin:
        token = sys.stdin.read().strip()
        if not token:
            raise click.ClickException("no token on stdin")
        email = asyncio.run(_keep(settings, api_url, token))
    else:
        controller = AuthController(_NoSession(), settings, lambda: None)  # type: ignore[arg-type]

        def status(text: str) -> None:
            click.echo(text)

        try:
            result = asyncio.run(asyncio.wait_for(controller.login(on_status=status), timeout))
        except TimeoutError as exc:
            raise click.ClickException(f"nobody signed in within {timeout:g}s") from exc
        if not result.ok:
            raise click.ClickException(f"signing in failed: {result.error}")
        email = result.email or ""
    click.echo(f"signed in to {api_url} as {email or 'an unnamed account'}")


async def _keep(settings: Any, api_url: str, token: str) -> str:
    portal = PortalClient(api_url=api_url, portal_url=getattr(settings, "portal_url", "") or None)
    checked = await portal.validate_token(token)
    if not checked.ok:
        why = f"HTTP {checked.status_code}" if checked.status_code else (checked.error or "")
        raise click.ClickException(
            f"igni server did not accept the token ({why or checked.reason})"
        )
    email = checked.user.email if checked.user else ""
    store = CredentialsStore(settings.auth.credentials_file)
    claims = JwtClaims.decode(token)
    if claims is not None and claims.exp:
        now = datetime.now(timezone.utc).timestamp()
        store.save(Credentials.new(token, email, ttl=max(int(claims.exp - now), 0)))
    else:
        store.save(Credentials.new(token, email))
    return email


__all__ = ["login"]
