"""Token persistence — save, load, validate, and clear stored credentials."""

import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_CREDENTIALS_PATH = "~/.ember/credentials.json"

# JWT default lifetime: 30 days (matches server-issued tokens)
DEFAULT_TOKEN_TTL = 30 * 24 * 3600


class Credentials(BaseModel):
    """Stored authentication credentials."""

    access_token: str
    token_type: str = "bearer"
    email: str = ""
    created_at: str = ""
    expires_at: str = ""


def _credentials_path(path: str | None = None) -> Path:
    """Resolve the credentials file path."""
    return Path(os.path.expanduser(path or DEFAULT_CREDENTIALS_PATH))


def save_credentials(
    token: str,
    email: str,
    path: str | None = None,
    ttl: int = DEFAULT_TOKEN_TTL,
) -> None:
    """Write credentials JSON with 0600 permissions."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    expires = datetime.fromtimestamp(now.timestamp() + ttl, tz=timezone.utc)

    creds = {
        "access_token": token,
        "token_type": "bearer",
        "email": email,
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }

    fp = _credentials_path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(creds, indent=2))
    os.chmod(fp, 0o600)
    logger.debug("Credentials saved to %s", fp)


def load_credentials(path: str | None = None) -> Credentials | None:
    """Read and validate stored credentials. Returns None if missing or malformed."""
    fp = _credentials_path(path)
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text())
        return Credentials(**data)
    except Exception as e:
        logger.warning("Failed to load credentials from %s: %s", fp, e)
        return None


def clear_credentials(path: str | None = None) -> None:
    """Delete the credentials file."""
    fp = _credentials_path(path)
    if fp.exists():
        fp.unlink()
        logger.debug("Credentials cleared: %s", fp)


def is_token_expired(creds: Credentials) -> bool:
    """Check whether the token has expired based on the stored expires_at."""
    if not creds.expires_at:
        return False  # no expiry info — assume valid
    try:
        from datetime import datetime, timezone

        expires = datetime.fromisoformat(creds.expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expires
    except Exception as exc:
        logger.debug("Failed to check token expiry: %s", exc)
        return False


def save_model_credentials(api_key: str, url: str, model_name: str = "MiniMax-M2.7") -> None:
    """Write model API key and URL into ~/.ember/config.yaml.

    Updates the model registry entry in-place, preserving other config.
    """
    import yaml

    config_path = Path.home() / ".ember" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config: dict = {}
    if config_path.exists():
        try:
            data = yaml.safe_load(config_path.read_text())
            if isinstance(data, dict):
                config = data
        except Exception as exc:
            logger.debug("Failed to load config.yaml: %s", exc)
            pass

    registry = config.setdefault("models", {}).setdefault("registry", {})
    model_entry = registry.setdefault(model_name, {})
    model_entry["api_key"] = api_key
    model_entry["url"] = url

    config_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    logger.debug("Model credentials for %s saved to %s", model_name, config_path)


def decode_jwt_claims(token: str) -> dict:
    """Decode JWT payload without verifying the signature.

    Safe for reading claims client-side — the server still validates
    the signature on every API call.
    """
    import base64

    try:
        payload = token.split(".")[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception as exc:
        logger.debug("Failed to decode JWT claims: %s", exc)
        return {}


class CloudCredentials:
    """Read-only view of the user's Ember Cloud credentials.

    One file read + one JWT decode per instance, cached for the
    instance's lifetime.

    Args:
        path: optional override; defaults to ``~/.ember/credentials.json``.
    """

    def __init__(self, path: str | None = None):
        self._path = path
        self._claims: dict | None = None
        self._loaded = False
        self._creds: Credentials | None = None

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        creds = load_credentials(self._path)
        if creds is None or is_token_expired(creds):
            return
        self._creds = creds
        self._claims = decode_jwt_claims(creds.access_token) if creds.access_token else {}

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
        return self._claims.get("org")

    @property
    def org_name(self) -> str | None:
        self._load()
        if self._claims is None:
            return None
        return self._claims.get("org_name")
