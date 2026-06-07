"""Tests for utils.format."""

from src.utils.format import to_kebab_case


def test_to_kebab_basic():
    assert to_kebab_case("Hello World") == "hello-world"
