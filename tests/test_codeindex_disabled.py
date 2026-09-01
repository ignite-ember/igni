"""Turning CodeIndex off, from the server, without paying for Neo4j.

A user who does not read code should not carry a graph database. The
index is Neo4j-backed, and attaching it downloads a ~200MB distribution
plus a JDK on first use and leaves a server process refcounted across
sessions — so "disabled" has to mean none of that happens, not merely
that the tool is hidden from the model.

The switch is ``code_index.enabled``, and it is deliberately one flag
feeding the flag that already existed. ``_codeindex_available`` already
gated three things — whether the ``CodeIndex`` tool is offered, whether
the prompt is the CodeIndex-first variant, and which variant of every
agent definition the pool loads — so forcing it off gets all three for
free. What needed adding was everything upstream of the model: the
sidecar attach, the warmup phase, and the refresher that would otherwise
turn it back on.

Server-settable because a group's ``settings`` entry is deep-merged into
config above CLI flags *and* project files, so an admin can decide this
for a group and a project cannot opt back in.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ember_code.core.config.settings import Settings, load_settings
from ember_code.core.paths import CONFIG_DIR

#: Enough of a model registry for a Session to construct.
_STUB_MODEL: dict[str, Any] = {
    "default": "stub",
    "registry": {
        "stub": {
            "provider": "openai_like",
            "model_id": "stub-1",
            "url": "http://localhost:9/v1",
            "api_key": "x",
            "context_window": 8192,
        }
    },
}


def _repo(tmp_path: Path) -> Path:
    """A git repo with one commit, so HEAD resolves."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def _settings(project: Path, *, enabled: bool, home: Path) -> Settings:
    config = project / CONFIG_DIR
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "code_index": {"enabled": enabled},
                "models": _STUB_MODEL,
                "storage": {"data_dir": str(home)},
            }
        )
    )
    return load_settings(project_dir=project)


def _claim_head_is_indexed(session: Any) -> str:
    """Record HEAD as indexed, so ``has_commit(HEAD)`` answers True.

    Through ``ManifestState.set_head`` — which auto-upserts a missing
    commit — rather than writing the JSON by hand. The first attempt did
    hand-write it and silently failed to load: the on-disk ``commits`` is
    a *list* of ``CommitInfo`` while the in-memory state keeps a dict.

    Without this the control case is False because nothing is indexed,
    and the test cannot tell a working switch from a missing index.
    """
    head = session.code_index_sync.current_sha()
    assert head, "the fixture repo should have a HEAD"

    from ember_code.core.code_index.schema.manifest import SystemClock

    store = session.code_index.manifest
    state = store.load()
    state.set_head(head, clock=SystemClock())
    store.save(state)
    return head


class TestTheFlagReachesTheAgent:
    def test_disabled_keeps_codeindex_out_of_the_tool_list(self, tmp_path: Path):
        from ember_code.core.config.tool_permissions import ToolPermissions
        from ember_code.core.session.core import Session
        from ember_code.core.tools.registry import ToolRegistry

        project = _repo(tmp_path / "proj")
        settings = _settings(project, enabled=False, home=tmp_path / "home")

        session = Session(settings=settings, project_dir=project)
        _claim_head_is_indexed(session)
        # Recompute the way a session would after a sync — and it must
        # still be off, because the manifest now says HEAD is indexed.
        session.refresh_codeindex_availability()

        registry = ToolRegistry(base_dir=project, permissions=ToolPermissions(project_dir=project))

        assert session._codeindex_available is False
        assert "CodeIndex" not in session.resolve_main_tool_names(registry)

    def test_enabled_with_an_indexed_head_does_offer_it(self, tmp_path: Path):
        """The control that makes the test above mean something.

        Same repo, same forged manifest, flag on — CodeIndex appears. If
        this did not pass, the assertion above would be satisfied by an
        index that simply was not there.
        """
        from ember_code.core.config.tool_permissions import ToolPermissions
        from ember_code.core.session.core import Session
        from ember_code.core.tools.registry import ToolRegistry

        project = _repo(tmp_path / "proj")
        settings = _settings(project, enabled=True, home=tmp_path / "home")

        session = Session(settings=settings, project_dir=project)
        _claim_head_is_indexed(session)
        session.refresh_codeindex_availability()

        registry = ToolRegistry(base_dir=project, permissions=ToolPermissions(project_dir=project))

        assert session._codeindex_available is True
        assert "CodeIndex" in session.resolve_main_tool_names(registry)

    def test_an_already_indexed_repo_is_off_from_construction(self, tmp_path: Path):
        """The common case, and the one the other tests could not see.

        A real user has synced before, so the manifest already lists HEAD
        when the Session is built — ``_init_codeindex`` computes
        availability there, long before any refresh runs. Without the
        gate in the constructor, that user is offered CodeIndex and the
        CodeIndex-first prompt for the whole session and only a later
        refresh would take it away.

        Built by pointing two sessions at one home: the first (enabled)
        writes the manifest, the second (disabled) inherits it.
        """
        from ember_code.core.config.tool_permissions import ToolPermissions
        from ember_code.core.session.core import Session
        from ember_code.core.tools.registry import ToolRegistry

        project = _repo(tmp_path / "proj")
        home = tmp_path / "home"

        warm = Session(settings=_settings(project, enabled=True, home=home), project_dir=project)
        _claim_head_is_indexed(warm)
        assert warm._codeindex_available is False, (
            "not yet — the flag was computed before the write"
        )

        # Same project, same home, index already on disk. Nothing is
        # refreshed: this is construction only.
        cold = Session(settings=_settings(project, enabled=False, home=home), project_dir=project)

        registry = ToolRegistry(base_dir=project, permissions=ToolPermissions(project_dir=project))

        assert cold._codeindex_available is False
        assert "CodeIndex" not in cold.resolve_main_tool_names(registry)

    def test_the_control_for_that_case(self, tmp_path: Path):
        """Same two-session setup with the flag left on — availability is
        True at construction. Without this, the test above would pass on
        a manifest that never loaded."""
        from ember_code.core.session.core import Session

        project = _repo(tmp_path / "proj")
        home = tmp_path / "home"

        warm = Session(settings=_settings(project, enabled=True, home=home), project_dir=project)
        _claim_head_is_indexed(warm)

        second = Session(settings=_settings(project, enabled=True, home=home), project_dir=project)

        assert second._codeindex_available is True

    def test_the_refresher_cannot_turn_it_back_on(self, tmp_path: Path):
        """The way the switch would have come undone.

        The session starts with the flag off; a background sync finishes;
        the refresher finds an indexed commit and flips availability —
        bringing back the tool, the CodeIndex-first prompt and the
        CodeIndex agent variants for somebody an admin turned it off for.
        """
        from ember_code.core.session.core import Session

        project = _repo(tmp_path / "proj")
        settings = _settings(project, enabled=False, home=tmp_path / "home")

        session = Session(settings=settings, project_dir=project)
        _claim_head_is_indexed(session)

        result = session.refresh_codeindex_availability()

        assert result.ok is True
        assert result.changed is False
        assert session._codeindex_available is False


class TestTheSidecarIsNotAttached:
    """The part that is actually expensive.

    ``attach_neo4j`` is what downloads the distribution and spawns the
    server, so a switch that hid the tool but still attached would save
    nothing at all.
    """

    @pytest.fixture
    def orchestrator(self):
        from ember_code.backend.session_orchestrator import SessionOrchestrator

        return SessionOrchestrator

    async def _attach(self, monkeypatch, *, code_index: bool, knowledge: bool):
        """Call ``attach_neo4j`` with the two flags set, recording whether
        the runtime was constructed at all."""
        from ember_code.backend import neo4j_runtime as runtime_module
        from ember_code.backend import session_orchestrator as module

        constructed: list[bool] = []

        class _Boom:
            def __init__(self, *a, **k):
                constructed.append(True)
                raise AssertionError("the Neo4j runtime must not be constructed")

        monkeypatch.setattr(runtime_module, "Neo4jRuntime", _Boom)
        monkeypatch.delenv("EMBER_NEO4J_DISABLED", raising=False)

        settings = Settings()
        object.__setattr__(settings.code_index, "enabled", code_index)
        object.__setattr__(settings.knowledge, "enabled", knowledge)

        orchestrator = module.SessionOrchestrator.__new__(module.SessionOrchestrator)
        orchestrator._settings = settings
        orchestrator._neo4j_runtime = None
        orchestrator._backend = None

        return await orchestrator.attach_neo4j(), constructed

    async def test_both_disabled_means_no_runtime_at_all(self, monkeypatch):
        runtime, constructed = await self._attach(monkeypatch, code_index=False, knowledge=False)

        assert runtime is None
        assert constructed == [], "no download, no process, no refcount"

    @pytest.mark.parametrize(
        ("code_index", "knowledge"),
        [(True, False), (False, True), (True, True)],
    )
    async def test_either_feature_still_gets_the_runtime(self, monkeypatch, code_index, knowledge):
        """Knowledge is backed by the same sidecar, and its Chroma
        fallback was removed when the index moved to Neo4j — so
        disabling CodeIndex must not take knowledge down with it.

        The stub runtime raises, so reaching construction is the
        observable signal that the attach was attempted.
        """
        from ember_code.backend import neo4j_runtime as runtime_module
        from ember_code.backend import session_orchestrator as module

        monkeypatch.delenv("EMBER_NEO4J_DISABLED", raising=False)

        attempted: list[bool] = []

        class _Boom:
            def __init__(self, *a, **k):
                attempted.append(True)
                raise RuntimeError("no java here")

        monkeypatch.setattr(runtime_module, "Neo4jRuntime", _Boom)

        settings = Settings()
        object.__setattr__(settings.code_index, "enabled", code_index)
        object.__setattr__(settings.knowledge, "enabled", knowledge)

        orchestrator = module.SessionOrchestrator.__new__(module.SessionOrchestrator)
        orchestrator._settings = settings
        orchestrator._neo4j_runtime = None
        orchestrator._backend = None

        # Degrades rather than raising — a missing JDK must not stop the
        # backend booting.
        assert await orchestrator.attach_neo4j() is None
        assert attempted == [True], "the runtime should have been attempted"


class TestTheWarmupIsSkipped:
    """The cloud resolve, the changeset sync and the HEAD watcher.

    None of it is free, and all of it is for an index the session has
    been told not to have.
    """

    def test_disabled_skips_the_whole_sequence(self, tmp_path: Path):
        from ember_code.core.session.startup.codeindex import CodeIndexWarmupPhase

        scheduled: list[object] = []

        class _Session:
            def __init__(self, enabled: bool):
                self.settings = Settings()
                object.__setattr__(self.settings.code_index, "enabled", enabled)

        phase = CodeIndexWarmupPhase(_Session(enabled=False))
        phase._schedule_on_loop = lambda fn: scheduled.append(fn)  # type: ignore[method-assign]
        phase.start_background()

        assert scheduled == []

    def test_enabled_schedules_it(self, tmp_path: Path):
        from ember_code.core.session.startup.codeindex import CodeIndexWarmupPhase

        scheduled: list[object] = []

        class _Session:
            def __init__(self, enabled: bool):
                self.settings = Settings()
                object.__setattr__(self.settings.code_index, "enabled", enabled)

        phase = CodeIndexWarmupPhase(_Session(enabled=True))
        phase._schedule_on_loop = lambda fn: scheduled.append(fn)  # type: ignore[method-assign]
        phase.start_background()

        assert len(scheduled) == 1


class TestTheServerChannel:
    def test_a_group_settings_entry_turns_it_off(self, tmp_path: Path):
        """The shape an admin actually writes, through the tier that
        carries it: ``{"code_index": {"enabled": false}}``."""
        from ember_code.core.config.group_policy import GroupPolicyEntry, GroupPolicyPack

        pack = GroupPolicyPack(
            group_id="g-1",
            group_name="Contractors",
            fetched_at=0.0,
            entries=[
                GroupPolicyEntry(
                    kind="settings",
                    entry_name="no-codeindex",
                    content=json.dumps({"code_index": {"enabled": False}}),
                    content_type="json",
                    enabled=True,
                )
            ],
        )

        assert pack.to_settings_dict() == {"code_index": {"enabled": False}}

    def test_the_group_beats_the_project(self):
        """An admin's decision has to survive a project trying to undo
        it, which is why this rides the settings tier rather than a
        loader.

        Asserted through the tier itself rather than by assembling the
        whole plan: ``GroupPolicyTier`` runs after every project tier in
        ``SettingsMergePlan.default`` — the ordering there is the
        precedence contract — so applying it over an accumulator that
        already carries the project's value is the question that matters.
        """
        from ember_code.core.config.accumulator import SettingsAccumulator
        from ember_code.core.config.group_policy import GroupPolicyEntry, GroupPolicyPack
        from ember_code.core.config.merge_plan import GroupPolicyTier

        # The project says yes.
        accumulator = SettingsAccumulator.from_defaults({"code_index": {"enabled": True}})
        assert accumulator.payload["code_index"]["enabled"] is True

        # The group says no, and runs later.
        pack = GroupPolicyPack(
            group_id="g-1",
            group_name="Contractors",
            fetched_at=0.0,
            entries=[
                GroupPolicyEntry(
                    kind="settings",
                    entry_name="no-codeindex",
                    content=json.dumps({"code_index": {"enabled": False}}),
                    content_type="json",
                    enabled=True,
                )
            ],
        )
        result = GroupPolicyTier(fetcher=lambda: pack).apply(accumulator)

        assert result.payload["code_index"]["enabled"] is False

    def test_the_group_tier_runs_after_every_project_tier(self):
        """The ordering that makes the test above load-bearing.

        Asserted by building the plan and reading the tier order, not by
        searching the source of ``default()``. The first version did the
        latter and broke the moment a *comment* in that function mentioned
        ``GroupPolicyTier`` — a text search cannot tell code from prose,
        and it failed pointing at a real ordering that had not changed.
        """
        from ember_code.core.config.accumulator import SettingsAccumulator
        from ember_code.core.config.merge_plan import (
            GroupPolicyTier,
            JsonFragmentTier,
            ManagedTier,
            SettingsMergePlan,
            YamlTier,
        )
        from ember_code.core.config.schemas.models import ModelsConfig
        from ember_code.core.config.settings import Settings

        plan = SettingsMergePlan.default(
            project_dir=Path("/tmp/does-not-matter"),
            cli=None,
            accumulator=SettingsAccumulator.from_defaults({}),
            settings_cls=Settings,
            defaults_models=ModelsConfig(),
            managed_path_provider=lambda: None,
            group_policy_fetcher=lambda: None,
        )
        # _tiers rather than a public accessor: the ordering is the
        # contract this test exists for, and there is no other way to read
        # it without re-running the whole merge.
        order = [type(tier) for tier in plan._tiers]

        group_at = order.index(GroupPolicyTier)
        file_tiers = [
            index for index, tier in enumerate(order) if tier in (YamlTier, JsonFragmentTier)
        ]

        assert file_tiers, "no file-based tiers found — the plan shape changed"
        assert max(file_tiers) < group_at, "a project file now outranks group policy"
        # And the sysadmin file still outranks the group.
        assert group_at < order.index(ManagedTier)

    def test_the_default_is_on(self):
        """Nobody who has not asked for this should notice it exists."""
        assert Settings().code_index.enabled is True
