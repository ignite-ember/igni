"""Authentication slash commands: ``/login``, ``/logout``, ``/whoami``.

Extracted from :mod:`ember_code.backend.command_handler` — three
commands for the Ember Cloud auth surface.

* ``/login`` — dispatches the login action; the transport
  layer opens the browser + handles the OAuth callback.
* ``/logout`` — clears credentials on disk, resets in-memory
  cloud state, and rebuilds the main agent. If the currently-
  selected model was cloud-backed (`api_key: "cloud_token"`),
  falls back to the first model that has its own credentials
  so the session doesn't brick.
* ``/whoami`` — read the credentials file, report identity +
  token-expiry status.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.core.auth.credentials import (
    CloudCredentials,
    clear_credentials,
    is_token_expired,
    load_credentials,
)

if TYPE_CHECKING:
    from ember_code.backend.command_handler import CommandHandler, CommandResult


async def cmd_login(handler: "CommandHandler") -> "CommandResult":
    """Dispatch the login action — transport handles the OAuth flow."""
    from ember_code.backend import command_handler as _handler

    return _handler.CommandResult.login()


async def cmd_logout(handler: "CommandHandler") -> "CommandResult":
    """Clear credentials, reset in-memory cloud state, rebuild agent."""
    from ember_code.backend import command_handler as _handler
    from ember_code.protocol.messages import CommandAction, CommandResultKind

    CommandResult = _handler.CommandResult
    creds = load_credentials()
    clear_credentials()

    # Clear in-memory cloud state and rebuild agent with direct model URL.
    messages: list[str] = []
    session = handler._session
    if session:
        session._cloud = CloudCredentials(path="/dev/null")

        # If current model uses cloud_token, switch to a non-cloud model.
        current = session.settings.models.default
        registry = session.settings.models.registry
        current_cfg = registry.get(current, {})
        if current_cfg.get("api_key") == "cloud_token":
            # Find first model with its own credentials.
            fallback = next(
                (
                    name
                    for name, cfg in registry.items()
                    if cfg.get("api_key") and cfg.get("api_key") != "cloud_token"
                ),
                None,
            )
            if fallback:
                session.settings.models.default = fallback
                messages.append(f"Switched to {fallback} (cloud model no longer available).")
            else:
                messages.append(
                    "Warning: no models with API keys configured. "
                    "Add a model with an api_key or /login again."
                )

        session.main_team = session._build_main_agent()

    email_msg = f"Logged out ({creds.email})." if creds else "Not logged in."
    messages.insert(0, email_msg)
    return CommandResult(
        kind=CommandResultKind.INFO,
        content="\n".join(messages),
        action=CommandAction.LOGOUT,
    )


async def cmd_whoami(handler: "CommandHandler") -> "CommandResult":
    """Report the logged-in identity + token expiry."""
    from ember_code.backend import command_handler as _handler

    CommandResult = _handler.CommandResult
    creds = load_credentials()
    if creds is None:
        return CommandResult.info("Not logged in. Use /login to authenticate.")
    if is_token_expired(creds):
        return CommandResult.info(
            f"Session expired for {creds.email}. Use /login to re-authenticate."
        )
    expires = creds.expires_at[:19] if creds.expires_at else "unknown"
    return CommandResult.info(f"Logged in as {creds.email} (expires: {expires})")
