"""Tests for auth.token."""

from src.auth.token import Token


def test_token_issue_sets_user_id():
    t = Token.issue(user_id="u-1")
    assert t.user_id == "u-1"
