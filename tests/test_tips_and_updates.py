"""Tests for tips and update checker."""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.core.config.settings import (
    GuardrailsConfig,
    KnowledgeConfig,
    PermissionsConfig,
    Settings,
)
from ember_code.core.utils.tips import CONTEXTUAL_TIPS, GENERAL_TIPS, get_tip, random_tip
from ember_code.core.utils.update_checker import (
    UpdateInfo,
    _is_newer,
    _parse_version,
    _read_cache,
    _write_cache,
    check_for_update,
)

# ── Tips ────────────────────────────────────────────────────────────


class TestTips:
    def test_general_tips_not_empty(self):
        assert len(GENERAL_TIPS) > 0

    def test_contextual_tips_not_empty(self):
        assert len(CONTEXTUAL_TIPS) > 0

    def test_all_general_tips_are_strings(self):
        for tip in GENERAL_TIPS:
            assert isinstance(tip, str)
            assert len(tip) > 0

    def test_random_tip_returns_string(self):
        tip = random_tip()
        assert isinstance(tip, str)
        assert tip in GENERAL_TIPS

    def test_get_tip_no_settings(self):
        """Without settings, returns a general tip."""
        tip = get_tip()
        assert isinstance(tip, str)
        assert tip in GENERAL_TIPS

    def test_get_tip_contextual_knowledge_disabled(self, tmp_path):
        """When knowledge is disabled, suggests enabling it."""
        settings = Settings(knowledge=KnowledgeConfig(enabled=False))
        # Create ember.md so that tip doesn't fire instead
        (tmp_path / "ember.md").write_text("# test")
        tip = get_tip(settings, tmp_path)
        assert isinstance(tip, str)
        assert len(tip) > 0

    def test_get_tip_contextual_no_ember_md(self, tmp_path):
        """When ember.md is missing, suggests creating it."""
        settings = Settings()
        tip = get_tip(settings, tmp_path)
        # Should be one of the contextual tips (ember.md missing is a match)
        assert isinstance(tip, str)
        assert len(tip) > 0

    def test_get_tip_all_features_enabled(self, tmp_path):
        """When everything is configured, falls back to general tips."""
        (tmp_path / "ember.md").write_text("# test")
        (tmp_path / ".ember" / "agents").mkdir(parents=True)
        (tmp_path / ".ember" / "agents" / "custom.md").write_text("---\nname: custom\n---")
        settings = Settings(
            knowledge=KnowledgeConfig(enabled=True, share=True),
            guardrails=GuardrailsConfig(pii_detection=True, prompt_injection=True, moderation=True),
            reasoning=Settings().reasoning.model_copy(update={"enabled": True}),
            learning=Settings().learning.model_copy(update={"enabled": True}),
            permissions=PermissionsConfig(web_search="allow"),
        )
        tip = get_tip(settings, tmp_path)
        assert isinstance(tip, str)
        assert tip in GENERAL_TIPS


# ── Version parsing ─────────────────────────────────────────────────


class TestVersionParsing:
    def test_parse_simple(self):
        assert _parse_version("1.2.3") == (1, 2, 3)

    def test_parse_with_v_prefix(self):
        assert _parse_version("v0.1.0") == (0, 1, 0)

    def test_parse_two_parts(self):
        assert _parse_version("1.0") == (1, 0)

    def test_parse_invalid(self):
        assert _parse_version("abc") == (0,)

    def test_is_newer_true(self):
        assert _is_newer("0.2.0", "0.1.0") is True

    def test_is_newer_false_same(self):
        assert _is_newer("0.1.0", "0.1.0") is False

    def test_is_newer_false_older(self):
        assert _is_newer("0.1.0", "0.2.0") is False

    def test_is_newer_major(self):
        assert _is_newer("1.0.0", "0.9.9") is True

    def test_is_newer_patch(self):
        assert _is_newer("0.1.1", "0.1.0") is True


# ── UpdateInfo ──────────────────────────────────────────────────────


class TestUpdateInfo:
    def test_no_update(self):
        info = UpdateInfo(available=False, current_version="0.1.0")
        assert info.message == ""

    def test_update_available(self):
        info = UpdateInfo(
            available=True,
            latest_version="0.2.0",
            current_version="0.1.0",
            release_notes="Bug fixes",
            download_url="https://example.com",
        )
        assert "0.1.0" in info.message
        assert "0.2.0" in info.message
        assert "Bug fixes" in info.message
        assert "https://example.com" in info.message

    def test_update_no_notes(self):
        info = UpdateInfo(
            available=True,
            latest_version="0.2.0",
            current_version="0.1.0",
        )
        assert "0.2.0" in info.message

    def test_error_no_message(self):
        info = UpdateInfo(error="network error")
        assert info.message == ""


# ── Cache ───────────────────────────────────────────────────────────


class TestCache:
    def test_write_and_read(self, tmp_path):
        cache_file = tmp_path / ".update-check"
        with patch("ember_code.core.utils.update_checker.CACHE_FILE", cache_file):
            data = {"latest_version": "0.2.0"}
            _write_cache(data)
            result = _read_cache(ttl=86400)
            assert result is not None
            assert result["latest_version"] == "0.2.0"

    def test_expired_cache(self, tmp_path):
        cache_file = tmp_path / ".update-check"
        with patch("ember_code.core.utils.update_checker.CACHE_FILE", cache_file):
            data = {"latest_version": "0.2.0", "checked_at": time.time() - 86401}
            cache_file.write_text(json.dumps(data))
            result = _read_cache(ttl=86400)
            assert result is None

    def test_missing_cache(self, tmp_path):
        cache_file = tmp_path / "nonexistent"
        with patch("ember_code.core.utils.update_checker.CACHE_FILE", cache_file):
            result = _read_cache(ttl=86400)
            assert result is None


# ── check_for_update ────────────────────────────────────────────────


class TestCheckForUpdate:
    @staticmethod
    def _make_mock_client(response_data):
        """Create a mock httpx.AsyncClient that returns the given JSON."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = response_data

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        return mock_client

    @pytest.mark.asyncio
    async def test_no_update(self, tmp_path):
        cache_file = tmp_path / ".update-check"
        mock_client = self._make_mock_client({"info": {"version": "0.1.0"}})
        with (
            patch("ember_code.core.utils.update_checker.CACHE_FILE", cache_file),
            patch("ember_code.core.utils.update_checker.__version__", "0.1.0"),
            patch(
                "ember_code.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            info = await check_for_update()
            assert info.available is False

    @pytest.mark.asyncio
    async def test_update_available(self, tmp_path):
        cache_file = tmp_path / ".update-check"
        mock_client = self._make_mock_client({"info": {"version": "0.2.0"}})
        with (
            patch("ember_code.core.utils.update_checker.CACHE_FILE", cache_file),
            patch("ember_code.core.utils.update_checker.__version__", "0.1.0"),
            patch(
                "ember_code.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            info = await check_for_update()
            assert info.available is True
            assert info.latest_version == "0.2.0"

    @pytest.mark.asyncio
    async def test_network_error(self, tmp_path):
        cache_file = tmp_path / ".update-check"
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("timeout"))

        with (
            patch("ember_code.core.utils.update_checker.CACHE_FILE", cache_file),
            patch("ember_code.core.utils.update_checker.__version__", "0.1.0"),
            patch(
                "ember_code.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            info = await check_for_update()
            assert info.available is False
            assert info.error is not None

    @pytest.mark.asyncio
    async def test_uses_cache(self, tmp_path):
        cache_file = tmp_path / ".update-check"
        cached_data = {
            "latest_version": "0.3.0",
            "release_notes": "Cached",
            "download_url": "",
            "checked_at": time.time(),
        }
        cache_file.write_text(json.dumps(cached_data))

        mock_settings = MagicMock()
        mock_settings.update_check_ttl = 86400  # 24h — cache will be fresh

        with (
            patch("ember_code.core.utils.update_checker.CACHE_FILE", cache_file),
            patch("ember_code.core.utils.update_checker.__version__", "0.1.0"),
        ):
            # Should NOT hit the network — cache is fresh
            info = await check_for_update(settings=mock_settings)
            assert info.available is True
            assert info.latest_version == "0.3.0"
