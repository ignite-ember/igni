"""Tests for auth/credentials.py — credential storage and JWT handling."""

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ember_code.core.auth.credentials import (
    CloudCredentials,
    Credentials,
    CredentialsStore,
)
from ember_code.core.auth.schemas import JwtClaims


def _make_jwt(payload: dict) -> str:
    """Create a fake JWT with the given payload (no signature verification)."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


class TestCredentialsStorePath:
    def test_default_path(self):
        store = CredentialsStore()
        assert store.path == Path.home() / ".ember" / "credentials.json"

    def test_custom_path(self, tmp_path):
        store = CredentialsStore(str(tmp_path / "creds.json"))
        assert store.path == tmp_path / "creds.json"

    def test_expands_tilde(self):
        store = CredentialsStore("~/.ember/credentials.json")
        assert str(Path.home()) in str(store.path)


class TestSaveAndLoadCredentials:
    def test_save_creates_file(self, tmp_path):
        creds_path = str(tmp_path / "creds.json")
        store = CredentialsStore(creds_path)
        store.save(Credentials.new("tok123", "user@test.com"))

        assert (tmp_path / "creds.json").exists()
        data = json.loads((tmp_path / "creds.json").read_text())
        assert data["access_token"] == "tok123"
        assert data["email"] == "user@test.com"

    def test_load_returns_credentials(self, tmp_path):
        creds_path = str(tmp_path / "creds.json")
        store = CredentialsStore(creds_path)
        store.save(Credentials.new("tok456", "a@b.com"))
        result = store.load()

        assert result.ok is True
        assert result.creds is not None
        assert result.creds.access_token == "tok456"
        assert result.creds.email == "a@b.com"

    def test_load_returns_no_file(self, tmp_path):
        store = CredentialsStore(str(tmp_path / "missing.json"))
        result = store.load()
        assert result.ok is False
        assert result.reason == "no_file"
        assert result.creds is None

    def test_load_returns_malformed_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json")
        result = CredentialsStore(str(bad)).load()
        assert result.ok is False
        assert result.reason == "malformed_json"
        assert result.creds is None


class TestClearCredentials:
    def test_removes_file(self, tmp_path):
        creds_path = str(tmp_path / "creds.json")
        store = CredentialsStore(creds_path)
        store.save(Credentials.new("tok", "a@b.com"))
        assert (tmp_path / "creds.json").exists()

        store.clear()
        assert not (tmp_path / "creds.json").exists()

    def test_no_error_on_missing(self, tmp_path):
        store = CredentialsStore(str(tmp_path / "missing.json"))
        store.clear()  # should not raise


class TestTokenExpiry:
    def test_not_expired(self, tmp_path):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        creds = Credentials(
            access_token="tok",
            email="a@b.com",
            expires_at=future,
        )
        assert not creds.is_expired()

    def test_expired(self):
        past = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
        creds = Credentials(
            access_token="tok",
            email="a@b.com",
            expires_at=past,
        )
        assert creds.is_expired()

    def test_no_expiry_is_not_expired(self):
        creds = Credentials(
            access_token="tok",
            email="a@b.com",
        )
        assert not creds.is_expired()


class TestCloudCredentialsAccessToken:
    def test_returns_token(self, tmp_path):
        creds_path = str(tmp_path / "creds.json")
        CredentialsStore(creds_path).save(Credentials.new("my_token", "a@b.com"))
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
        claims = JwtClaims.decode(token)
        assert claims is not None
        assert claims.org == "org123"

    def test_returns_none_for_invalid(self):
        assert JwtClaims.decode("not.a.jwt") is None
        assert JwtClaims.decode("") is None
        assert JwtClaims.decode("single") is None

    def test_handles_padding(self):
        # Payload that needs padding
        token = _make_jwt({"org": "x"})
        claims = JwtClaims.decode(token)
        assert claims is not None
        assert claims.org == "x"


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
