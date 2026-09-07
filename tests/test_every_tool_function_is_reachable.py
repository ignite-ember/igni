"""Every tool igni registers must be named by some test.

The tools are the product. The RPC table is how a client talks to the
backend; the *tool functions* are what the agent does to your
repository — edit files, run shell, spawn sub-agents, delete knowledge.
An inventory found sixty-six statically-named functions and thirteen
of igni's own with no direct test at all, including ``stop_process``,
which kills a process, and the whole of ``KnowledgeTools``, which can
delete the knowledge base.

**A rule, not a list.** The registered functions are read out of the
toolkits and compared against the test suite. Adding a tool without a
test fails here, and the failure names the function.

What this can and cannot establish, said plainly: it checks that the
name appears in ``tests/``. That is a low bar — a test *mentioning*
``stop_process`` satisfies it — but it is the bar that catches the
thing that actually happens, which is a toolkit shipping with no test
file thinking about it at all. Whether the test is any good is a
judgement, and the judgement is in the test's own assertions.

``_UNTESTED`` is the exceptions list, and it is meant to shrink. Every
entry names why. It is not a place to park new work: adding to it is a
visible decision in review, which is the point.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = _ROOT / "src/ember_code/core/tools"
_TESTS = _ROOT / "tests"

#: Functions registered by igni's toolkits that no test names, with
#: the reason. Every one of these is a decision, not an oversight.
_UNTESTED: dict[str, str] = {}


def _registered_functions() -> dict[str, str]:
    """``{function_name: file}`` for every ``self.register(self.x)``.

    ``Toolkit.register`` is how a function becomes callable by the
    model — a method that is never registered is ordinary Python and
    not part of this surface.
    """
    found: dict[str, str] = {}
    for path in _TOOLS.rglob("*.py"):
        text = path.read_text()
        for name in re.findall(r"self\.register\(\s*self\.([a-z_0-9]+)\s*\)", text):
            found[name] = str(path.relative_to(_ROOT))
    return found


def _names_in_tests() -> set[str]:
    """Every identifier-ish token appearing anywhere under ``tests/``."""
    seen: set[str] = set()
    for path in _TESTS.rglob("*.py"):
        if path.name == Path(__file__).name:
            continue
        seen.update(re.findall(r"[a-z_][a-z_0-9]{3,}", path.read_text()))
    return seen


class TestBothSidesWereRead:
    """A scan that measured nothing passes every comparison below."""

    def test_the_toolkits_register_functions(self):
        assert len(_registered_functions()) > 30

    def test_the_test_suite_was_read(self):
        assert len(_names_in_tests()) > 2000


class TestEveryRegisteredFunctionIsNamedByATest:
    @pytest.mark.parametrize("name", sorted(_registered_functions()))
    def test_it_appears_somewhere_in_tests(self, name: str):
        if name in _UNTESTED:
            pytest.skip(f"{name}: {_UNTESTED[name]}")

        assert name in _names_in_tests(), (
            f"{name} ({_registered_functions()[name]}) is registered as a "
            "model-callable tool and no test mentions it. The agent can call "
            "it against a user's repository; something should have run it "
            "first."
        )

    def test_the_exceptions_list_is_still_true(self):
        """An entry that has since acquired a test is a lie about the
        state of the suite, and the kind that quietly accumulates."""
        stale = sorted(n for n in _UNTESTED if n in _names_in_tests())

        assert stale == [], (
            f"{stale} are in _UNTESTED and are now named by tests. Delete the "
            "entries — an exceptions list that is not true is worse than none."
        )

    def test_the_exceptions_list_names_real_functions(self):
        invented = sorted(set(_UNTESTED) - set(_registered_functions()))

        assert invented == [], f"{invented} are excused from a rule they were never subject to."


class TestEveryRegisteredFunctionHasADocstring:
    """The docstring *is* the tool's specification.

    Agno sends it to the model as the function description — it is the
    only thing the model reads before deciding whether to call
    something that can delete files. A registered function without one
    is a tool the model has to guess at.
    """

    @staticmethod
    def _docstrings() -> dict[str, tuple[str, str | None]]:
        out: dict[str, tuple[str, str | None]] = {}
        registered = _registered_functions()
        for path in _TOOLS.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                    and node.name in registered
                ):
                    out[node.name] = (
                        str(path.relative_to(_ROOT)),
                        ast.get_docstring(node),
                    )
        return out

    @pytest.mark.parametrize("name", sorted(_registered_functions()))
    def test_the_model_is_told_what_it_does(self, name: str):
        doc = self._docstrings().get(name)
        if doc is None:
            pytest.skip(f"{name} is registered in a class this parser did not reach")

        path, text = doc
        assert text, f"{name} ({path}) is model-callable with no docstring."
        assert len(text) > 25, (
            f"{name} ({path}) has a docstring too short to describe a tool: {text!r}"
        )


class TestExecutionToolsAreGated:
    """A toolkit that runs code must gate every way it runs code.

    ``PythonSpec`` named one of ``PythonTools``' seven functions in
    ``confirm_function_names``. The other six ran with no approval
    prompt, and four of them execute code or install packages from the
    internet: ``save_to_file_and_run``,
    ``run_python_file_return_variable``, ``pip_install_package``,
    ``uv_pip_install_package``.

    Partial gating is worse than none. A user who asks for ``Python``
    in an agent file sees a prompt on ``run_python_code``, concludes
    the toolkit is supervised, and is wrong about the rest.

    So the rule is per *toolkit*, not per function: if a spec gates
    anything, it gates everything its toolkit registers. The
    exceptions are named, with reasons, in ``_UNGATED``.
    """

    #: Specs that deliberately gate nothing, and why. Both are written
    #: decisions in ``tool_spec.py``, not omissions.
    _UNGATED = {
        "Schedule": (
            "the HITL prompt would fire on the LLM tool call rather than "
            "the underlying kernel action — see ScheduleSpec's docstring"
        ),
        "LS": "read-only directory listing; build() forces confirm=False",
        "Visualize": "one-way broadcast to the FE, no side effect on the host",
    }

    @staticmethod
    def _registered(toolkit) -> dict:
        """Everything a built toolkit exposes, sync **and** async.

        ``Toolkit.register`` sorts by ``iscoroutinefunction``: sync
        callables land in ``.functions`` and async ones in
        ``.async_functions``. Reading only ``.functions`` says a
        toolkit of async tools exposes nothing — it made ``WebTools``
        look like it registered no functions at all, and I nearly
        wrote that up as a defect. Half of igni's toolkits are async.
        """
        return {
            **(toolkit.functions or {}),
            **(getattr(toolkit, "async_functions", {}) or {}),
        }

    def test_python_is_not_in_the_registry_at_all(self):
        """The stronger version of the gate that used to be here.

        ``PythonSpec`` named one of ``PythonTools``' seven functions in
        ``confirm_function_names``; the other six ran with no approval
        prompt, four of them executing code or installing packages from
        the internet. That was fixed by gating all seven — and then the
        whole spec was removed, because nothing shipped declared
        ``Python`` and the main agent never had it.

        Removal beats gating: a toolkit that cannot be attached cannot
        be mis-gated. Pinned by name so a future re-introduction is a
        decision somebody makes on purpose, with the paragraph above
        in front of them.
        """
        from ember_code.core.tools.tool_spec import ToolSpecCatalog

        catalog = ToolSpecCatalog.default()
        assert "Python" not in catalog.registry_names_with_aliases
        assert "Python" not in catalog.valid_ephemeral_names

    @pytest.mark.parametrize("retired", ["Read", "Grep", "Glob", "LS", "Python"])
    def test_the_retired_toolkits_stay_retired(self, retired: str):
        """No bundled agent declared them and the main agent never had
        them, so they granted nothing and could only be reached by a
        user-authored ephemeral — which ``create_agent``'s own
        docstring was still recommending."""
        from ember_code.core.tools.tool_spec import ToolSpecCatalog

        assert retired not in ToolSpecCatalog.default().registry_names_with_aliases

    @pytest.mark.parametrize(
        "spec_name",
        ["Write", "Edit", "WebSearch", "WebFetch", "NotebookEdit"],
    )
    def test_every_gated_name_exists_in_the_built_toolkit(self, spec_name: str):
        """A gate on a function that does not exist gates nothing.

        ``WebSearchSpec`` listed ``duckduckgo_search`` and
        ``duckduckgo_news``; ``DuckDuckGoTools`` registers
        ``web_search`` and ``search_news``. agno matched the list
        against its registry, found neither, logged "Requires
        confirmation tool(s) not present in the toolkit" — and web
        search ran with no approval prompt while the spec asserted it
        was gated.

        The same shape as ``/config`` reading ``storage.backend``: a
        name renamed upstream, still referenced, failing quietly. The
        warning existed; nothing read the log.

        Only the specs whose toolkits build without live session
        context are listed — ``Bash``, ``Schedule``, ``CodeIndex`` and
        the rest need a real session, and a test that silently skipped
        them all would be the failure it is checking for. That is why
        this is parametrized by name and not derived: the list is
        visible, and a spec added to the catalog is not silently
        exempt.
        """
        from pathlib import Path

        from ember_code.core.tools.tool_spec import ToolBuildContext, ToolSpecCatalog

        spec = next((s for s in ToolSpecCatalog.default().specs if s.name == spec_name), None)
        assert spec is not None, f"{spec_name} is no longer in the catalog"
        if not spec.confirm_function_names:
            pytest.skip(f"{spec_name} gates nothing by design")

        try:
            toolkit = spec.build(ToolBuildContext(base_dir=Path("/tmp")), confirm=True)
        except ImportError as exc:  # optional extra not installed
            pytest.skip(f"{spec_name}: {exc}")

        registered = set(self._registered(toolkit))
        phantom = sorted(set(spec.confirm_function_names) - registered)

        assert phantom == [], (
            f"{spec_name} gates {phantom}, which its toolkit does not register. "
            f"The gate does nothing. Registered: {sorted(registered)}"
        )

    @pytest.mark.parametrize(
        "spec_name",
        ["Write", "Edit", "WebSearch", "WebFetch", "NotebookEdit"],
    )
    def test_nothing_that_writes_or_leaves_the_machine_is_ungated(self, spec_name: str):
        """Read-only functions may go ungated; the rest may not.

        A blunter rule — "a spec that gates anything gates everything"
        — flagged ``read_file_chunk``, ``notebook_read`` and
        ``notebook_read_cell``, none of which do anything to the user's
        machine. Loosening the rule to make those pass would have
        dropped the real finding with them, so the rule names the
        read-only functions instead and everything else must be gated.

        A new function is gated-by-default under this rule, which is
        the direction a permission check should fail in.
        """
        from pathlib import Path

        from ember_code.core.tools.tool_spec import ToolBuildContext, ToolSpecCatalog

        read_only = {
            "read_file",
            "read_file_chunk",
            "list_files",
            "search_content",
            "search_files",
            "notebook_read",
            "notebook_read_cell",
            "grep",
            "grep_files",
            "grep_count",
            "glob_files",
        }

        spec = next((s for s in ToolSpecCatalog.default().specs if s.name == spec_name), None)
        assert spec is not None
        try:
            toolkit = spec.build(ToolBuildContext(base_dir=Path("/tmp")), confirm=True)
        except ImportError as exc:
            pytest.skip(f"{spec_name}: {exc}")

        ungated = sorted(
            name
            for name, fn in self._registered(toolkit).items()
            if name not in read_only and not getattr(fn, "requires_confirmation", False)
        )

        assert ungated == [], (
            f"{spec_name} exposes {ungated} to the model with no approval "
            "prompt, and they are not on the read-only list."
        )

    def test_the_ungated_list_is_still_true(self):
        from ember_code.core.tools.tool_spec import ToolSpecCatalog

        by_name = {s.name: s for s in ToolSpecCatalog.default().specs}
        wrong = sorted(
            name
            for name in self._UNGATED
            if name in by_name and by_name[name].confirm_function_names
        )

        assert wrong == [], (
            f"{wrong} are excused from gating and now gate something. Delete "
            "the entries, or the exception outlives the decision."
        )
