"""The tool functions the model can call and no test had called.

The RPC table is the client↔backend contract; the *tools* are what
the agent does with your machine. An inventory of the toolkits found
sixty-six statically-named functions, and thirteen of the ones igni
itself implements had no direct test at all:

* ``EmberShellTools`` — ``watch_process``, ``stop_process``,
  ``list_processes``. Three of the five process-control functions.
  ``stop_process`` kills a process; nothing had ever called it.
* ``LoopProgressTool`` — all five. The toolkit was not imported by a
  single test file.
* ``KnowledgeTools`` — all four. Likewise. The names appear in tests,
  but at the session and slash-command layers, not the toolkit.
* ``LoopTools.loop_resume`` — only ``Session.resume_loop`` was
  covered, not the function the model calls.

Three more gaps are agno's code, not ours — ``PythonTools`` (7),
``DuckDuckGoTools.search_news``, ``ReasoningTools.think``/``analyze``.
Testing a vendored library's internals is not this repository's job;
what *is* our job is that they are attached under the right
conditions, and that is
``tests/test_every_tool_function_is_reachable.py``.

Each test here calls the function the way the model would — by name,
with the arguments the docstring advertises — and checks the string
that comes back. The return value is the whole interface: the model
reads it and decides what to do next, so a function that "works" and
answers unintelligibly has failed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ember_code.core.tools.knowledge import KnowledgeTools
from ember_code.core.tools.loop_progress import LoopProgressTool
from ember_code.core.tools.process_supervisor import ProcessSupervisor
from ember_code.core.tools.shell import EmberShellTools


# ── EmberShellTools: the three process-control functions ────────────


@pytest.fixture
def shell(tmp_path):
    """A toolkit on its own supervisor, so nothing leaks between tests."""
    supervisor = ProcessSupervisor()
    tools = EmberShellTools(base_dir=tmp_path, supervisor=supervisor)
    try:
        yield tools
    finally:
        supervisor.registry.kill_all()


def _pid_from(output: str) -> int:
    """Pull the pid out of ``run_shell_command(background=True)``.

    Backgrounding sleeps three seconds before returning — the startup
    window in ``ProcessSupervisor`` — and a process that has already
    exited by then is removed from the registry and reported without a
    pid. So every command here has to outlive that window, which is
    why they sleep four seconds rather than echoing immediately.
    """
    import re

    match = re.search(r"\b(?:PID|pid)[^0-9]{0,3}(\d+)", output)
    assert match, f"no pid in background output: {output!r}"
    return int(match.group(1))


class TestListProcesses:
    @pytest.mark.asyncio
    async def test_it_says_so_when_nothing_is_running(self, shell):
        """The empty case is the one the model meets most often, and a
        bare empty string would read as a failed call."""
        assert await shell.list_processes() == "No background processes running."

    @pytest.mark.asyncio
    async def test_a_backgrounded_process_appears_with_its_command(self, shell):
        out = await shell.run_shell_command(
            command="sleep 30", background=True, timeout=2
        )
        pid = _pid_from(out)

        listing = await shell.list_processes()

        # The pid, because that is the handle every other function
        # takes; and the command, because a table of anonymous pids
        # tells the model nothing about which one to stop.
        assert str(pid) in listing
        assert "sleep 30" in listing
        assert "PID" in listing


class TestStopProcess:
    @pytest.mark.asyncio
    async def test_an_unknown_pid_is_reported_not_ignored(self, shell):
        assert "No tracked process" in await shell.stop_process(pid=999_999)

    @pytest.mark.asyncio
    async def test_it_kills_a_running_process_and_forgets_it(self, shell):
        out = await shell.run_shell_command(
            command="sleep 30", background=True, timeout=2
        )
        pid = _pid_from(out)

        result = await shell.stop_process(pid=pid)

        assert f"Process {pid} stopped" in result
        # Actually gone, not merely reported gone. The registry is what
        # the watcher panel and `list_processes` read.
        assert await shell.list_processes() == "No background processes running."

    @pytest.mark.asyncio
    async def test_stopping_a_finished_process_says_it_already_finished(self, shell):
        """Distinct from "stopped" on purpose: the model asked to kill
        something and needs to know it did not have to."""
        out = await shell.run_shell_command(
            command="sleep 3.5; echo bye", background=True, timeout=2
        )
        pid = _pid_from(out)
        for _ in range(120):
            if not (
                shell._supervisor.registry.get(pid)
                and shell._supervisor.registry.get(pid).is_running()
            ):
                break
            await asyncio.sleep(0.05)

        result = await shell.stop_process(pid=pid)

        assert "already finished" in result
        assert "exit code" in result


class TestWatchProcess:
    @pytest.mark.asyncio
    async def test_an_unknown_pid_is_reported(self, shell):
        assert "No tracked process" in await shell.watch_process(pid=999_999)

    @pytest.mark.asyncio
    async def test_it_returns_output_produced_during_the_window(self, shell):
        out = await shell.run_shell_command(
            command="sleep 3.4; echo WATCHED", background=True, timeout=2
        )
        pid = _pid_from(out)

        result = await shell.watch_process(pid=pid, seconds=5)

        assert "WATCHED" in result

    @pytest.mark.asyncio
    async def test_it_returns_early_when_the_process_exits(self, shell):
        """The docstring promises "or until the process exits". Without
        that, a model asking to watch for 30s blocks the turn for 30s
        on a process that ended immediately."""
        out = await shell.run_shell_command(
            command="sleep 3.4", background=True, timeout=2
        )
        pid = _pid_from(out)

        started = asyncio.get_event_loop().time()
        await shell.watch_process(pid=pid, seconds=30)
        elapsed = asyncio.get_event_loop().time() - started

        assert elapsed < 10, f"watch_process blocked for {elapsed:.1f}s on a dead process"

    @pytest.mark.asyncio
    async def test_seconds_is_clamped_to_the_documented_range(self, shell):
        """``1–30`` per the docstring. A model that passes 3600 must
        not hold the turn open for an hour."""
        out = await shell.run_shell_command(
            command="sleep 60", background=True, timeout=2
        )
        pid = _pid_from(out)

        started = asyncio.get_event_loop().time()
        await shell.watch_process(pid=pid, seconds=3600)
        elapsed = asyncio.get_event_loop().time() - started

        assert elapsed < 40, f"watch_process ignored the 30s cap: {elapsed:.1f}s"


# ── LoopProgressTool: the whole toolkit ─────────────────────────────


class _Store:
    """The store's four operations, in memory."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}

    async def get(self, run_id: str, key: str) -> str | None:
        return self.rows.get((run_id, key))

    async def set(self, run_id: str, key: str, value: str) -> None:
        self.rows[(run_id, key)] = value

    async def list(self, run_id: str) -> list[tuple[str, str]]:
        return [(k, v) for (r, k), v in self.rows.items() if r == run_id]

    async def delete(self, run_id: str, key: str) -> bool:
        return self.rows.pop((run_id, key), None) is not None

    async def clear(self, run_id: str) -> int:
        keys = [k for k in self.rows if k[0] == run_id]
        for k in keys:
            del self.rows[k]
        return len(keys)


class _LoopSession:
    def __init__(self, run_id: str | None) -> None:
        self.loop_run_id = run_id
        self.loop_progress_store = _Store()


@pytest.fixture
def progress():
    return LoopProgressTool(_LoopSession("run-1"))  # type: ignore[arg-type]


class TestLoopProgressWithoutALoop:
    """Every one of the five must refuse, and say why.

    This is the state the model is in when it calls them speculatively,
    and an empty string back would read as "nothing recorded" rather
    than "you are not in a loop".
    """

    @pytest.mark.parametrize(
        "call",
        [
            lambda t: t.loop_progress_get("k"),
            lambda t: t.loop_progress_set("k", "v"),
            lambda t: t.loop_progress_list(),
            lambda t: t.loop_progress_delete("k"),
            lambda t: t.loop_progress_clear(),
        ],
        ids=["get", "set", "list", "delete", "clear"],
    )
    @pytest.mark.asyncio
    async def test_it_explains_that_no_loop_is_active(self, call: Any):
        tools = LoopProgressTool(_LoopSession(None))  # type: ignore[arg-type]

        result = await call(tools)

        assert result.startswith("ERROR:")
        assert "loop_start" in result, f"the error does not say what to do: {result!r}"


class TestLoopProgressRoundTrip:
    @pytest.mark.asyncio
    async def test_a_key_that_was_never_set_reads_empty(self, progress):
        assert await progress.loop_progress_get("nope") == ""

    @pytest.mark.asyncio
    async def test_set_then_get_returns_the_value_verbatim(self, progress):
        await progress.loop_progress_set("section_3", "verified ok")

        assert await progress.loop_progress_get("section_3") == "verified ok"

    @pytest.mark.asyncio
    async def test_set_is_idempotent(self, progress):
        """The docstring promises a second ``set`` replaces rather than
        throwing on the unique constraint — the model appends notes
        across iterations."""
        await progress.loop_progress_set("k", "first")
        await progress.loop_progress_set("k", "second")

        assert await progress.loop_progress_get("k") == "second"

    @pytest.mark.asyncio
    async def test_an_empty_list_is_the_signal_for_iteration_one(self, progress):
        assert await progress.loop_progress_list() == "No progress recorded."

    @pytest.mark.asyncio
    async def test_list_names_every_key_and_value(self, progress):
        await progress.loop_progress_set("a", "done")
        await progress.loop_progress_set("b", "skipped")

        listing = await progress.loop_progress_list()

        assert "- a: done" in listing
        assert "- b: skipped" in listing

    @pytest.mark.asyncio
    async def test_delete_distinguishes_a_hit_from_a_miss(self, progress):
        """"Deleted" and "no entry" are different facts, and the model
        re-does work based on which it got."""
        await progress.loop_progress_set("k", "v")

        assert "Deleted" in await progress.loop_progress_delete("k")
        assert "No entry" in await progress.loop_progress_delete("k")

    @pytest.mark.asyncio
    async def test_clear_reports_how_many_it_removed(self, progress):
        await progress.loop_progress_set("a", "1")
        await progress.loop_progress_set("b", "2")

        assert "Cleared 2 progress entries" in await progress.loop_progress_clear()
        assert await progress.loop_progress_list() == "No progress recorded."

    @pytest.mark.asyncio
    async def test_clear_gets_the_singular_right(self, progress):
        await progress.loop_progress_set("only", "1")

        assert "Cleared 1 progress entry" in await progress.loop_progress_clear()

    @pytest.mark.asyncio
    async def test_another_loops_entries_are_invisible(self, progress):
        """The module docstring: entries from a previous run stay in the
        DB but are invisible. A loop that reads the last loop's verdicts
        skips work it never did."""
        await progress.loop_progress_set("shared", "from run-1")
        progress._session.loop_run_id = "run-2"

        assert await progress.loop_progress_get("shared") == ""
        assert await progress.loop_progress_list() == "No progress recorded."


# ── KnowledgeTools: the whole toolkit ───────────────────────────────


class _Result:
    def __init__(self, name: str, content: str) -> None:
        self.name = name
        self.content = content


class _SearchResponse:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.total = len(results)


class _AddResult:
    def __init__(self, success: bool, message: str = "", error: str = "") -> None:
        self.success = success
        self.message = message
        self.error = error


class _Status:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.collection_name = "igni"
        self.document_count = 42
        self.embedder = "test-embedder"


class _DeleteResult:
    def __init__(self, deleted: int) -> None:
        self.deleted = deleted


class _Knowledge:
    def __init__(self, deleted: int = 0, raises: Exception | None = None) -> None:
        self._deleted = deleted
        self._raises = raises

    async def delete_by_query(self, query: str) -> _DeleteResult:
        if self._raises:
            raise self._raises
        return _DeleteResult(self._deleted)


class _Manager:
    def __init__(
        self,
        results: list[_Result] | None = None,
        add: _AddResult | None = None,
        enabled: bool = True,
        knowledge: _Knowledge | None = None,
    ) -> None:
        self._results = results or []
        self._add = add or _AddResult(True, "Stored.")
        self._enabled = enabled
        self.knowledge = knowledge
        self.added: list[tuple[str, dict | None]] = []

    async def search(self, query: str, limit: int = 5) -> _SearchResponse:
        return _SearchResponse(self._results)

    async def add(self, text: str, metadata: dict | None = None) -> _AddResult:
        self.added.append((text, metadata))
        return self._add

    async def status(self) -> _Status:
        return _Status(self._enabled)


class TestKnowledgeSearch:
    @pytest.mark.asyncio
    async def test_an_empty_result_names_the_query(self, ):
        """"No results" without the query leaves the model unable to
        tell which of its several searches came back empty."""
        tools = KnowledgeTools(_Manager())  # type: ignore[arg-type]

        assert await tools.knowledge_search("deploy process") == (
            "No knowledge found for: deploy process"
        )

    @pytest.mark.asyncio
    async def test_hits_carry_their_name_and_content(self):
        tools = KnowledgeTools(  # type: ignore[arg-type]
            _Manager(results=[_Result("runbook.md", "restart the worker")])
        )

        out = await tools.knowledge_search("restart")

        assert "Found 1 result(s):" in out
        assert "[runbook.md]" in out
        assert "restart the worker" in out

    @pytest.mark.asyncio
    async def test_an_unnamed_hit_is_labelled_rather_than_blank(self):
        tools = KnowledgeTools(_Manager(results=[_Result("", "body")]))  # type: ignore[arg-type]

        assert "[untitled]" in await tools.knowledge_search("q")


class TestKnowledgeAdd:
    @pytest.mark.asyncio
    async def test_it_stores_the_content_and_confirms(self):
        manager = _Manager()
        tools = KnowledgeTools(manager)  # type: ignore[arg-type]

        assert await tools.knowledge_add("a fact") == "Stored."
        assert manager.added == [("a fact", None)]

    @pytest.mark.asyncio
    async def test_a_source_becomes_metadata(self):
        manager = _Manager()
        tools = KnowledgeTools(manager)  # type: ignore[arg-type]

        await tools.knowledge_add("a fact", source="docs/x.md")

        assert manager.added == [("a fact", {"source": "docs/x.md"})]

    @pytest.mark.asyncio
    async def test_a_failure_is_reported_as_an_error(self):
        """Not silently swallowed: the model must not go on believing
        it recorded something it did not."""
        tools = KnowledgeTools(  # type: ignore[arg-type]
            _Manager(add=_AddResult(False, error="embedder unavailable"))
        )

        assert await tools.knowledge_add("x") == "Error: embedder unavailable"


class TestKnowledgeDelete:
    @pytest.mark.asyncio
    async def test_it_previews_instead_of_deleting_by_default(self):
        """``confirm`` defaults to False, and this is the guard that
        stops a model deleting the knowledge base while exploring."""
        knowledge = _Knowledge(deleted=5)
        tools = KnowledgeTools(_Manager(knowledge=knowledge))  # type: ignore[arg-type]

        out = await tools.knowledge_delete("everything")

        assert "Preview mode" in out
        assert "confirm=True" in out

    @pytest.mark.asyncio
    async def test_confirming_deletes_and_reports_the_count(self):
        tools = KnowledgeTools(_Manager(knowledge=_Knowledge(deleted=3)))  # type: ignore[arg-type]

        out = await tools.knowledge_delete("stale", confirm=True)

        assert "Deleted 3" in out

    @pytest.mark.asyncio
    async def test_a_confirmed_delete_that_matches_nothing_says_so(self):
        tools = KnowledgeTools(_Manager(knowledge=_Knowledge(deleted=0)))  # type: ignore[arg-type]

        assert "No entries found" in await tools.knowledge_delete("x", confirm=True)

    @pytest.mark.asyncio
    async def test_no_knowledge_base_is_an_error_not_a_crash(self):
        tools = KnowledgeTools(_Manager(knowledge=None))  # type: ignore[arg-type]

        assert "not available" in await tools.knowledge_delete("x", confirm=True)

    @pytest.mark.asyncio
    async def test_a_store_failure_is_returned_not_raised(self):
        """A raise here would abort the model's turn; a string lets it
        recover and say what happened."""
        tools = KnowledgeTools(  # type: ignore[arg-type]
            _Manager(knowledge=_Knowledge(raises=RuntimeError("chroma is down")))
        )

        out = await tools.knowledge_delete("x", confirm=True)

        assert out.startswith("Error deleting entries:")
        assert "chroma is down" in out


class TestKnowledgeStatus:
    @pytest.mark.asyncio
    async def test_a_disabled_base_says_disabled(self):
        assert (
            await KnowledgeTools(_Manager(enabled=False)).knowledge_status()  # type: ignore[arg-type]
            == "Knowledge base is disabled."
        )

    @pytest.mark.asyncio
    async def test_an_enabled_base_reports_collection_count_and_embedder(self):
        out = await KnowledgeTools(_Manager()).knowledge_status()  # type: ignore[arg-type]

        assert "igni" in out
        assert "42" in out
        assert "test-embedder" in out
