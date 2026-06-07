"""Tests for auth.login."""

from src.auth.login import login


def test_login_unknown_user_returns_none():
    assert login("nope", "x") is None
