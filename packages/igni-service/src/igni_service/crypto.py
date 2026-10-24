"""RSA keys, JWKs, and the two things signed with them.

RS256 everywhere, 2048 bits, the same as igni's own key. A JWK's `kid`
is its RFC 7638 thumbprint, which is also what a super admin compares in
igni's portal before approving a service.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.api_jws import PyJWS
from pydantic import BaseModel, ConfigDict, Field

#: The only algorithm on this wire. Chosen here, never by a token's header.
ALGORITHM = "RS256"


class Base64Url:
    """base64url without padding, as JOSE writes it."""

    @staticmethod
    def encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    @classmethod
    def uint(cls, value: int) -> str:
        return cls.encode(value.to_bytes((value.bit_length() + 7) // 8 or 1, "big"))


class Jwk(BaseModel):
    """One public key, as a JWK set carries it."""

    model_config = ConfigDict(extra="allow", frozen=True)

    kty: str
    kid: str = ""
    n: str | None = None
    e: str | None = None
    alg: str | None = None
    use: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)

    def public_key(self) -> Any:
        """The key as PyJWT verifies with it."""
        return jwt.PyJWK(self.as_dict(), algorithm=ALGORITHM).key


class JwkSet(BaseModel):
    keys: list[Jwk] = Field(default_factory=list)

    def find(self, kid: str) -> Jwk | None:
        return next((key for key in self.keys if key.kid == kid and key.kty == "RSA"), None)


class BundleClaim(BaseModel):
    """What the signature over an extension bundle says (§8)."""

    extension: str
    version: str
    sha256: str


class RsaKeyPair:
    """A private RSA key and what is derived from it."""

    KEY_SIZE = 2048

    def __init__(self, private_key: rsa.RSAPrivateKey) -> None:
        self._private = private_key

    @classmethod
    def generate(cls) -> RsaKeyPair:
        return cls(rsa.generate_private_key(public_exponent=65537, key_size=cls.KEY_SIZE))

    @classmethod
    def from_pem(cls, pem: str) -> RsaKeyPair:
        loaded = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise ValueError("not an RSA private key")
        return cls(loaded)

    @property
    def private_key(self) -> rsa.RSAPrivateKey:
        return self._private

    def private_pem(self) -> str:
        return self._private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    def thumbprint(self) -> str:
        """RFC 7638: SHA-256 over `{"e","kty","n"}`, in that order, no spaces."""
        numbers = self._private.public_key().public_numbers()
        members = {"e": Base64Url.uint(numbers.e), "kty": "RSA", "n": Base64Url.uint(numbers.n)}
        canonical = json.dumps(members, separators=(",", ":"), sort_keys=True).encode()
        return Base64Url.encode(hashlib.sha256(canonical).digest())

    def public_jwk(self, kid: str | None = None) -> Jwk:
        """The public half. `kid` is the thumbprint unless named."""
        numbers = self._private.public_key().public_numbers()
        return Jwk(
            kty="RSA",
            use="sig",
            alg=ALGORITHM,
            kid=kid or self.thumbprint(),
            n=Base64Url.uint(numbers.n),
            e=Base64Url.uint(numbers.e),
        )

    def sign_jwt(self, claims: dict[str, Any], *, kid: str | None = None) -> str:
        """A compact JWT over `claims`, RS256, with `kid` in its header."""
        return jwt.encode(
            claims, self._private, algorithm=ALGORITHM, headers={"kid": kid or self.thumbprint()}
        )

    def sign_bytes(self, payload: bytes, *, kid: str | None = None) -> str:
        """A compact JWS over arbitrary bytes, RS256, with `kid` in its header."""
        return PyJWS().encode(
            payload, self._private, algorithm=ALGORITHM, headers={"kid": kid or self.thumbprint()}
        )

    def sign_bundle(self, claim: BundleClaim) -> str:
        return self.sign_bytes(claim.model_dump_json().encode())

    @staticmethod
    def new_jti() -> str:
        """A token id nobody else will use."""
        return uuid.uuid4().hex
