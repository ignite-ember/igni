"""Login handler — checks credentials against the user store."""

import hashlib
import secrets

from src.auth.token import Token


def login(username: str, password: str) -> Token | None:
    user = _lookup_user(username)
    if not user:
        return None
    if not _verify_password(password, user["password_hash"]):
        return None
    return Token.issue(user_id=user["id"])


def _lookup_user(username: str) -> dict | None:
    return None


def _verify_password(password: str, expected_hash: str) -> bool:
    candidate = hashlib.sha256(password.encode()).hexdigest()
    return secrets.compare_digest(candidate, expected_hash)
