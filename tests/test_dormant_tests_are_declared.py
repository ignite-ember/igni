"""A test that skips itself by default has to be declared, with a reason.

Thirty-five tests sat skipped for want of a database, covering the
subsystem that broke most often. Nothing was wrong with them — they
simply never ran, and a suite reporting green while a third of its
coverage of the riskiest area is dormant is worse than one that never
had the tests, because it looks like coverage.

The gates themselves are legitimate: some tests need a service, or an
API key that costs money, or hardware. What is not legitimate is
acquiring one silently. This inventory makes adding a gate a visible
act — the test fails until the new variable is listed here with a note
on why it cannot run by default, and on what makes it run.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).parent

#: Every environment variable that can make part of the suite dormant,
#: with why it exists and what provides it.
DECLARED_GATES: dict[str, str] = {
    "NEO4J_TEST_URI": (
        "A live Neo4j. CI provides one as a service container (see the "
        "`test` job in ci.yml), so these DO run on every push; locally "
        "they skip unless you point this at a database."
    ),
    "IGNI_TEST_LLM_API_KEY": (
        "Live model calls, which cost money per run. Deliberately not "
        "in CI; run locally when touching the model layer."
    ),
    "IGNI_TEST_LLM_BASE_URL": (
        "Which endpoint the live model tests call. Read alongside the "
        "key above; absent it, those tests skip with it."
    ),
    "IGNI_TEST_LLM_MODEL": (
        "Which model the live tests use. Read alongside the key above; "
        "absent it, those tests skip with it."
    ),
    "NEO4J_TEST_USER": (
        "Credentials for NEO4J_TEST_URI. CI sets them with the service "
        "container; locally they default to neo4j/test."
    ),
    "NEO4J_TEST_PASSWORD": (
        "Credentials for NEO4J_TEST_URI. CI sets them with the service "
        "container; locally they default to neo4j/test."
    ),
    "IGNI_TEST_NEO4J_RUNTIME": (
        "Spawns real Neo4j subprocesses — needs ~4 GB free RAM, which "
        "a standard GitHub runner does not reliably have. The service "
        "container above covers the same code paths against a database "
        "we did not have to start ourselves."
    ),
    "IGNI_LIVE_WS": (
        "Drives the Playwright specs against a real backend over a "
        "websocket. Lives in the e2e suite rather than here."
    ),
}


class _EnvReadVisitor(ast.NodeVisitor):
    """Collects environment reads, never descending into a test.

    A gate is decided before collection, so a read inside a test body
    cannot be one. ``test_embeddings`` asserts on ``HF_HUB_OFFLINE``
    from inside a method of a test class — a subject, not a condition —
    which is why this refuses test functions at any depth rather than
    filtering the module's top level.
    """

    def __init__(self, consts: dict[str, str]) -> None:
        self.consts = consts
        self.names: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 — ast API
        if not node.name.startswith("test_"):
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802 — ast API
        if not node.name.startswith("test_"):
            self.generic_visit(node)

    def _literal(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.consts.get(node.id)
        return None

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast API
        func = node.func
        reads_env = (
            isinstance(func, ast.Attribute)
            and func.attr in {"get", "getenv"}
            and bool(node.args)
            and (
                func.attr == "getenv"
                or (isinstance(func.value, ast.Attribute) and func.value.attr == "environ")
            )
        )
        if reads_env and (name := self._literal(node.args[0])):
            self.names.add(name)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802 — ast API
        value = node.value
        is_environ = isinstance(value, ast.Attribute) and value.attr == "environ"
        if is_environ and (name := self._literal(node.slice)):
            self.names.add(name)
        self.generic_visit(node)


def _gating_variables() -> set[str]:
    """Environment variables that decide whether tests run.

    A module counts when it both skips and reads the environment.
    Module-level, not per-test: ``monkeypatch.setenv`` inside a test
    body is a test configuring its own world, which decides nothing
    about whether it runs — matching those is how an earlier version
    of this scanner reported ``HF_HOME`` as a gate.
    """
    found: set[str] = set()
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        if "skip" not in source.lower():
            continue
        tree = ast.parse(source)
        consts = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            for target in node.targets
            if isinstance(target, ast.Name) and isinstance(node.value.value, str)
        }
        visitor = _EnvReadVisitor(consts)
        visitor.visit(tree)
        found.update(visitor.names)
    return found


def test_every_gate_is_declared():
    """The mechanism. A new gate fails this until someone writes down
    what it needs and why CI cannot provide it."""
    undeclared = _gating_variables() - set(DECLARED_GATES)

    assert not undeclared, (
        "These environment variables gate tests but are not declared in "
        f"DECLARED_GATES: {sorted(undeclared)}. Add them with a note on what "
        "they need and why the suite cannot provide it by default."
    )


def test_declared_gates_are_still_in_use():
    """The other direction: a gate nobody uses is a stale note, and a
    stale note is how a file like this stops being trusted."""
    in_use = _gating_variables()
    # ``IGNI_LIVE_WS`` gates the Playwright suite, which lives under
    # clients/web/e2e and is not scanned here.
    stale = {g for g in DECLARED_GATES if g not in in_use} - {"IGNI_LIVE_WS"}

    assert not stale, f"declared but no longer gating anything: {sorted(stale)}"


def test_every_gate_says_why():
    """A variable name is not a reason. The next person needs to know
    whether to set it, not merely that they could."""
    for gate, reason in DECLARED_GATES.items():
        assert len(reason) > 40, f"{gate}: say what it needs and why it is not on by default"


def test_the_neo4j_gate_is_satisfied_in_ci():
    """The specific regression this file was written for.

    These tests were dormant everywhere, including CI, which is what
    let them stay dormant. CI now runs them against a service
    container; if that wiring is removed, this says so.
    """
    ci = (TESTS_DIR.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "NEO4J_TEST_URI" in ci, "CI no longer provides a Neo4j; 35 tests just went dormant again"
    assert "neo4j:" in ci
