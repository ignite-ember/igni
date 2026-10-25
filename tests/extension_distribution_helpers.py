"""What a service and igni server do, played by the tests.

Not a test module: no ``test_`` prefix, so pytest collects nothing.

* :class:`Publisher` — a service's registered key pair, signing its
  bundle the way igni server's ``docs/service-identity.md`` §8 says: a compact JWS
  over ``{"extension", "version", "sha256"}`` with ``kid`` set.
* :func:`bundle_archive` — the gzipped tar a service uploads.
* :func:`build_wheel` — a pure-Python wheel with a console script,
  written by hand so building it needs no network and no build backend.
* :class:`FakeIgni` — ``GET /v1/extensions``, the bundle download and
  the token endpoint, as an ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

API_URL = "https://igni.test"
SESSION = "session-token-of-the-person"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _uint(value: int) -> str:
    return b64url(value.to_bytes((value.bit_length() + 7) // 8 or 1, "big"))


class Publisher:
    """A service's key pair — RS256 or ES256 — and its signatures."""

    def __init__(self, kind: str = "RSA", kid: str = "service-key-1") -> None:
        self.kind = kind
        self.kid = kid
        if kind == "RSA":
            self._private: Any = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            self._private = ec.generate_private_key(ec.SECP256R1())

    @property
    def algorithm(self) -> str:
        return "RS256" if self.kind == "RSA" else "ES256"

    def jwk(self) -> dict[str, str]:
        numbers = self._private.public_key().public_numbers()
        if self.kind == "RSA":
            return {"kty": "RSA", "kid": self.kid, "n": _uint(numbers.n), "e": _uint(numbers.e)}
        return {
            "kty": "EC",
            "kid": self.kid,
            "crv": "P-256",
            "x": b64url(numbers.x.to_bytes(32, "big")),
            "y": b64url(numbers.y.to_bytes(32, "big")),
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.jwk()]}

    def sign(self, payload: dict[str, Any], *, header: dict[str, Any] | None = None) -> str:
        head = header if header is not None else {"alg": self.algorithm, "kid": self.kid}
        signing_input = (
            f"{b64url(json.dumps(head).encode())}.{b64url(json.dumps(payload).encode())}"
        ).encode()
        if self.kind == "RSA":
            signature = self._private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        else:
            der = self._private.sign(signing_input, ec.ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(der)
            signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{signing_input.decode()}.{b64url(signature)}"

    def sign_bundle(self, extension: str, version: str, archive: bytes) -> str:
        return self.sign(
            {
                "extension": extension,
                "version": version,
                "sha256": hashlib.sha256(archive).hexdigest(),
            }
        )


def manifest(
    extension_id: str = "acme.notes",
    version: str = "1.0.0",
    **changes: Any,
) -> dict[str, Any]:
    """A manifest a service would ship: ``python`` packaging, signed tier."""
    raw: dict[str, Any] = {
        "id": extension_id,
        "version": version,
        "contract": ">=1.0 <2",
        "trust": "first-party",
        "python": {
            "package": "acme-notes",
            "entry": "acme-notes",
            "wheels": "wheels",
            "mcp": {"transport": "stdio"},
        },
    }
    raw.update(changes)
    return raw


def bundle_archive(
    manifest_json: dict[str, Any],
    files: dict[str, bytes] | None = None,
    *,
    extra_members: list[tuple[tarfile.TarInfo, bytes | None]] | None = None,
) -> bytes:
    """A gzipped tar with ``manifest.json`` at its root."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        everything = {"manifest.json": json.dumps(manifest_json).encode(), **(files or {})}
        for name, data in everything.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info, data in extra_members or []:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


#: The console script's code: fetch the service token the way §9 says
#: an extension does, and print what came back.
SCRIPT = """
import json, os, sys, urllib.request, urllib.error


def main():
    url = os.environ.get("IGNI_SERVICE_TOKEN_URL")
    if not url:
        print(json.dumps({"hello": "from acme-notes"}))
        return
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + os.environ["IGNI_SERVICE_TOKEN_SECRET"]}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = {"status": exc.code}
    body["service_url_env"] = os.environ.get("IGNI_SERVICE_URL")
    print(json.dumps(body))
"""


def build_wheel(
    directory: Path,
    *,
    distribution: str = "acme_notes",
    version: str = "1.0.0",
    script: str = "acme-notes",
) -> Path:
    """A pure-Python wheel with one console script, no build backend."""
    directory.mkdir(parents=True, exist_ok=True)
    package = distribution
    dist_info = f"{distribution}-{version}.dist-info"
    files: dict[str, bytes] = {
        f"{package}/__init__.py": SCRIPT.encode(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {distribution.replace('_', '-')}\nVersion: {version}\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: igni-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": (
            f"[console_scripts]\n{script} = {package}:main\n"
        ).encode(),
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, data in files.items():
        digest = b64url(hashlib.sha256(data).digest())
        writer.writerow([name, f"sha256={digest}", str(len(data))])
    writer.writerow([f"{dist_info}/RECORD", "", ""])
    files[f"{dist_info}/RECORD"] = record.getvalue().encode()

    wheel = directory / f"{distribution}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return wheel


def wheel_files(tmp: Path, **kwargs: Any) -> dict[str, bytes]:
    """The wheelhouse as bundle members: ``wheels/<file>.whl``."""
    wheel = build_wheel(tmp, **kwargs)
    return {f"wheels/{wheel.name}": wheel.read_bytes()}


class Listed:
    """One extension igni distributes in a :class:`FakeIgni`."""

    def __init__(
        self,
        archive: bytes,
        publisher: Publisher,
        *,
        extension_id: str = "acme.notes",
        version: str = "1.0.0",
        service_url: str = "https://notes.example.com",
        signature: str | None = None,
        listed_sha256: str | None = None,
        header_sha256: str | None = None,
        jwks: dict[str, Any] | None = None,
    ) -> None:
        self.archive = archive
        self.extension_id = extension_id
        self.version = version
        self.service_url = service_url
        self.signature = (
            signature
            if signature is not None
            else publisher.sign_bundle(extension_id, version, archive)
        )
        sha = hashlib.sha256(archive).hexdigest()
        self.listed_sha256 = listed_sha256 or sha
        self.header_sha256 = header_sha256 or sha
        self.jwks = jwks or publisher.jwks()

    def row(self) -> dict[str, Any]:
        return {
            "extension_id": self.extension_id,
            "version": self.version,
            "name": self.extension_id,
            "description": "",
            "service_url": self.service_url,
            "sha256": self.listed_sha256,
            "signature": self.signature,
            "publisher_jwks": self.jwks,
            "published_at": "2026-10-24T00:00:00Z",
        }


class FakeIgni:
    """igni server's distribution and token endpoints."""

    def __init__(self) -> None:
        self.listed: dict[str, Listed] = {}
        #: The super admin's decisions on collisions, as the listing carries them.
        self.priority: list[str] = []
        self.requests: list[httpx.Request] = []
        self.listing_status = 200
        self.token_status = 200
        self.tokens_issued = 0
        self.on_token: Callable[[dict[str, list[str]]], None] | None = None
        #: Assets igni holds, by digest — what ``GET …/assets/{sha256}`` serves.
        self.assets: dict[str, bytes] = {}

    def publish(self, listed: Listed) -> None:
        self.listed[listed.extension_id] = listed

    def withdraw(self, extension_id: str) -> None:
        self.listed.pop(extension_id, None)

    def downloads(self) -> list[str]:
        return [r.url.path for r in self.requests if r.url.path.endswith("/bundle")]

    def asset_downloads(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests if "/assets/" in r.url.path]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/oauth/token":
            return self._token(request)
        if request.headers.get("authorization") != f"Bearer {SESSION}":
            return httpx.Response(401, json={"detail": "not signed in"})
        if path == "/v1/extensions":
            if self.listing_status != 200:
                return httpx.Response(self.listing_status, json={"detail": "down"})
            return httpx.Response(
                200,
                json={
                    "extensions": [entry.row() for entry in self.listed.values()],
                    "priority": self.priority,
                },
            )
        if path.startswith("/v1/extensions/") and "/assets/" in path:
            body = self.assets.get(path.rsplit("/", 1)[-1])
            if body is None:
                return httpx.Response(404)
            return httpx.Response(200, content=body)
        if path.startswith("/v1/extensions/") and path.endswith("/bundle"):
            extension_id = path.removeprefix("/v1/extensions/").removesuffix("/bundle")
            entry = self.listed.get(extension_id)
            if entry is None:
                return httpx.Response(404)
            return httpx.Response(
                200,
                content=entry.archive,
                headers={
                    "content-type": "application/gzip",
                    "X-Igni-Bundle-Signature": entry.signature,
                    "X-Igni-Bundle-Sha256": entry.header_sha256,
                },
            )
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if self.on_token is not None:
            self.on_token(form)
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "invalid_grant"})
        self.tokens_issued += 1
        audience = form["audience"][0]
        return httpx.Response(
            200,
            json={
                "access_token": f"token-{self.tokens_issued}-for-{audience}",
                "token_type": "Bearer",
                "expires_in": 600,
                "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            },
        )


def asset(
    name: str, platform: str, body: bytes, *, file: str | None = None, unpack: bool = False
) -> dict[str, Any]:
    """One asset as a manifest declares it."""
    return {
        "name": name,
        "platform": platform,
        "file": file or f"{name}.bin",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "source": f"https://downloads.example/{name}/{platform}",
        "unpack": unpack,
    }


def tar_gz(files: dict[str, bytes], *, executable: tuple[str, ...] = ()) -> bytes:
    """A gzipped tar, as a JVM or a database ships."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name in executable else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
