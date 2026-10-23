"""Does this bundle come from somebody this deployment trusts?

The manifest has carried a ``signature`` field and a ``trust`` tier
since it was first modelled, the model has required one for anything
but a development build, and nothing has ever looked at it. A field
that a schema validates, a model requires and nobody checks is worse
than an absent one: it reads as a control.

**Offline, by construction.** Verification is a local public-key
check against keys the deployment already holds. Nothing is fetched,
no OCSP, no CRL, no registry — an air-gapped customer is the normal
case, not the exception, and a check that needs the network is a
check that gets skipped.

**Ed25519**, because the alternative is choosing a padding scheme and
a digest for RSA and getting one of them wrong. Small keys, small
signatures, no parameters to misconfigure.

## What is signed

Not the manifest alone. Signing the manifest would attest to the
permissions and leave the code they apply to unprotected, which is
the wrong way round: the whole point of the trust tier is that the
bundle an administrator approved is the bundle that runs.

So the digest covers every file in the bundle, each one's path and
its bytes, in sorted order — with the manifest's own ``signature``
field removed before it is hashed, because a value cannot cover
itself. Paths are included, so moving a file is a different bundle
even when the bytes are the same.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ember_code.extensions.manifest import ExtensionManifest

logger = logging.getLogger(__name__)

#: Separates a path from its bytes in the digest, and one entry from
#: the next. A byte that cannot occur in a path, so two different
#: file sets cannot hash the same by arranging their names.
_SEP = b"\x00"


class SignatureError(Exception):
    """The bundle is not signed by anyone this deployment trusts."""


@dataclass(frozen=True, slots=True)
class TrustedKey:
    """One public key an administrator added, and who it belongs to."""

    #: Whose key. Shown beside the trust tier, because "partner" only
    #: means something if the administrator knows which partner.
    owner: str
    key: Ed25519PublicKey

    @classmethod
    def from_base64(cls, owner: str, raw: str) -> TrustedKey:
        try:
            material = base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise SignatureError(f"key for {owner!r} is not valid base64: {exc}") from exc
        try:
            return cls(owner=owner, key=Ed25519PublicKey.from_public_bytes(material))
        except Exception as exc:
            raise SignatureError(
                f"key for {owner!r} is not a 32-byte Ed25519 public key: {exc}"
            ) from exc


class TrustStore:
    """The keys this deployment will accept a bundle from.

    Populated by an administrator — never by an install, and never
    from the bundle being installed, which would make the signature
    a formality that any bundle could satisfy by bringing its own
    key.
    """

    def __init__(self, keys: dict[str, TrustedKey] | None = None) -> None:
        self._keys: dict[str, TrustedKey] = dict(keys or {})

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, key: TrustedKey) -> None:
        self._keys[key.owner] = key

    def owners(self) -> list[str]:
        return sorted(self._keys)

    @classmethod
    def from_file(cls, path: Path) -> TrustStore:
        """Load ``{"owner": "<base64 public key>"}`` from JSON.

        A missing file is an empty store rather than an error: a
        deployment that trusts nobody yet is a normal state, and it
        fails at the install with a message about trust rather than
        here with one about a path.
        """
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SignatureError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise SignatureError(f"{path} should be an object of owner to key")
        return cls({owner: TrustedKey.from_base64(owner, value) for owner, value in raw.items()})

    def verify(self, digest: bytes, signature: bytes) -> TrustedKey:
        """The key that signed this digest.

        Every key is tried. There is no key hint in the manifest on
        purpose: a bundle naming which key to check against is a
        bundle choosing its own verifier, and with a handful of keys
        the cost of trying them all is nothing.
        """
        for key in self._keys.values():
            try:
                key.key.verify(signature, digest)
            except InvalidSignature:
                continue
            return key
        raise SignatureError(
            "no key this deployment trusts signed this bundle. "
            + (
                f"Trusted keys: {', '.join(self.owners())}."
                if self._keys
                else "This deployment trusts no keys yet; an administrator adds them."
            )
        )


class BundleDigest:
    """What a signature is over."""

    #: Files that are never part of the digest. ``manifest.json`` is
    #: hashed separately, with its signature removed.
    EXCLUDED = frozenset({"manifest.json"})

    @classmethod
    def of(cls, bundle: Path, manifest: ExtensionManifest) -> bytes:
        """The digest of one bundle, as both signer and verifier see it."""
        hasher = hashlib.sha256()

        # The manifest first, canonicalised and without its own
        # signature. ``sort_keys`` and a fixed separator so a
        # reformatted file is the same bundle — an administrator who
        # pretty-prints a manifest has not changed what it says.
        payload = manifest.model_dump(
            by_alias=True, exclude_none=True, exclude_defaults=True, mode="json"
        )
        payload.pop("signature", None)
        hasher.update(b"manifest.json")
        hasher.update(_SEP)
        hasher.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        hasher.update(_SEP)

        for path in cls.files(bundle):
            relative = path.relative_to(bundle).as_posix()
            hasher.update(relative.encode())
            hasher.update(_SEP)
            hasher.update(hashlib.sha256(path.read_bytes()).digest())
            hasher.update(_SEP)

        return hasher.digest()

    @classmethod
    def files(cls, bundle: Path) -> list[Path]:
        """Every file the digest covers, in a stable order.

        Sorted by POSIX relative path so the order does not depend on
        the filesystem, which is what made a digest computed on macOS
        disagree with the same bundle on Linux.
        """
        return sorted(
            (
                path
                for path in bundle.rglob("*")
                if path.is_file() and path.relative_to(bundle).as_posix() not in cls.EXCLUDED
            ),
            key=lambda path: path.relative_to(bundle).as_posix(),
        )


def verify_bundle(bundle: Path, manifest: ExtensionManifest, store: TrustStore) -> TrustedKey:
    """Check the bundle's signature, or raise :class:`SignatureError`.

    Returns the key that signed it, so the caller can record *who*
    an administrator ended up trusting rather than only that somebody
    did.
    """
    if not manifest.signature:
        raise SignatureError(
            f"{manifest.id} declares trust {manifest.trust.value!r} and carries no signature."
        )
    try:
        signature = base64.b64decode(manifest.signature, validate=True)
    except Exception as exc:
        raise SignatureError(f"{manifest.id}: signature is not valid base64: {exc}") from exc

    key = store.verify(BundleDigest.of(bundle, manifest), signature)
    logger.info("extension %s verified against %s's key", manifest.id, key.owner)
    return key


def sign_bundle(bundle: Path, manifest: ExtensionManifest, private_key: object) -> str:
    """Sign a bundle, returning the base64 signature for the manifest.

    Here rather than in a build tool so that signing and verifying
    read the same digest definition from the same file. The two
    drifting apart is the failure mode that produces "it verified on
    my machine", and it is undetectable until a customer's install
    refuses.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if not isinstance(private_key, Ed25519PrivateKey):
        raise SignatureError("signing needs an Ed25519 private key")
    return base64.b64encode(private_key.sign(BundleDigest.of(bundle, manifest))).decode()
