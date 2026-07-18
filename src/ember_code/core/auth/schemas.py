"""Pydantic models for the CLI auth flow.

Keeps typed wire-shapes separate from the OAuth server plumbing
in :mod:`ember_code.core.auth.portal_client` /
:mod:`ember_code.core.auth.callback_server`. Additions go here —
do not inline new models next to the classes.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from ember_code.core.auth.credentials import Credentials

logger = logging.getLogger(__name__)


class UserInfo(BaseModel):
    """Typed shape for ``/v1/portal/me`` responses.

    Only exposes the fields callers actually read (email + plan
    tier + org display name) so extra portal fields don't leak
    into typed callers. Rule 1: replaces the previous raw ``dict``
    return from :meth:`ember_code.core.auth.portal_client.PortalClient.validate_token`.

    ``model_config = {"extra": "ignore"}`` — the portal is free to
    add fields without breaking the CLI.
    """

    model_config = {"extra": "ignore"}

    email: str = ""
    tier: str | None = None
    org_display_name: str | None = None


LoginReason = Literal["ok", "timeout", "port_bind_failed", "handler_error", "error"]


class LoginResult(BaseModel):
    """Result of a full browser-callback login flow.

    Pattern 3: replaces the previous ``raise TimeoutError`` /
    bare-``tuple[str, str]`` returns with a single Pydantic value
    carrying the outcome, the token (on success), and a machine
    readable ``reason`` so callers can branch without string-matching
    exception messages.

    ``ok`` is the single source of truth — callers should check it
    before reading ``token`` or acting on ``user`` on the sibling
    :class:`ValidateResult`.
    """

    model_config = {"extra": "ignore"}

    ok: bool
    token: str = ""
    callback_url: str = ""
    reason: LoginReason = "ok"
    error: str = ""


ValidateReason = Literal[
    "ok",
    "http_error",
    "network_error",
    "decode_error",
    "schema_mismatch",
]


class ValidateResult(BaseModel):
    """Result of a portal-token validation call.

    Pattern 3: replaces the previous ambiguous ``UserInfo | None``
    collapse (which conflated network errors, non-200 responses,
    malformed JSON, and schema drift) with a Pydantic value that
    keeps the failure ``reason`` and optional HTTP ``status_code``
    for the caller to log / branch on.
    """

    model_config = {"extra": "ignore"}

    ok: bool
    user: UserInfo | None = None
    reason: ValidateReason = "ok"
    status_code: int | None = None
    error: str = ""


LoadCredentialsReason = Literal[
    "ok",
    "no_file",
    "malformed_json",
    "schema_mismatch",
    "error",
]


class LoadCredentialsResult(BaseModel):
    """Result of a :meth:`CredentialsStore.load` call.

    Pattern 3: replaces the previous ``Credentials | None`` collapse
    (which conflated "file missing" with "file corrupt") so the
    caller can branch on the machine-readable ``reason`` without
    ambiguity.

    Expiry is *not* a load-result reason — it's a property of the
    loaded :class:`Credentials` value itself, exposed via
    :meth:`Credentials.is_expired`.
    """

    model_config = {"extra": "ignore"}

    ok: bool
    creds: Credentials | None = None
    reason: LoadCredentialsReason = "ok"
    error: str = ""


class JwtClaims(BaseModel):
    """Typed shape for the JWT payload we care about.

    Pattern 1: replaces the previous raw-``dict`` return from
    ``decode_jwt_claims`` — callers get typed ``org`` / ``org_name``
    / ``email`` / ``exp`` fields instead of ``claims.get("org")``.

    ``model_config = {"extra": "ignore"}`` — the portal is free to
    add fields to the JWT payload without breaking the CLI.
    """

    model_config = {"extra": "ignore"}

    email: str | None = None
    exp: int | None = None
    org: str | None = None
    org_name: str | None = None

    @classmethod
    def decode(cls, token: str) -> JwtClaims | None:
        """Decode a JWT payload without verifying the signature.

        Safe for reading claims client-side — the server still
        validates the signature on every API call. Returns ``None``
        when the token is malformed or the payload can't be parsed.
        """
        try:
            payload = token.split(".")[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += "=" * padding
            decoded = base64.urlsafe_b64decode(payload)
            data = json.loads(decoded)
        except Exception as exc:
            logger.debug("Failed to decode JWT claims: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        return cls(**data)
