"""Tests for ModelRegistryEntry.resolve_api_key — API key resolution."""

from ember_code.core.config.model_entry import ModelRegistryEntry


def _entry(**fields) -> ModelRegistryEntry:
    """Build a typed entry. Keys-only fields are exercised here — other
    fields on ModelRegistryEntry are provider kwargs the test doesn't
    touch, so a minimal ``model_id`` keeps validation happy."""
    fields.setdefault("model_id", "_test_")
    return ModelRegistryEntry.model_validate(fields)


class TestResolveApiKey:
    def test_direct_key(self):
        entry = _entry(api_key="sk-abc123")
        assert entry.resolve_api_key() == "sk-abc123"

    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("TEST_API_KEY", "sk-from-env")
        entry = _entry(api_key_env="TEST_API_KEY")
        assert entry.resolve_api_key() == "sk-from-env"

    def test_env_var_missing(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_KEY", raising=False)
        entry = _entry(api_key_env="NONEXISTENT_KEY")
        assert entry.resolve_api_key() is None

    def test_cmd(self):
        entry = _entry(api_key_cmd="echo sk-from-cmd")
        result = entry.resolve_api_key()
        assert result == "sk-from-cmd"

    def test_cmd_strips_whitespace(self):
        entry = _entry(api_key_cmd="echo '  sk-trimmed  '")
        result = entry.resolve_api_key()
        assert result == "sk-trimmed"

    def test_priority_direct_over_env(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "from-env")
        entry = _entry(api_key="from-direct", api_key_env="TEST_KEY")
        assert entry.resolve_api_key() == "from-direct"

    def test_priority_env_over_cmd(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "from-env")
        entry = _entry(api_key_env="TEST_KEY", api_key_cmd="echo from-cmd")
        assert entry.resolve_api_key() == "from-env"

    def test_empty_entry(self):
        assert _entry().resolve_api_key() is None

    def test_cmd_failure_returns_none(self):
        entry = _entry(api_key_cmd="false")  # command that exits non-zero
        assert entry.resolve_api_key() is None
