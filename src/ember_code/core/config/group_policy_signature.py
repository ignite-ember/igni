"""Is this group-policy pack one igni server issued, for this person?

igni server sends each person's group pack twice in one response: as the
plain fields it always had, and as ``signed_pack`` — a compact JWS over the
same content, signed with the key igni server signs its tokens with and
publishes at ``/.well-known/jwks.json`` (ember-server
``app/services/portal/group_pack_signature.py``). igni stores that string
exactly as received (``<data_dir>/group-policy/pack.jws``) and applies a
pack only after :func:`verify_pack` has accepted it.

## What is checked

* **The signature**, RS256 or ES256, against a trusted key (below). The
  header's ``alg`` must agree with the key it is checked against, ``typ``
  must say this is a group pack, and a ``crit`` header is refused.
* **``iss``** — the igni server this install is configured for
  (``api_url``). A pack from another server is not this one's.
* **``sub``** — the person signed in here (their session token's ``sub``).
  One person's pack is not replayable as another's.
* **``version``** — not older than the newest pack this client has already
  accepted. An older pack, even a genuine one, is a rollback.
* **``org``** — when the signed-in session names an organisation, the
  pack's must be the same one.
* **``no_pack: true``** with ``policy: null`` is igni server's signed "this
  person has no pack"; it verifies like a pack and means "clear the cache".
* **``token_use``** is ``group_policy`` and the payload carries a
  ``policy`` object. There is no ``exp``: a pack keeps working offline.

## Which keys are trusted

1. **Pinned by an administrator** — ``group_policy.trusted_keys``: RFC 7638
   thumbprints of igni server's signing keys (the ``kid`` igni server gives
   a key it generated *is* its thumbprint, but the thumbprint is computed
   here, never read from the ``kid``). Honoured only from the
   managed-settings file or ``IGNI_GROUP_POLICY_TRUSTED_KEYS``
   (:class:`~ember_code.core.config.merge_plan.ManagedOnlyTier`). The key
   material still comes from a JWKS — live, or the copy cached beside the
   pack — but only a key whose thumbprint is pinned is used, so a cached
   JWKS anybody could have rewritten cannot introduce a key.
2. **Otherwise, the JWKS igni server serves**, fetched over TLS when the
   pack is fetched, and cached beside it for offline starts. Without a pin
   that cached copy is only as trustworthy as the directory it is in: it
   stops a repository or a stray file from planting a policy, and it does
   **not** stop the person who owns the machine (or malware running as
   them) from replacing both the pack and the keys. Pinning is what closes
   that.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

#: The JWS ``typ`` igni server puts on a pack.
PACK_TYPE = "igni-group-policy+jwt"

#: The ``token_use`` claim on a pack.
PACK_USE = "group_policy"

#: The algorithm each key type may be used with — checked against the key,
#: never chosen by the header.
_ALGORITHM_FOR_KEY_TYPE: Mapping[str, str] = {"RSA": "RS256", "EC": "ES256"}

#: Pins from the environment: comma- or whitespace-separated thumbprints.
TRUSTED_KEYS_ENV = "IGNI_GROUP_POLICY_TRUSTED_KEYS"

#: ``1``/``true``/``yes``/``on`` makes a verified pack required.
REQUIRED_ENV = "IGNI_GROUP_POLICY_REQUIRED"


class PackRejected(Exception):
    """The pack is not one this install may apply, and why."""


def _b64decode(segment: str, *, what: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise PackRejected(f"the pack's {what} is not base64url") from exc


def _b64int(segment: Any, *, what: str) -> int:
    return int.from_bytes(_b64decode(str(segment), what=what), "big")


def jwk_thumbprint(jwk: Mapping[str, Any]) -> str:
    """RFC 7638: SHA-256 over the key's required members, base64url.

    The same computation igni server's ``PublicJwk.thumbprint`` makes, so
    an administrator can pin the value the portal shows (and the ``kid``
    of a generated key).
    """
    kty = jwk.get("kty")
    if kty == "RSA":
        members = {"e": jwk.get("e"), "kty": "RSA", "n": jwk.get("n")}
    elif kty == "EC":
        members = {"crv": jwk.get("crv"), "kty": "EC", "x": jwk.get("x"), "y": jwk.get("y")}
    else:
        raise PackRejected(f"a key of type {kty!r} is not RSA or EC")
    canonical = json.dumps(members, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode()


class _Key:
    """One public key from a JWKS."""

    def __init__(self, jwk: Mapping[str, Any]) -> None:
        self.kid = str(jwk.get("kid") or "")
        kty = str(jwk.get("kty") or "")
        if kty not in _ALGORITHM_FOR_KEY_TYPE:
            raise PackRejected(f"key {self.kid!r} is of type {kty!r}, not RSA or EC")
        self.algorithm = _ALGORITHM_FOR_KEY_TYPE[kty]
        self.thumbprint = jwk_thumbprint(jwk)
        try:
            if kty == "RSA":
                self._key: rsa.RSAPublicKey | ec.EllipticCurvePublicKey = rsa.RSAPublicNumbers(
                    e=_b64int(jwk["e"], what="key exponent"),
                    n=_b64int(jwk["n"], what="key modulus"),
                ).public_key()
            else:
                if jwk.get("crv") != "P-256":
                    raise PackRejected(f"key {self.kid!r} is not on P-256")
                self._key = ec.EllipticCurvePublicNumbers(
                    x=_b64int(jwk["x"], what="key x"),
                    y=_b64int(jwk["y"], what="key y"),
                    curve=ec.SECP256R1(),
                ).public_key()
        except KeyError as exc:
            raise PackRejected(f"key {self.kid!r} is missing {exc.args[0]!r}") from exc
        except ValueError as exc:
            raise PackRejected(f"key {self.kid!r} is not usable: {exc}") from exc

    def verifies(self, signing_input: bytes, signature: bytes) -> bool:
        try:
            if isinstance(self._key, rsa.RSAPublicKey):
                self._key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            else:
                if len(signature) != 64:
                    return False
                r = int.from_bytes(signature[:32], "big")
                s = int.from_bytes(signature[32:], "big")
                self._key.verify(
                    encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256())
                )
        except InvalidSignature:
            return False
        return True


def keys_from_jwks(jwks: Any) -> list[_Key]:
    """The usable keys in a JWKS; malformed entries are skipped."""
    raw = jwks.get("keys") if isinstance(jwks, Mapping) else None
    keys: list[_Key] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, Mapping):
            continue
        try:
            keys.append(_Key(entry))
        except PackRejected:
            continue
    return keys


def parse_pins(value: Any) -> tuple[str, ...]:
    """Thumbprints from the setting or the environment variable.

    Accepts a list of strings, or one string separated by commas or
    whitespace (the environment's form). Anything else pins nothing.
    """
    if isinstance(value, str):
        items: Iterable[Any] = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return ()
    return tuple(str(item).strip() for item in items if isinstance(item, str) and item.strip())


def pins_from_environment() -> tuple[str, ...]:
    return parse_pins(os.environ.get(TRUSTED_KEYS_ENV, ""))


def required_by_environment() -> bool:
    value = os.environ.get(REQUIRED_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def normalise_issuer(url: str | None) -> str:
    """``scheme://host[:port][/path]``, lower-cased host, no trailing slash."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return url.strip().rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def jwks_url_for(api_url: str) -> str:
    return f"{normalise_issuer(api_url)}/.well-known/jwks.json"


def may_trust_unpinned_jwks_from(api_url: str) -> bool:
    """Whether a JWKS fetched from ``api_url`` may be trusted without a pin.

    Over TLS, yes. Over plain HTTP only to loopback (a developer's own
    server): anywhere else the keys could be swapped in transit along with
    the pack, so an install on a plain-HTTP network has to pin a key.
    """
    parsed = urlparse(api_url or "")
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and (parsed.hostname or "") in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


@dataclass(frozen=True)
class VerifiedPack:
    """A pack :func:`verify_pack` accepted."""

    #: The pack's content, or ``None`` when igni server signed "this person
    #: has no pack" (:attr:`absent`).
    policy: dict[str, Any] | None
    issuer: str
    subject: str
    org: str | None
    version: int
    kid: str
    pinned: bool
    #: igni server's signed "no pack for this person": clear what is cached.
    absent: bool = False


def unverified_claims(compact: str) -> dict[str, Any]:
    """The payload, unchecked — for log lines about a pack being refused."""
    try:
        return json.loads(_b64decode(compact.split(".")[1], what="payload"))
    except Exception:  # noqa: BLE001 — diagnostics only
        return {}


def verify_pack(
    compact: str,
    *,
    jwks: Any,
    pins: Iterable[str] = (),
    api_url: str | None,
    subject: str | None,
    min_version: int | None = None,
    org: str | None = None,
) -> VerifiedPack:
    """The pack's content, if it is genuine and meant for this install.

    ``jwks`` supplies key material; ``pins``, when any are given, restricts
    it to keys with those thumbprints. ``org``, when the signed-in session
    names one, must be the pack's ``org`` too. Raises :class:`PackRejected`
    naming the first check that failed.
    """
    pins = tuple(pins)
    if not isinstance(compact, str) or compact.count(".") != 2:
        raise PackRejected("the pack is not a compact JWS (it is unsigned or truncated)")
    header_segment, payload_segment, signature_segment = compact.strip().split(".")
    try:
        header = json.loads(_b64decode(header_segment, what="header"))
    except json.JSONDecodeError as exc:
        raise PackRejected("the pack's header is not JSON") from exc
    if not isinstance(header, dict):
        raise PackRejected("the pack's header is not an object")
    if header.get("crit"):
        raise PackRejected("the pack has critical header parameters")
    if header.get("typ") != PACK_TYPE:
        raise PackRejected(f"the JWS is a {header.get('typ')!r}, not a group pack")
    algorithm = header.get("alg")
    if algorithm not in _ALGORITHM_FOR_KEY_TYPE.values():
        raise PackRejected(
            f"the pack is signed with {algorithm!r}; only RS256 and ES256 are accepted"
        )

    keys = keys_from_jwks(jwks)
    if pins:
        keys = [key for key in keys if key.thumbprint in pins]
        if not keys:
            raise PackRejected(
                "none of the keys pinned in group_policy.trusted_keys is available to check "
                "the pack against"
            )
    if not keys:
        raise PackRejected("there is no igni server key to check the pack against")

    kid = str(header.get("kid") or "")
    candidates = [k for k in keys if k.algorithm == algorithm and (not kid or k.kid == kid)]
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = _b64decode(signature_segment, what="signature")
    if not any(key.verifies(signing_input, signature) for key in candidates):
        raise PackRejected(
            "the pack's signature does not verify against "
            + ("a pinned key" if pins else "igni server's keys")
        )

    try:
        claims = json.loads(_b64decode(payload_segment, what="payload"))
    except json.JSONDecodeError as exc:
        raise PackRejected("the pack's payload is not JSON") from exc
    if not isinstance(claims, dict):
        raise PackRejected("the pack's payload is not an object")
    if claims.get("token_use") != PACK_USE:
        raise PackRejected("the signed object is not a group pack")

    expected_issuer = normalise_issuer(api_url)
    if not expected_issuer:
        raise PackRejected("api_url is not set, so the pack's issuer cannot be checked")
    if normalise_issuer(str(claims.get("iss") or "")) != expected_issuer:
        raise PackRejected(
            f"the pack was issued by {claims.get('iss')!r}, not by this install's igni server "
            f"({expected_issuer})"
        )
    if not subject:
        raise PackRejected("nobody is signed in, so the pack cannot be matched to a person")
    if str(claims.get("sub") or "") != subject:
        raise PackRejected("the pack was issued for somebody other than the person signed in")

    version = claims.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise PackRejected("the pack carries no version")
    if min_version is not None and version < min_version:
        raise PackRejected(
            f"the pack is older (version {version}) than one this install has already "
            f"applied (version {min_version}); a rollback is refused"
        )
    pack_org = str(claims.get("org")) if claims.get("org") else None
    if org and pack_org and pack_org != org:
        raise PackRejected(
            f"the pack is for organisation {pack_org!r}, not the one signed in here ({org!r})"
        )
    absent = claims.get("no_pack") is True
    policy = claims.get("policy")
    if absent:
        policy = None
    elif not isinstance(policy, dict):
        raise PackRejected("the pack carries no policy")

    return VerifiedPack(
        policy=policy,
        issuer=expected_issuer,
        subject=subject,
        org=pack_org,
        version=version,
        kid=kid,
        pinned=bool(pins),
        absent=absent,
    )
