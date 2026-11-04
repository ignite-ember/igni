"""Is this archive the one the service signed?

A service signs its extension bundle with the key it registered at
igni server — a compact JWS over
``{"extension": <id>, "version": <version>, "sha256": <hex digest of the archive>}``
(igni server's ``docs/service-identity.md`` §8). igni checks it on upload, and the
host checks it **again** before installing, against the publisher's
public keys that came with the distribution listing: a bundle altered
anywhere between the service and this machine does not run.

This replaces the Ed25519 trust store an administrator used to fill
by hand. Nobody adds keys here any more; the key is the one a super
admin compared against the service's log and approved, and igni hands
it over beside the bundle.

**Two algorithms and no others**: RS256 and ES256, the two a service
may register. ``alg: none``, an HMAC (which would need a shared
secret, the thing this design removes) or anything unexpected is a
refusal, and the algorithm must agree with the key it is checked
against — the classic JWS mistake is letting the token pick.

Verified with :mod:`cryptography` directly rather than a JOSE
library: the format is three base64url segments and the two checks
are a dozen lines, which is less than a new dependency costs an
air-gapped install.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from pydantic import BaseModel, ValidationError

#: The algorithm each key type may be used with. One each, so the
#: header's ``alg`` is checked against the key rather than chosen by it.
ALGORITHM_FOR_KEY_TYPE: Mapping[str, str] = {"RSA": "RS256", "EC": "ES256"}

#: An ES256 signature is ``r || s``, 32 bytes each.
_ES256_LENGTH = 64


class BundleSignatureError(Exception):
    """The archive is not what its publisher signed, and why."""


class BundleClaim(BaseModel):
    """What the signature says the archive is."""

    extension: str
    version: str
    sha256: str


class _Base64Url:
    """Decoding the unpadded base64url JWS uses."""

    @staticmethod
    def decode(segment: str, *, what: str) -> bytes:
        try:
            return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        except (binascii.Error, ValueError) as exc:
            raise BundleSignatureError(f"the signature's {what} is not base64url") from exc

    @staticmethod
    def to_int(segment: str, *, what: str) -> int:
        return int.from_bytes(_Base64Url.decode(segment, what=what), "big")


class PublisherKey:
    """One public key from the publisher's JWK set."""

    def __init__(self, jwk: Mapping[str, Any]) -> None:
        self.kid = str(jwk.get("kid") or "")
        self.kty = str(jwk.get("kty") or "")
        if self.kty not in ALGORITHM_FOR_KEY_TYPE:
            raise BundleSignatureError(f"a publisher key of type {self.kty!r} is not RSA or EC")
        self.algorithm = ALGORITHM_FOR_KEY_TYPE[self.kty]
        declared = jwk.get("alg")
        if declared and declared != self.algorithm:
            raise BundleSignatureError(
                f"publisher key {self.kid!r} declares {declared!r}, which is not "
                f"what a {self.kty} key signs with here"
            )
        self._key = self._public_key(jwk)

    def _public_key(self, jwk: Mapping[str, Any]) -> rsa.RSAPublicKey | ec.EllipticCurvePublicKey:
        try:
            if self.kty == "RSA":
                return rsa.RSAPublicNumbers(
                    e=_Base64Url.to_int(str(jwk["e"]), what="key exponent"),
                    n=_Base64Url.to_int(str(jwk["n"]), what="key modulus"),
                ).public_key()
            if jwk.get("crv") != "P-256":
                raise BundleSignatureError(f"publisher key {self.kid!r} is not on P-256")
            return ec.EllipticCurvePublicNumbers(
                x=_Base64Url.to_int(str(jwk["x"]), what="key x"),
                y=_Base64Url.to_int(str(jwk["y"]), what="key y"),
                curve=ec.SECP256R1(),
            ).public_key()
        except KeyError as exc:
            raise BundleSignatureError(
                f"publisher key {self.kid!r} is missing {exc.args[0]!r}"
            ) from exc
        except ValueError as exc:
            raise BundleSignatureError(f"publisher key {self.kid!r} is not usable: {exc}") from exc

    def verifies(self, signing_input: bytes, signature: bytes) -> bool:
        """Whether this key made ``signature`` over ``signing_input``."""
        try:
            if isinstance(self._key, rsa.RSAPublicKey):
                self._key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            else:
                if len(signature) != _ES256_LENGTH:
                    return False
                r = int.from_bytes(signature[:32], "big")
                s = int.from_bytes(signature[32:], "big")
                self._key.verify(
                    encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256())
                )
        except InvalidSignature:
            return False
        return True


class PublisherKeys:
    """The keys a service registered, as the distribution listing gives them."""

    def __init__(self, jwks: Mapping[str, Any]) -> None:
        raw = jwks.get("keys")
        if not isinstance(raw, list) or not raw:
            raise BundleSignatureError("the publisher has no keys to check the signature against")
        self._keys = [PublisherKey(key) for key in raw if isinstance(key, Mapping)]

    def verify(self, compact: str) -> BundleClaim:
        """The claim, if one of these keys signed it.

        A ``kid`` in the header narrows the candidates to that key and
        no other: a header naming a key the publisher does not have is
        not a reason to try the rest.
        """
        parts = compact.strip().split(".")
        if len(parts) != 3:
            raise BundleSignatureError("the signature is not a compact JWS")
        header_segment, payload_segment, signature_segment = parts
        try:
            header = json.loads(_Base64Url.decode(header_segment, what="header"))
        except json.JSONDecodeError as exc:
            raise BundleSignatureError("the signature's header is not JSON") from exc
        if not isinstance(header, dict):
            raise BundleSignatureError("the signature's header is not an object")
        if header.get("crit"):
            # Nothing here understands an extension a signer marked
            # critical, and RFC 7515 says not to accept one then.
            raise BundleSignatureError("the signature has critical header parameters")
        algorithm = header.get("alg")
        if algorithm not in ALGORITHM_FOR_KEY_TYPE.values():
            raise BundleSignatureError(
                f"the signature uses {algorithm!r}; only RS256 and ES256 are accepted"
            )

        kid = str(header.get("kid") or "")
        candidates = [
            key for key in self._keys if key.algorithm == algorithm and (not kid or key.kid == kid)
        ]
        signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
        signature = _Base64Url.decode(signature_segment, what="signature")
        if not any(key.verifies(signing_input, signature) for key in candidates):
            raise BundleSignatureError("the bundle is not signed by its publisher's key")

        try:
            return BundleClaim.model_validate_json(
                _Base64Url.decode(payload_segment, what="payload")
            )
        except ValidationError as exc:
            raise BundleSignatureError(f"the signed claim is malformed: {exc}") from exc
