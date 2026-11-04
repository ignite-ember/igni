"""Signing group packs the way igni server does, for tests.

igni server signs each person's pack as a compact JWS with its token
signing key (igni__server ``app/services/portal/group_pack_signature.py``)
and igni applies only a pack whose signature verifies. Tests that want a
policy applied therefore need a pack signed by a key igni trusts, for the
person signed in, by the server igni is configured for. This is that
server, in miniature.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ember_code.core.config.group_policy import GroupPolicyCache, GroupPolicyPack
from ember_code.core.config.group_policy_signature import PACK_TYPE, PACK_USE, jwk_thumbprint

#: The ``api_url`` tests configure, and so the ``iss`` a pack must carry.
ISSUER = "https://igni.example.invalid"

#: The person signed in, in tests that sign somebody in.
PERSON = "person-1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64_uint(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8 or 1, "big"))


class ServerKey:
    """An RSA signing key, as igni server generates one."""

    def __init__(self) -> None:
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self._private.public_key().public_numbers()
        jwk: dict[str, str] = {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "n": _b64_uint(numbers.n),
            "e": _b64_uint(numbers.e),
        }
        jwk["kid"] = jwk_thumbprint(jwk)
        self.jwk = jwk

    @property
    def kid(self) -> str:
        return self.jwk["kid"]

    @property
    def thumbprint(self) -> str:
        return jwk_thumbprint(self.jwk)

    @property
    def jwks(self) -> dict[str, Any]:
        return {"keys": [dict(self.jwk)]}

    def sign_claims(self, claims: dict[str, Any], *, header: dict[str, Any] | None = None) -> str:
        head = {"alg": "RS256", "kid": self.kid, "typ": PACK_TYPE, **(header or {})}
        signing_input = f"{_b64(json.dumps(head).encode())}.{_b64(json.dumps(claims).encode())}"
        signature = self._private.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{_b64(signature)}"

    def sign(
        self,
        pack: GroupPolicyPack,
        *,
        sub: str = PERSON,
        iss: str = ISSUER,
        org: str | None = "org-1",
        version: int | None = None,
    ) -> str:
        """``pack`` as igni server would sign it for ``sub``."""
        now = time.time()
        policy = pack.model_dump(mode="json", exclude={"etag", "signed_pack"})
        return self.sign_claims(
            {
                "iss": iss,
                "sub": sub,
                "org": org,
                "iat": int(now),
                "version": version if version is not None else int(now * 1000),
                "token_use": PACK_USE,
                "policy": policy,
            }
        )

    def sign_absence(
        self,
        *,
        sub: str = PERSON,
        iss: str = ISSUER,
        org: str | None = "org-1",
        version: int | None = None,
    ) -> str:
        """igni server's signed "no pack for ``sub``"."""
        now = time.time()
        return self.sign_claims(
            {
                "iss": iss,
                "sub": sub,
                "org": org,
                "iat": int(now),
                "version": version if version is not None else int(now * 1000),
                "token_use": PACK_USE,
                "policy": None,
                "no_pack": True,
            }
        )

    def signed(self, pack: GroupPolicyPack, **kwargs: Any) -> GroupPolicyPack:
        """A copy of ``pack`` carrying its signed form, as the portal client returns it."""
        return pack.model_copy(update={"signed_pack": self.sign(pack, **kwargs)})


#: One key for the whole suite: generating RSA keys is slow.
KEY = ServerKey()


def session_token(sub: str = PERSON, org: str | None = None) -> str:
    """A session token naming ``sub`` (and ``org``). igni decodes it without verifying."""
    head = _b64(json.dumps({"alg": "none"}).encode())
    claims: dict[str, Any] = {"sub": sub, "exp": int(time.time()) + 3600}
    if org:
        claims["org"] = org
    body = _b64(json.dumps(claims).encode())
    return f"{head}.{body}.sig"


def sign_in(credentials_file: Path, sub: str = PERSON, org: str | None = None) -> str:
    """Write a credentials file for ``sub``; returns the token."""
    token = session_token(sub, org)
    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    credentials_file.write_text(
        json.dumps({"access_token": token, "email": f"{sub}@example.invalid"})
    )
    return token


def store(
    cache_dir: Path,
    pack: GroupPolicyPack,
    *,
    key: ServerKey = KEY,
    jwks: dict | None = None,
    version: int | None = None,
    **sign_kwargs: Any,
) -> str:
    """Put ``pack`` in the cache the way a verified hydrate leaves it."""
    compact = key.sign(pack, version=version, **sign_kwargs)
    cache = GroupPolicyCache(cache_dir=cache_dir)
    cache.materialize(pack)
    cache.store_signed(
        compact,
        jwks=jwks if jwks is not None else key.jwks,
        version=version if version is not None else json.loads(_payload(compact))["version"],
    )
    return compact


def _payload(compact: str) -> bytes:
    segment = compact.split(".")[1]
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
