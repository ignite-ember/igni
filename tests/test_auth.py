"""Tests for auth/credentials.py — credential storage and JWT handling."""

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ember_code.core.auth.credentials import (
    CloudCredentials,
    Credentials,
    _credentials_path,
    clear_credentials,
    decode_jwt_claims,
    is_token_expired,
    load_credentials,
    save_credentials,
)


def _make_jwt(payload: dict) -> str:
    """Create a fake JWT with the given payload (no signature verification)."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


class TestCredentialsPath:
    def test_default_path(self):
        path = _credentials_path()
        assert path == Path.home() / ".ember" / "credentials.json"

    def test_custom_path(self, tmp_path):
        path = _credentials_path(str(tmp_path / "creds.json"))
        assert path == tmp_path / "creds.json"

    def test_expands_tilde(self):
        path = _credentials_path("~/.ember/credentials.json")
        assert str(Path.home()) in str(path)


class TestSaveAndLoadCredentials:
    def test_save_creates_file(self, tmp_path):
        creds_path = str(tmp_path / "creds.json")
        save_credentials("tok123", "user@test.com", path=creds_path)

        assert (tmp_path / "creds.json").exists()
        data = json.loads((tmp_path / "creds.json").read_text())
        assert data["access_token"] == "tok123"
        assert data["email"] == "user@test.com"

    def test_load_returns_credentials(self, tmp_path):
        creds_path = str(tmp_path / "creds.json")
        save_credentials("tok456", "a@b.com", path=creds_path)
        creds = load_credentials(path=creds_path)

        assert creds is not None
        assert creds.access_token == "tok456"
        assert creds.email == "a@b.com"

    def test_load_returns_none_for_missing(self, tmp_path):
        creds = load_credentials(path=str(tmp_path / "missing.json"))
        assert creds is None

    def test_load_returns_none_for_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json")
        creds = load_credentials(path=str(bad))
        assert creds is None


class TestClearCredentials:
    def test_removes_file(self, tmp_path):
        creds_path = str(tmp_path / "creds.json")
        save_credentials("tok", "a@b.com", path=creds_path)
        assert (tmp_path / "creds.json").exists()

        clear_credentials(path=creds_path)
        assert not (tmp_path / "creds.json").exists()

    def test_no_error_on_missing(self, tmp_path):
        clear_credentials(path=str(tmp_path / "missing.json"))  # should not raise


class TestTokenExpiry:
    def test_not_expired(self, tmp_path):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        creds = Credentials(
            access_token="tok",
            email="a@b.com",
            expires_at=future,
        )
        assert not is_token_expired(creds)

    def test_expired(self):
        past = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
        creds = Credentials(
            access_token="tok",
            email="a@b.com",
            expires_at=past,
        )
        assert is_token_expired(creds)

    def test_no_expiry_is_not_expired(self):
        creds = Credentials(
            access_token="tok",
            email="a@b.com",
        )
        assert not is_token_expired(creds)


class TestCloudCredentialsAccessToken:
    def test_returns_token(self, tmp_path):
        creds_path = str(tmp_path / "creds.json")
        save_credentials("my_token", "a@b.com", path=creds_path)
        creds = CloudCredentials(creds_path)
        assert creds.access_token == "my_token"
        assert creds.is_authenticated is True

    def test_returns_none_for_missing(self, tmp_path):
        creds = CloudCredentials(str(tmp_path / "nope.json"))
        assert creds.access_token is None
        assert creds.is_authenticated is False

    def test_returns_none_for_expired(self, tmp_path):
        creds_path = tmp_path / "creds.json"
        data = {
            "access_token": "expired_tok",
            "email": "a@b.com",
            "expires_at": time.time() - 100,
        }
        creds_path.write_text(json.dumps(data))
        creds = CloudCredentials(str(creds_path))
        assert creds.access_token is None


class TestDecodeJwtClaims:
    def test_decodes_valid_jwt(self):
        token = _make_jwt({"sub": "user1", "org": "org123"})
        claims = decode_jwt_claims(token)
        assert claims["sub"] == "user1"
        assert claims["org"] == "org123"

    def test_returns_empty_for_invalid(self):
        assert decode_jwt_claims("not.a.jwt") == {}
        assert decode_jwt_claims("") == {}
        assert decode_jwt_claims("single") == {}

    def test_handles_padding(self):
        # Payload that needs padding
        token = _make_jwt({"org": "x"})
        claims = decode_jwt_claims(token)
        assert claims["org"] == "x"


class TestCloudCredentialsOrg:
    def test_org_id(self, tmp_path):
        token = _make_jwt({"org": "org_42", "org_name": "Acme"})
        creds_path = tmp_path / "creds.json"
        data = {"access_token": token, "email": "a@b.com"}
        creds_path.write_text(json.dumps(data))

        assert CloudCredentials(str(creds_path)).org_id == "org_42"

    def test_org_name(self, tmp_path):
        token = _make_jwt({"org": "org_42", "org_name": "Acme"})
        creds_path = tmp_path / "creds.json"
        data = {"access_token": token, "email": "a@b.com"}
        creds_path.write_text(json.dumps(data))

        assert CloudCredentials(str(creds_path)).org_name == "Acme"

    def test_returns_none_when_no_token(self, tmp_path):
        creds = CloudCredentials(str(tmp_path / "missing.json"))
        assert creds.org_id is None
        assert creds.org_name is None

    def test_returns_none_when_claim_missing(self, tmp_path):
        token = _make_jwt({"sub": "user1"})  # no org claims
        creds_path = tmp_path / "creds.json"
        data = {"access_token": token, "email": "a@b.com"}
        creds_path.write_text(json.dumps(data))

        creds = CloudCredentials(str(creds_path))
        assert creds.org_id is None
        assert creds.org_name is None
