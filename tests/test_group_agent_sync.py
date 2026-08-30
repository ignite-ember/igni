"""The group's agents, copied into a project the person can edit.

The server is where a team's agents are written, but somebody still has
to be able to open one and change a line — and an admin editing the same
agent must not silently overwrite that. So the two rules that matter:

* Nobody touched their copy → take the server's, say nothing.
* Somebody edited their copy → keep it, and ask.

Everything below is one of those two, or the thing that makes a group
*switch* work: agents the new group does not ship have to go, or moving
a person from engineering to legal would leave them holding both.

The dangerous outcome is not a missed update — it is deleting or
overwriting somebody's work, so most of these check what stayed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ember_code.core.init.checksum_store import ChecksumStore
from ember_code.core.init.group_agent_sync import CONFLICTS_FILE, GroupAgentSync
from ember_code.core.init.schemas import InitConfig


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "project" / ".ember").mkdir(parents=True)
    return tmp_path / "project"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    (tmp_path / "group-policy" / "agents").mkdir(parents=True)
    return tmp_path / "group-policy" / "agents"


def _agent(directory: Path, name: str, body: str) -> Path:
    path = directory / f"{name}.md"
    path.write_text(f"---\nname: {name}\ndescription: d\n---\n{body}\n", encoding="utf-8")
    return path


def _sync(project: Path, source: Path) -> GroupAgentSync:
    return GroupAgentSync(project_dir=project, source_dir=source, config=InitConfig())


def _local(project: Path, name: str) -> Path:
    return project / ".ember" / "agents" / f"{name}.md"


class TestFirstSync:
    def test_the_group_agents_arrive_in_the_project(self, project: Path, source: Path):
        _agent(source, "reviewer", "Review it.")

        report = _sync(project, source).run()

        assert report.copied == ["reviewer"]
        assert "Review it." in _local(project, "reviewer").read_text(encoding="utf-8")

    def test_nothing_to_sync_is_not_an_error(self, project: Path, tmp_path: Path):
        """No group, or a group with no agents — the common case for
        somebody who has not logged in."""
        report = GroupAgentSync(
            project_dir=project, source_dir=tmp_path / "nope", config=InitConfig()
        ).run()

        assert not report.changed_anything

    def test_running_twice_changes_nothing(self, project: Path, source: Path):
        _agent(source, "reviewer", "Review it.")
        _sync(project, source).run()

        report = _sync(project, source).run()

        assert report.unchanged == ["reviewer"]
        assert not report.changed_anything


class TestWhenNobodyTouchedIt:
    def test_a_server_change_is_taken_without_asking(self, project: Path, source: Path):
        """The common case. It must be silent."""
        _agent(source, "reviewer", "Review it.")
        _sync(project, source).run()
        _agent(source, "reviewer", "Review it carefully.")

        report = _sync(project, source).run()

        assert report.updated == ["reviewer"]
        assert report.conflicts == []
        assert "carefully" in _local(project, "reviewer").read_text(encoding="utf-8")

    def test_an_agent_dropped_by_the_group_is_removed(self, project: Path, source: Path):
        """Which is what makes a group switch a switch."""
        _agent(source, "reviewer", "Review it.")
        _sync(project, source).run()
        (source / "reviewer.md").unlink()

        report = _sync(project, source).run()

        assert report.removed == ["reviewer"]
        assert not _local(project, "reviewer").exists()

    def test_a_whole_group_switch_swaps_the_set(self, project: Path, source: Path):
        _agent(source, "reviewer", "Review it.")
        _agent(source, "debugger", "Debug it.")
        _sync(project, source).run()

        (source / "reviewer.md").unlink()
        (source / "debugger.md").unlink()
        _agent(source, "contract-review", "Read the contract.")
        _sync(project, source).run()

        present = {p.stem for p in (project / ".ember" / "agents").glob("*.md")}
        assert present == {"contract-review"}


class TestWhenSomebodyEditedIt:
    def test_their_edit_is_not_overwritten(self, project: Path, source: Path):
        _agent(source, "reviewer", "Review it.")
        _sync(project, source).run()
        _local(project, "reviewer").write_text("MY VERSION", encoding="utf-8")
        _agent(source, "reviewer", "Review it carefully.")

        _sync(project, source).run()

        assert _local(project, "reviewer").read_text(encoding="utf-8") == "MY VERSION"

    def test_the_question_is_raised(self, project: Path, source: Path):
        _agent(source, "reviewer", "Review it.")
        _sync(project, source).run()
        _local(project, "reviewer").write_text("MY VERSION", encoding="utf-8")
        _agent(source, "reviewer", "Review it carefully.")

        report = _sync(project, source).run()

        assert [c.entry_name for c in report.conflicts] == ["reviewer"]
        assert report.conflicts[0].change == "changed"

    def test_the_incoming_version_is_parked_where_it_can_be_read(self, project: Path, source: Path):
        """Answering "which one" is easier next to the file than in the
        abstract."""
        _agent(source, "reviewer", "Review it.")
        _sync(project, source).run()
        _local(project, "reviewer").write_text("MY VERSION", encoding="utf-8")
        _agent(source, "reviewer", "Review it carefully.")

        _sync(project, source).run()

        parked = project / ".ember" / "agents" / "reviewer.md.incoming"
        assert "carefully" in parked.read_text(encoding="utf-8")

    def test_an_unanswered_question_survives_a_restart(self, project: Path, source: Path):
        _agent(source, "reviewer", "Review it.")
        _sync(project, source).run()
        _local(project, "reviewer").write_text("MY VERSION", encoding="utf-8")
        _agent(source, "reviewer", "Review it carefully.")
        _sync(project, source).run()

        assert [c.entry_name for c in _sync(project, source).pending()] == ["reviewer"]

    def test_an_edited_agent_the_group_dropped_is_kept(self, project: Path, source: Path):
        """Deleting somebody's work because an admin moved them to
        another team would be indefensible."""
        _agent(source, "reviewer", "Review it.")
        _sync(project, source).run()
        _local(project, "reviewer").write_text("MY VERSION", encoding="utf-8")
        (source / "reviewer.md").unlink()

        report = _sync(project, source).run()

        assert _local(project, "reviewer").exists()
        assert [c.change for c in report.conflicts] == ["removed"]

    def test_a_file_we_never_put_there_is_not_adopted(self, project: Path, source: Path):
        """Somebody's own reviewer.md predating the group is theirs."""
        (project / ".ember" / "agents").mkdir(parents=True, exist_ok=True)
        _local(project, "reviewer").write_text("MINE ALL ALONG", encoding="utf-8")
        _agent(source, "reviewer", "Review it.")

        report = _sync(project, source).run()

        assert _local(project, "reviewer").read_text(encoding="utf-8") == "MINE ALL ALONG"
        assert [c.entry_name for c in report.conflicts] == ["reviewer"]

    def test_an_identical_untracked_file_is_no_argument(self, project: Path, source: Path):
        """The handover from the bundled copy: same bytes, nothing to
        ask about."""
        (project / ".ember" / "agents").mkdir(parents=True, exist_ok=True)
        src = _agent(source, "reviewer", "Review it.")
        _local(project, "reviewer").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

        report = _sync(project, source).run()

        assert report.conflicts == []
        assert report.unchanged == ["reviewer"]


class TestAnsweringIt:
    def _conflicted(self, project: Path, source: Path) -> GroupAgentSync:
        _agent(source, "reviewer", "Review it.")
        _sync(project, source).run()
        _local(project, "reviewer").write_text("MY VERSION", encoding="utf-8")
        _agent(source, "reviewer", "Review it carefully.")
        _sync(project, source).run()
        return _sync(project, source)

    def test_taking_the_group_version_replaces_the_file(self, project: Path, source: Path):
        sync = self._conflicted(project, source)

        assert sync.resolve("reviewer", accept_incoming=True)

        assert "carefully" in _local(project, "reviewer").read_text(encoding="utf-8")
        assert sync.pending() == []

    def test_keeping_mine_leaves_the_file_alone(self, project: Path, source: Path):
        sync = self._conflicted(project, source)

        sync.resolve("reviewer", accept_incoming=False)

        assert _local(project, "reviewer").read_text(encoding="utf-8") == "MY VERSION"
        assert sync.pending() == []

    def test_keeping_mine_is_not_asked_again(self, project: Path, source: Path):
        """ "Keep mine" means until the group changes the agent *again* —
        not a question on every start."""
        sync = self._conflicted(project, source)
        sync.resolve("reviewer", accept_incoming=False)

        report = _sync(project, source).run()

        assert report.conflicts == []
        assert _local(project, "reviewer").read_text(encoding="utf-8") == "MY VERSION"

    def test_but_the_next_server_change_asks_again(self, project: Path, source: Path):
        sync = self._conflicted(project, source)
        sync.resolve("reviewer", accept_incoming=False)
        _agent(source, "reviewer", "Review it very carefully indeed.")

        report = _sync(project, source).run()

        assert [c.entry_name for c in report.conflicts] == ["reviewer"]

    def test_the_parked_copy_is_cleaned_up(self, project: Path, source: Path):
        sync = self._conflicted(project, source)

        sync.resolve("reviewer", accept_incoming=True)

        assert not (project / ".ember" / "agents" / "reviewer.md.incoming").exists()

    def test_accepting_a_removal_deletes_the_file(self, project: Path, source: Path):
        _agent(source, "reviewer", "Review it.")
        _sync(project, source).run()
        _local(project, "reviewer").write_text("MY VERSION", encoding="utf-8")
        (source / "reviewer.md").unlink()
        _sync(project, source).run()

        _sync(project, source).resolve("reviewer", accept_incoming=True)

        assert not _local(project, "reviewer").exists()

    def test_answering_something_that_is_not_pending_does_nothing(
        self, project: Path, source: Path
    ):
        assert not _sync(project, source).resolve("nobody", accept_incoming=True)

    def test_an_unreadable_conflict_file_is_not_fatal(self, project: Path, source: Path):
        """Refusing to start over a corrupt bookkeeping file would be a
        poor trade."""
        (project / ".ember" / CONFLICTS_FILE).write_text("{not json", encoding="utf-8")

        assert _sync(project, source).pending() == []


class TestTheHandoverFromTheBundle:
    def test_an_untouched_bundled_agent_is_updated_cleanly(self, project: Path, source: Path):
        """The bundle and the group share a checksum key on purpose: a
        person who never edited their scaffolded copy should not be asked
        anything the day their org starts serving agents."""
        agents = project / ".ember" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        bundled = agents / "reviewer.md"
        bundled.write_text("---\nname: reviewer\ndescription: d\n---\nBundled.\n", encoding="utf-8")
        store = ChecksumStore.load(project, InitConfig())
        store.entries["agents/reviewer.md"] = ChecksumStore.file_hash(bundled)
        store.save()

        _agent(source, "reviewer", "From the group.")
        report = _sync(project, source).run()

        assert report.updated == ["reviewer"]
        assert "From the group." in bundled.read_text(encoding="utf-8")

    def test_the_checksum_file_is_shared_not_duplicated(self, project: Path, source: Path):
        _agent(source, "reviewer", "Review it.")
        _sync(project, source).run()

        entries = json.loads((project / ".ember" / ".checksums.json").read_text(encoding="utf-8"))
        assert "agents/reviewer.md" in entries


class TestEveryEditableKind:
    """The promise is not "your agents survive". It is that anything you
    opened and changed survives, and that you are asked rather than
    overwritten. A skill you tuned is as much your work as an agent.
    """

    CASES = [
        ("agents", "reviewer", "reviewer.md"),
        ("skills", "deploy", "deploy/SKILL.md"),
        ("commands", "ship", "ship.md"),
        ("rules", "style", "style.md"),
        ("output-styles", "terse", "terse.md"),
        ("workflows", "review", "review.mjs"),
    ]

    @staticmethod
    def _write_source(source_root: Path, kind: str, rel: str, body: str) -> None:
        path = source_root / kind / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def _sync(self, project: Path, source_root: Path, kind: str) -> GroupAgentSync:
        return GroupAgentSync(
            project_dir=project,
            source_dir=source_root / kind,
            config=InitConfig(),
            kind=kind,
        )

    @pytest.mark.parametrize("kind,name,rel", CASES)
    def test_it_arrives(self, project: Path, tmp_path: Path, kind: str, name: str, rel: str):
        source = tmp_path / "cache"
        self._write_source(source, kind, rel, "the group's version")

        report = self._sync(project, source, kind).run()

        assert report.copied == [name]

    @pytest.mark.parametrize("kind,name,rel", CASES)
    def test_an_edit_is_not_overwritten(self, project: Path, tmp_path: Path, kind, name, rel):
        source = tmp_path / "cache"
        self._write_source(source, kind, rel, "the group's version")
        sync = self._sync(project, source, kind)
        sync.run()

        local = project / ".ember" / kind / rel
        local.write_text("MY VERSION", encoding="utf-8")
        self._write_source(source, kind, rel, "the group's newer version")
        report = self._sync(project, source, kind).run()

        assert local.read_text(encoding="utf-8") == "MY VERSION"
        assert [c.entry_name for c in report.conflicts] == [name]

    @pytest.mark.parametrize("kind,name,rel", CASES)
    def test_the_question_names_the_thing(self, project: Path, tmp_path: Path, kind, name, rel):
        """ "Your group changed the skill \"deploy\"" — not "the agent"."""
        source = tmp_path / "cache"
        self._write_source(source, kind, rel, "v1")
        self._sync(project, source, kind).run()
        (project / ".ember" / kind / rel).write_text("MINE", encoding="utf-8")
        self._write_source(source, kind, rel, "v2")

        conflict = self._sync(project, source, kind).run().conflicts[0]

        assert conflict.entry_kind == kind
        assert name in conflict.question()
        assert "keep yours" in conflict.question()

    @pytest.mark.parametrize("kind,name,rel", CASES)
    def test_taking_the_group_version_replaces_it(
        self, project: Path, tmp_path: Path, kind, name, rel
    ):
        source = tmp_path / "cache"
        self._write_source(source, kind, rel, "v1")
        self._sync(project, source, kind).run()
        (project / ".ember" / kind / rel).write_text("MINE", encoding="utf-8")
        self._write_source(source, kind, rel, "v2")
        self._sync(project, source, kind).run()

        self._sync(project, source, kind).resolve(name, accept_incoming=True)

        assert (project / ".ember" / kind / rel).read_text(encoding="utf-8") == "v2"

    def test_questions_of_different_kinds_share_one_list(self, project: Path, tmp_path: Path):
        """A person answering them wants the lot, not one list per kind
        — and two kinds may hold the same name."""
        source = tmp_path / "cache"
        for kind, _name, rel in (self.CASES[0], self.CASES[1]):
            self._write_source(source, kind, rel, "v1")
            self._sync(project, source, kind).run()
            (project / ".ember" / kind / rel).write_text("MINE", encoding="utf-8")
            self._write_source(source, kind, rel, "v2")
            self._sync(project, source, kind).run()

        everything = self._sync(project, source, "agents").pending_all()

        assert {c.entry_kind for c in everything} == {"agents", "skills"}

    def test_answering_one_leaves_the_others(self, project: Path, tmp_path: Path):
        source = tmp_path / "cache"
        for kind, _name, rel in (self.CASES[0], self.CASES[1]):
            self._write_source(source, kind, rel, "v1")
            self._sync(project, source, kind).run()
            (project / ".ember" / kind / rel).write_text("MINE", encoding="utf-8")
            self._write_source(source, kind, rel, "v2")
            self._sync(project, source, kind).run()

        self._sync(project, source, "agents").resolve("reviewer", accept_incoming=True)

        left = self._sync(project, source, "agents").pending_all()
        assert [c.id for c in left] == ["skills/deploy"]
