"""Token persistence — save, load, validate, and clear stored credentials.

Single-concern module: everything here is about the on-disk auth
credential file (``~/.ember/credentials.json`` by default).

* :class:`Credentials` — Pydantic model of the stored token +
  identity + expiry metadata. Behaviour on the type: the
  :meth:`Credentials.new` factory owns the ``created_at`` /
  ``expires_at`` math, and :meth:`Credentials.is_expired` owns
  the expiry check.
* :class:`CredentialsStore` — file-IO coordinator. One instance
  per credential file; ``save`` / ``load`` / ``clear`` are methods
  on the instance instead of free functions taking ``path`` as
  first arg.
* :class:`CloudCredentials` — read-only view over one credential
  file (one file read + one JWT decode, cached per instance).
  Wraps a :class:`CredentialsStore` collaborator.
* :class:`EmptyCloudCredentials` — subclass whose ``_load`` is a
  no-op, so :meth:`CloudCredentials.empty` returns a real instance
  built via ``__init__`` instead of ``cls.__new__`` reach-in.

The JWT-claim decoder lives on :class:`~ember_code.core.auth.schemas.JwtClaims`
— import from there when reading claims.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from ember_code.core.auth.schemas import JwtClaims, LoadCredentialsResult

logger = logging.getLogger(__name__)

DEFAULT_CREDENTIALS_PATH = "~/.ember/credentials.json"

# JWT default lifetime: 30 days (matches server-issued tokens)
DEFAULT_TOKEN_TTL = 30 * 24 * 3600


class Credentials(BaseModel):
    """Stored authentication credentials.

    Behaviour that operates on this shape lives here as methods
    (:meth:`is_expired`) and classmethods (:meth:`new`) instead
    of as free functions taking a :class:`Credentials` as first
    arg (Rule 1 / OOP hygiene).
    """

    access_token: str
    token_type: str = "bearer"
    email: str = ""
    created_at: str = ""
    expires_at: str = ""

    @classmethod
    def new(
        cls,
        token: str,
        email: str,
        ttl: int = DEFAULT_TOKEN_TTL,
    ) -> Credentials:
        """Construct a fresh :class:`Credentials` with the ``created_at``
        / ``expires_at`` math computed once at the point of creation.

        This is the canonical construction path used by
        :meth:`CredentialsStore.save` — the datetime bookkeeping is
        NOT the store's concern.
        """
        now = datetime.now(timezone.utc)
        expires = datetime.fromtimestamp(now.timestamp() + ttl, tz=timezone.utc)
        return cls(
            access_token=token,
            token_type="bearer",
            email=email,
            created_at=now.isoformat(),
            expires_at=expires.isoformat(),
        )

    def is_expired(self) -> bool:
        """True when the stored ``expires_at`` is at or before now.

        A missing / unparseable ``expires_at`` returns ``False`` —
        we treat "no expiry info" as "assume valid" to match the
        previous free-function behaviour.
        """
        if not self.expires_at:
            return False  # no expiry info — assume valid
        try:
            expires = datetime.fromisoformat(self.expires_at)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= expires
        except Exception as exc:
            logger.debug("Failed to check token expiry: %s", exc)
            return False


# Resolve the ``"Credentials | None"`` forward reference on the
# schema now that :class:`Credentials` is defined here. The schema
# module can't import it directly without a circular import.
LoadCredentialsResult.model_rebuild(_types_namespace={"Credentials": Credentials})


class CredentialsStore:
    """Read/write coordinator for one credential file.

    One instance per file path; the ``save`` / ``load`` / ``clear``
    operations are methods on the instance instead of free
    functions that take ``path`` as first arg (OOP: state that
    every operation reads becomes instance state, not a per-call
    kwarg).

    Args:
        path: optional override; defaults to
            ``~/.ember/credentials.json``. ``None`` or an empty
            string routes to the default.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path: Path = Path(os.path.expanduser(path or DEFAULT_CREDENTIALS_PATH))

    @property
    def path(self) -> Path:
        """The resolved on-disk path this store reads / writes."""
        return self._path

    def save(self, creds: Credentials) -> None:
        """Write ``creds`` as JSON with 0600 permissions.

        Callers construct the :class:`Credentials` via
        :meth:`Credentials.new` — this method does not do any
        datetime bookkeeping on its own.
        """
        fp = self._path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(creds.model_dump_json(indent=2))
        os.chmod(fp, 0o600)
        logger.debug("Credentials saved to %s", fp)

    def load(self) -> LoadCredentialsResult:
        """Read + validate the credential file into a Pattern-3
        :class:`LoadCredentialsResult`.

        ``reason`` disambiguates:

        * ``no_file`` — the file doesn't exist
        * ``malformed_json`` — file exists but isn't valid JSON
        * ``schema_mismatch`` — JSON parsed but Pydantic rejected it
        * ``error`` — any other IO/decoding failure
        * ``ok`` — ``creds`` is populated

        Expiry is *not* reported here — call :meth:`Credentials
        .is_expired` on the returned ``creds`` value.
        """
        fp = self._path
        if not fp.exists():
            return LoadCredentialsResult(ok=False, reason="no_file")
        try:
            raw = fp.read_text()
        except Exception as exc:
            logger.warning("Failed to read credentials from %s: %s", fp, exc)
            return LoadCredentialsResult(ok=False, reason="error", error=str(exc))
        try:
            data = json.loads(raw)
        except Exception as exc:
            logger.warning("Malformed credentials JSON at %s: %s", fp, exc)
            return LoadCredentialsResult(ok=False, reason="malformed_json", error=str(exc))
        try:
            creds = Credentials(**data)
        except Exception as exc:
            logger.warning("Credentials at %s don't match schema: %s", fp, exc)
            return LoadCredentialsResult(ok=False, reason="schema_mismatch", error=str(exc))
        return LoadCredentialsResult(ok=True, creds=creds)

    def clear(self) -> None:
        """Delete the credential file if it exists (no-op otherwise)."""
        fp = self._path
        if fp.exists():
            fp.unlink()
            logger.debug("Credentials cleared: %s", fp)


class CloudCredentials:
    """Read-only view of the user's Ember Cloud credentials.

    One file read + one JWT decode per instance, cached for the
    instance's lifetime.

    Args:
        path: optional override; defaults to
            ``~/.ember/credentials.json``. Preserved as a
            positional/keyword for back-compat with the many
            existing call sites (``sync_manager``, ``resolver``,
            ``session.core``, ``session.cloud_catalog``,
            ``settings``, ``models``, ``server_auth``).
        store: optional pre-built :class:`CredentialsStore` to
            reuse — mutually exclusive with ``path``. Only
            :meth:`empty` and new callers should use this.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        store: CredentialsStore | None = None,
    ) -> None:
        if store is not None and path is not None:
            raise ValueError("CloudCredentials: pass either path or store, not both")
        self._store: CredentialsStore = store or CredentialsStore(path)
        self._claims: JwtClaims | None = None
        self._loaded: bool = False
        self._creds: Credentials | None = None

    @classmethod
    def empty(cls) -> CloudCredentials:
        """Construct a logged-out :class:`CloudCredentials`.

        Every property (``access_token``, ``is_authenticated``,
        ``email``, ``org_id``, ``org_name``) short-circuits to
        ``None`` / ``False`` because :class:`EmptyCloudCredentials`
        overrides :meth:`_load` to a no-op — no file lookup ever
        happens. Killed the previous ``cls.__new__`` reach-in.
        """
        return EmptyCloudCredentials()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        result = self._store.load()
        if not result.ok or result.creds is None:
            return
        creds = result.creds
        if creds.is_expired():
            return
        self._creds = creds
        if creds.access_token:
            self._claims = JwtClaims.decode(creds.access_token)

    @property
    def access_token(self) -> str | None:
        self._load()
        return self._creds.access_token if self._creds else None

    @property
    def is_authenticated(self) -> bool:
        return self.access_token is not None

    @property
    def email(self) -> str | None:
        self._load()
        return self._creds.email if self._creds else None

    @property
    def org_id(self) -> str | None:
        self._load()
        if self._claims is None:
            return None
        return self._claims.org

    @property
    def org_name(self) -> str | None:
        self._load()
        if self._claims is None:
            return None
        return self._claims.org_name


class EmptyCloudCredentials(CloudCredentials):
    """A :class:`CloudCredentials` that is always logged-out.

    Subclass with an inert :meth:`_load` — no file lookup, no
    JWT decode. Returned by :meth:`CloudCredentials.empty` so the
    logged-out construction path goes through a real ``__init__``
    (with no ``cls.__new__`` shenanigans) and preserves
    ``isinstance(x, CloudCredentials)`` checks on the callers.
    """

    def __init__(self) -> None:  # noqa: D401 — override
        # Skip the parent's store setup entirely: this instance
        # never reads a credentials file.
        self._store = CredentialsStore(path=None)
        self._claims = None
        self._creds = None
        self._loaded = True

    def _load(self) -> None:  # noqa: D401 — override
        # No-op: we are the logged-out sentinel by construction.
        return
