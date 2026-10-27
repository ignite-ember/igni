"""URLs, written the one way igni writes them, and the paths the wire fixes."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


class ServiceUrl:
    """A service's own URL as igni normalises it.

    A person's token for a service names the service's URL as its
    audience, and igni compares it as a string: it normalises the
    `client_uri` a service registers — lower-case scheme and host, no
    trailing slash, no query or fragment — and puts exactly that in every
    token. So the service normalises its own URL the same way before it
    registers it and before it compares against it.
    """

    @staticmethod
    def normalise(url: str) -> str:
        """`scheme://host[:port][/path]`. Raises `ValueError` for anything else."""
        parts = urlsplit((url or "").strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError(f"{url!r} is not an absolute http(s) URL")
        if parts.query or parts.fragment:
            raise ValueError(f"{url!r} must not have a query or a fragment")
        host = parts.hostname.lower()
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", ""))


class IgniPaths:
    """The paths igni serves, spelled once."""

    DISCOVERY = "/.well-known/openid-configuration"
    JWKS = "/.well-known/jwks.json"
    CLIENTS = "/v1/clients"
    TOKEN = "/v1/oauth/token"
    REVOKE = "/v1/oauth/revoke"
    DIRECTORY = "/v1/directory"
    NOTIFICATIONS = "/v1/service-notifications"
    #: What igni has revoked in an organisation (service-identity §21).
    REVOCATIONS = "/v1/revocations"

    @classmethod
    def extension(cls, client_id: str) -> str:
        return f"{cls.CLIENTS}/{client_id}/extension"

    @classmethod
    def extension_asset(cls, client_id: str, sha256: str) -> str:
        return f"{cls.CLIENTS}/{client_id}/extension/assets/{sha256}"

    #: Where igni calls a service about one person (erasure, export).
    SUBJECTS = "/v1/igni/subjects"
    #: Where igni tells a service that an organisation was deleted.
    ORGANISATIONS = "/v1/igni/organisations"
