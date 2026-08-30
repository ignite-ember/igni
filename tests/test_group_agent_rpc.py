"""What the app is told about the group, and how a person answers it.

Two things the FE needs and could not get. Which group somebody is in —
they should not have to guess why their agents changed — and the
questions the sync could not answer alone, with a way to answer them.

The answer has to land in the *running* session. A person who takes the
group's version of an agent and then finds the old one still answering
would reasonably conclude the button did nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.server_panels import PanelsController
from ember_code.core.config.group_policy import (
    GroupPolicyCache,
    GroupPolicyEntry,
    GroupPolicyPack,
)
from ember_code.core.init.group_agent_sync import GroupAgentSync


def _pack(*, agents: dict[str, str]) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime.now(timezone.utc),
        default_model="legal-reviewer",
        entries=[
            GroupPolicyEntry(
                kind="agents",
                entry_name=name,
                content=f"---\nname: {name}\ndescription: d\n---\n{body}\n",
                content_type="markdown",
            )
            for name, body in agents.items()
        ],
    )


@pytest.fixture
def session(tmp_path: Path, monkeypatch):
    """A stub with the two things the panel actually reaches for."""
    project = tmp_path / "project"
    (project / ".ember").mkdir(parents=True)
    cache_dir = tmp_path / "home" / "group-policy"

    # PanelsController reads pack metadata through a default-constructed
    # cache, which points at the real home directory.
    monkeypatch.setattr(GroupPolicyCache, "__init__", _pinned_cache_init(cache_dir))

    def _sync(kind: str = "agents") -> GroupAgentSync:
        return GroupAgentSync(
            project_dir=project,
            source_dir=cache_dir / kind,
            kind=kind,
        )

    stub = MagicMock()
    stub.project_dir = project
    stub.group_agent_sync.side_effect = _sync
    stub.group_conflicts.side_effect = lambda: _sync().pending_all()
    stub.resolve_group_conflict.side_effect = lambda kind, name, accept: _sync(kind).resolve(
        name, accept_incoming=accept
    )
    stub.reload_group_agents.return_value = True
    stub._cache_dir = cache_dir
    return stub


def _pinned_cache_init(cache_dir: Path):
    original = GroupPolicyCache.__init__

    def _init(self, cache_dir_arg=None, installer=None, data_dir=None):  # noqa: ANN001
        original(self, cache_dir=cache_dir_arg or cache_dir, installer=installer, data_dir=data_dir)

    return _init


def _materialize(session, pack: GroupPolicyPack) -> None:
    GroupPolicyCache(session._cache_dir).materialize(pack)


class TestWhatTheAppIsTold:
    def test_it_names_the_group(self, session):
        """So a person can see why their agents look like that."""
        _materialize(session, _pack(agents={"contract-review": "Read it."}))

        result = PanelsController(session).group_policy()

        assert result.group_name == "Legal"

    def test_it_carries_the_model(self, session):
        _materialize(session, _pack(agents={"x": "y"}))

        result = PanelsController(session).group_policy()

        assert result.default_model == "legal-reviewer"

    def test_no_group_is_not_an_error(self, session):
        result = PanelsController(session).group_policy()

        assert result.group_name is None
        assert result.pending_conflicts == []

    def test_a_quiet_sync_asks_nothing(self, session):
        """The common case, and it must stay silent."""
        _materialize(session, _pack(agents={"reviewer": "Review it."}))
        session.group_agent_sync().run()

        assert PanelsController(session).group_policy().pending_conflicts == []


class TestTheQuestionsItRaises:
    def _conflicted(self, session) -> None:
        _materialize(session, _pack(agents={"reviewer": "Review it."}))
        session.group_agent_sync().run()
        (session.project_dir / ".ember" / "agents" / "reviewer.md").write_text(
            "MY VERSION", encoding="utf-8"
        )
        _materialize(session, _pack(agents={"reviewer": "Review it carefully."}))
        session.group_agent_sync().run()

    def test_an_edited_agent_the_group_changed_is_surfaced(self, session):
        self._conflicted(session)

        conflicts = PanelsController(session).group_policy().pending_conflicts

        assert [c.entry_name for c in conflicts] == ["reviewer"]
        assert conflicts[0].kind == "changed"

    def test_the_question_says_which_way_round_it_is(self, session):
        """ "Take the group's version" and "the group dropped this" are
        different questions; the FE should not have to work out which."""
        self._conflicted(session)

        question = PanelsController(session).group_policy().pending_conflicts[0].question

        assert "reviewer" in question
        assert "?" in question

    def test_taking_the_group_version_applies_it(self, session):
        self._conflicted(session)

        result = PanelsController(session).resolve_group_agent_conflict(
            entry_name="reviewer", accept_incoming=True
        )

        assert result.resolved
        local = (session.project_dir / ".ember" / "agents" / "reviewer.md").read_text(
            encoding="utf-8"
        )
        assert "carefully" in local

    def test_answering_rebuilds_the_running_session(self, session):
        """Otherwise the button appears to do nothing until a restart."""
        self._conflicted(session)

        result = PanelsController(session).resolve_group_agent_conflict(
            entry_name="reviewer", accept_incoming=True
        )

        assert result.reloaded
        session.reload_group_agents.assert_called_once()

    def test_keeping_mine_changes_no_files_and_rebuilds_nothing(self, session):
        self._conflicted(session)

        result = PanelsController(session).resolve_group_agent_conflict(
            entry_name="reviewer", accept_incoming=False
        )

        assert not result.reloaded
        session.reload_group_agents.assert_not_called()
        local = (session.project_dir / ".ember" / "agents" / "reviewer.md").read_text(
            encoding="utf-8"
        )
        assert local == "MY VERSION"

    def test_the_question_is_gone_afterwards(self, session):
        self._conflicted(session)

        PanelsController(session).resolve_group_agent_conflict(
            entry_name="reviewer", accept_incoming=False
        )

        assert PanelsController(session).group_policy().pending_conflicts == []


class TestWhenTheAdminMovesSomebody:
    @pytest.mark.asyncio
    async def test_a_refreshed_pack_reloads_the_agents(self, tmp_path: Path):
        """An admin moving a person from engineering to legal should
        change what they have, not tell them to restart."""
        from ember_code.backend.server_auth import AuthController

        ctrl = AuthController.__new__(AuthController)
        ctrl._settings = SimpleNamespace(
            api_url="https://api.example.invalid",
            storage=SimpleNamespace(data_dir=str(tmp_path)),
            auth=SimpleNamespace(credentials_file=str(tmp_path / "credentials.json")),
        )
        ctrl._portal = MagicMock()
        ctrl._portal.fetch_group_pack = AsyncMock(return_value=_pack(agents={"x": "y"}))
        ctrl._hydration_lock = None
        ctrl._status_provider = MagicMock()
        ctrl._session = MagicMock()
        ctrl._session.reload_group_agents.return_value = True

        assert await ctrl._hydrate_group_policy(token="t-1") is True
        ctrl._session.reload_group_agents.assert_called_once()
