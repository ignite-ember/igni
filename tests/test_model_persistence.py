"""Tests for model-selection persistence across app restarts.

Two layers cooperate:

* ``save_default_model`` — writes to ``~/.ember/config.yaml`` so a
  fresh session opened after restart loads the chosen model.
* ``SessionPreferencesStore`` — writes to ``state.db`` keyed by
  ``session_id`` so ``--continue`` restores the model the user was
  using in that specific session, even if the user has since picked
  a different default for new sessions.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from ember_code.backend.server import BackendServer
from ember_code.core.code_index.paths import state_db_path
from ember_code.core.config.settings import Settings, save_default_model
from ember_code.core.session.session_preferences import SessionPreferencesStore


class TestSaveDefaultModel:
    """``save_default_model`` round-trips through ``~/.ember/config.yaml``."""

    def test_writes_default_to_user_config(self, tmp_path, monkeypatch):
        """Setting a default writes a minimal ``models.default`` key
        and nothing else — no synthetic registry, no clobber of
        unrelated keys."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        save_default_model("MiniMax-M2.7")

        cfg_path = tmp_path / ".ember" / "config.yaml"
        assert cfg_path.exists()
        cfg = yaml.safe_load(cfg_path.read_text())
        assert cfg == {"models": {"default": "MiniMax-M2.7"}}

    def test_preserves_unrelated_keys(self, tmp_path, monkeypatch):
        """Writing the default must not clobber other top-level keys
        the user has set (e.g. ``permissions``, ``display``)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg_dir = tmp_path / ".ember"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "display": {"color_theme": "dark"},
                    "models": {"max_context_window": 200_000},
                }
            )
        )

        save_default_model("gpt-7")

        cfg = yaml.safe_load((cfg_dir / "config.yaml").read_text())
        assert cfg["display"] == {"color_theme": "dark"}
        # Original key in the models block survives — only ``default``
        # gets added.
        assert cfg["models"]["max_context_window"] == 200_000
        assert cfg["models"]["default"] == "gpt-7"

    def test_overwrites_existing_default(self, tmp_path, monkeypatch):
        """A second call replaces the previous default — no append,
        no list."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        save_default_model("first")
        save_default_model("second")

        cfg = yaml.safe_load((tmp_path / ".ember" / "config.yaml").read_text())
        assert cfg["models"]["default"] == "second"

    def test_recovers_from_corrupt_models_block(self, tmp_path, monkeypatch):
        """If ``models`` exists but isn't a dict (e.g. someone wrote
        a list by hand), the helper must overwrite it rather than
        crash. The previous bad value is gone, which is acceptable —
        the alternative is the next launch failing to load config."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg_dir = tmp_path / ".ember"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.yaml").write_text(yaml.safe_dump({"models": ["bogus", "list"]}))

        save_default_model("MiniMax-M2.7")

        cfg = yaml.safe_load((cfg_dir / "config.yaml").read_text())
        assert cfg["models"] == {"default": "MiniMax-M2.7"}


class TestSessionPreferencesStore:
    """Per-session model preference round-trips through SQLite."""

    def test_set_then_get_returns_model(self, tmp_path):
        """Basic upsert + read."""
        store = SessionPreferencesStore(tmp_path / "state.db")
        store.set_model("sess-a", "MiniMax-M2.7")
        assert store.get_model("sess-a") == "MiniMax-M2.7"

    def test_get_unknown_session_returns_none(self, tmp_path):
        """No row → ``None``, not an error."""
        store = SessionPreferencesStore(tmp_path / "state.db")
        assert store.get_model("never-set") is None

    def test_set_is_per_session(self, tmp_path):
        """Two sessions don't share preferences."""
        store = SessionPreferencesStore(tmp_path / "state.db")
        store.set_model("sess-a", "model-a")
        store.set_model("sess-b", "model-b")
        assert store.get_model("sess-a") == "model-a"
        assert store.get_model("sess-b") == "model-b"

    def test_set_upserts(self, tmp_path):
        """Repeat ``set_model`` overwrites — no duplicate rows, no
        history."""
        store = SessionPreferencesStore(tmp_path / "state.db")
        store.set_model("sess", "old")
        store.set_model("sess", "new")
        assert store.get_model("sess") == "new"

    def test_survives_reopen(self, tmp_path):
        """Closing and reopening the store yields the same value —
        i.e. the data is genuinely on disk, not just in memory."""
        db_path = tmp_path / "state.db"
        SessionPreferencesStore(db_path).set_model("sess", "MiniMax-M2.7")
        # Fresh instance — new connection, no shared cache.
        assert SessionPreferencesStore(db_path).get_model("sess") == "MiniMax-M2.7"

    @pytest.mark.asyncio
    async def test_async_wrappers(self, tmp_path):
        store = SessionPreferencesStore(tmp_path / "state.db")
        await store.aset_model("sess", "MiniMax-M2.7")
        assert await store.aget_model("sess") == "MiniMax-M2.7"


class TestBackendServerResumeOverride:
    """On ``--continue``, ``BackendServer.__init__`` must consult the
    per-session store and override ``settings.models.default`` BEFORE
    ``Session.__init__`` builds the main team."""

    @staticmethod
    def _make_settings(tmp_path, registry):
        settings = Settings()
        settings.storage.data_dir = str(tmp_path / "ember")
        settings.models.default = "user-level-default"
        settings.models.registry = registry
        return settings

    @staticmethod
    def _construct_server(settings, project_dir, captured, **kwargs):
        """Run ``BackendServer.__init__`` with Session mocked.

        Records the value of ``settings.models.default`` at the
        moment Session is instantiated — that's the only thing
        these tests care about. The returned mock has a real
        ``project_dir`` so the downstream PendingMessageStore call
        succeeds.
        """

        def fake_session(*args, **kw):
            captured["default"] = settings.models.default
            m = MagicMock()
            m.project_dir = project_dir
            return m

        with patch("ember_code.core.session.Session", side_effect=fake_session):
            BackendServer(settings, project_dir=project_dir, **kwargs)

    def test_resume_overrides_default_with_persisted_choice(self, tmp_path):
        """The model the user was on last time wins over the
        user-level default."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = state_db_path(project_dir, data_dir=str(tmp_path / "ember"))
        SessionPreferencesStore(db_path).set_model("resumed-sess", "per-session-model")

        settings = self._make_settings(
            tmp_path,
            {
                "user-level-default": {"url": "x", "model_id": "x"},
                "per-session-model": {"url": "y", "model_id": "y"},
            },
        )

        captured: dict = {}
        self._construct_server(
            settings,
            project_dir,
            captured,
            resume_session_id="resumed-sess",
        )
        assert captured["default"] == "per-session-model"

    def test_resume_falls_back_when_persisted_model_missing_from_registry(self, tmp_path):
        """Stale row pointing at a model no longer in the registry
        must NOT override — the user would get a broken team."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = state_db_path(project_dir, data_dir=str(tmp_path / "ember"))
        SessionPreferencesStore(db_path).set_model("resumed-sess", "deleted-model")

        settings = self._make_settings(
            tmp_path,
            {"user-level-default": {"url": "x", "model_id": "x"}},
        )

        captured: dict = {}
        self._construct_server(
            settings,
            project_dir,
            captured,
            resume_session_id="resumed-sess",
        )
        assert captured["default"] == "user-level-default"

    def test_fresh_session_does_not_consult_store(self, tmp_path):
        """No ``resume_session_id`` → no per-session lookup. The
        user-level default stays in place even if some other
        session in the same store has a different model."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = state_db_path(project_dir, data_dir=str(tmp_path / "ember"))
        SessionPreferencesStore(db_path).set_model("some-old-sess", "leftover-model")

        settings = self._make_settings(
            tmp_path,
            {
                "user-level-default": {"url": "x", "model_id": "x"},
                "leftover-model": {"url": "y", "model_id": "y"},
            },
        )

        captured: dict = {}
        self._construct_server(settings, project_dir, captured)
        assert captured["default"] == "user-level-default"
