"""A fresh install says so, instead of advertising what it cannot do.

With no model configured, every capability the welcome screen offers
comes back `No model configured. Run /login…`. The screen had no way to
know that: it could only compare `status.model` against the id string of
the `NoModelConfigured` placeholder, which made a sentinel into a
contract between two files that had no idea they shared one.

`status.model_configured` answers it outright, from the side that knows.
"""

from __future__ import annotations

from ember_code.core.config.models import ModelRegistry
from ember_code.core.config.settings import Settings


def _settings(**models) -> Settings:
    s = Settings()
    for key, value in models.items():
        setattr(s.models, key, value)
    return s


def test_a_fresh_install_has_no_usable_model():
    """Nothing configured and nothing discovered — the first-run state
    this whole change exists for."""
    registry = ModelRegistry(_settings(default="", registry={}))

    assert registry.has_usable_model() is False


def test_an_explicit_default_counts():
    registry = ModelRegistry(_settings(default="gpt-4o", registry={}))

    assert registry.has_usable_model() is True


def test_a_discovered_model_counts_even_without_a_default():
    """Cloud discovery merges entries without necessarily setting a
    default; `_effective_default` falls through to the first key, so a
    logged-in user with models is configured."""
    registry = ModelRegistry(_settings(default="", registry={"claude-opus-5": {}}))

    assert registry.has_usable_model() is True


def test_the_answer_matches_what_get_model_would_do():
    """The point of the method: it predicts the fallthrough rather than
    reimplementing a rule that could drift from it."""
    from ember_code.core.config.null_model import NoModelConfigured

    empty = ModelRegistry(_settings(default="", registry={}))
    assert empty.has_usable_model() is False
    assert isinstance(empty.get_model(), NoModelConfigured)
