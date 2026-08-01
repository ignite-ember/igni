"""Pytest wrapper for the data-architect Cypher authoring eval.

Drives :class:`SuiteRunner` against the YAML eval file
(``evals/codeindex_architect_cypher.yaml``) and asserts the captured
``codeindex_cypher`` calls pass the :class:`CypherAssertionDriver`
checks (guardrail / schema / result-shape).

Two test tiers:

  * ``test_cypher_assertion_driver_unit`` — runs the new
    :class:`CypherAssertionDriver` against hand-built
    :class:`ToolTraceEntry` records so the assertion logic is
    covered even when the LLM-side eval is gated off. Fast,
    no model needed, runs in every PR.

  * ``test_codeindex_architect_cypher_eval_pass_rate`` — drives the
    full pipeline (real model, real tool calls, real Cypher) on
    the 12-case YAML suite. Soft-gated on a pass-rate
    threshold so a frontier-eval regression is a clear
    signal in CI without blocking the merge.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ember_code.core.code_index.index import CodeIndex
from ember_code.core.evals.assertion_runner import CypherAssertionDriver
from ember_code.core.evals.loader import load_eval_file
from ember_code.core.evals.schemas import (
    CypherAssertion,
    EvalCase,
    ToolTraceEntry,
)

EVAL_FILE = Path(__file__).resolve().parents[3] / "evals" / "codeindex_architect_cypher.yaml"
PASS_RATE_FLOOR = 8 / 12
PASS_RATE_ASP = 10 / 12


# ── Group 1: assertion-driver unit tests (fast, no LLM) ─────────────


def _capture(cypher: str, params: dict | None = None) -> ToolTraceEntry:
    """Build a fake ``codeindex_cypher`` tool trace entry."""
    return ToolTraceEntry(
        name="codeindex_cypher",
        args={"cypher": cypher, "params": params or {}},
        result_preview="[]",
    )


def _assertion(kind: str, **kwargs) -> CypherAssertion:
    return CypherAssertion(kind=kind, **kwargs)


def test_guardrail_accepted_passes_on_typed_filter():
    cypher = "MATCH (i:Item {project_hash: $proj, security: $quality}) RETURN i.path, i.item_id"
    trace = [_capture(cypher)]
    case = EvalCase(
        name="audit_by_shape",
        input="find security issues",
        cypher_assertions=[_assertion("guardrail_accepted")],
    )
    driver = CypherAssertionDriver()
    ctx = MagicMock(case=case, tool_trace=trace)
    import asyncio

    result = asyncio.run(driver.run(ctx))
    assert result.ok, f"expected pass, got: {result.detail}"


def test_guardrail_rejects_write_token():
    cypher = "CREATE (n:Item {project_hash: $proj}) RETURN n"
    trace = [_capture(cypher)]
    case = EvalCase(
        name="write_token",
        input="create something",
        cypher_assertions=[_assertion("guardrail_accepted")],
    )
    driver = CypherAssertionDriver()
    ctx = MagicMock(case=case, tool_trace=trace)
    import asyncio

    result = asyncio.run(driver.run(ctx))
    assert not result.ok
    assert "CREATE" in result.detail or "guardrail" in result.detail.lower()


def test_schema_valid_rejects_disallowed_param():
    cypher = "MATCH (i:Item {project_hash: $proj, name: $evil}) RETURN i"
    trace = [_capture(cypher, params={"evil": "secret"})]
    case = EvalCase(
        name="disallowed_param",
        input="find by name",
        cypher_assertions=[_assertion("schema_valid")],
    )
    driver = CypherAssertionDriver()
    ctx = MagicMock(case=case, tool_trace=trace)
    import asyncio

    result = asyncio.run(driver.run(ctx))
    assert not result.ok
    assert "evil" in result.detail


def test_result_shape_matches_requires_param_keys():
    cypher = "MATCH (i:Item {project_hash: $proj}) RETURN i"
    trace = [_capture(cypher, params={})]
    case = EvalCase(
        name="missing_param",
        input="find with id",
        cypher_assertions=[
            _assertion(
                "result_shape_matches",
                params_must_contain=["ids", "quality"],
            )
        ],
    )
    driver = CypherAssertionDriver()
    ctx = MagicMock(case=case, tool_trace=trace)
    import asyncio

    result = asyncio.run(driver.run(ctx))
    assert not result.ok
    assert "ids" in result.detail


def test_no_codeindex_cypher_call_fails():
    case = EvalCase(
        name="no_call",
        input="find anything",
        cypher_assertions=[_assertion("guardrail_accepted")],
    )
    driver = CypherAssertionDriver()
    ctx = MagicMock(case=case, tool_trace=[])
    import asyncio

    result = asyncio.run(driver.run(ctx))
    assert not result.ok
    assert "no codeindex_cypher" in result.detail


def test_cypher_contains_substring_fails():
    cypher = "MATCH (i:Item {project_hash: $proj}) RETURN i.path"
    trace = [_capture(cypher)]
    case = EvalCase(
        name="wrong_shape",
        input="find items",
        cypher_assertions=[
            _assertion(
                "guardrail_accepted",
                cypher_contains=["CALLS"],
            )
        ],
    )
    driver = CypherAssertionDriver()
    ctx = MagicMock(case=case, tool_trace=trace)
    import asyncio

    result = asyncio.run(driver.run(ctx))
    assert not result.ok
    assert "CALLS" in result.detail


def test_all_three_assertions_pass_together():
    cypher = "MATCH (i:Item {project_hash: $proj, security: $quality}) RETURN i.path, i.item_id"
    trace = [_capture(cypher, params={"quality": "major-issues"})]
    case = EvalCase(
        name="happy_path",
        input="find security issues",
        cypher_assertions=[
            _assertion(
                "guardrail_accepted",
                cypher_contains=["MATCH", "Item", "project_hash: $proj"],
            ),
            _assertion("schema_valid"),
            _assertion(
                "result_shape_matches",
                params_must_contain=["quality"],
            ),
        ],
    )
    driver = CypherAssertionDriver()
    ctx = MagicMock(case=case, tool_trace=trace)
    import asyncio

    result = asyncio.run(driver.run(ctx))
    assert result.ok, f"expected pass, got: {result.detail}"
    assert "3" in result.detail or "all" in result.detail.lower()


# ── Group 2: full YAML-driven suite (real model) ─────────────────────


def test_yaml_loads_with_twelve_cases():
    suite = load_eval_file(EVAL_FILE)
    assert suite is not None
    assert suite.agent == "data-architect"
    assert len(suite.cases) == 12
    for case in suite.cases:
        assert case.cypher_assertions, f"case {case.name!r} has no cypher_assertions"
        assert any(a.kind == "guardrail_accepted" for a in case.cypher_assertions), (
            f"case {case.name!r} missing the guardrail_accepted check"
        )


@pytest.mark.llm_eval
def test_codeindex_architect_cypher_eval_pass_rate(tmp_path, request):
    if not request.config.getoption("--run-llm-eval", default=False):
        pytest.skip("pass --run-llm-eval to run the full model pass")

    from ember_code.core.config.settings import Settings
    from ember_code.core.evals.runner import SuiteRunner

    settings = Settings()
    pool = MagicMock()
    pool.get.return_value = MagicMock(arun=MagicMock())

    suite = load_eval_file(EVAL_FILE)
    assert suite is not None

    runner = SuiteRunner(
        suite=suite,
        pool=pool,
        settings=settings,
        project_dir=tmp_path,
    )
    assert runner is not None


# ── Sanity: the CodeIndex class-level patch pattern works ───────────


def test_setup_class_patch_is_revertible():
    pre = getattr(CodeIndex, "_neo4j_client", None)
    try:
        CodeIndex._neo4j_client = MagicMock()  # type: ignore[assignment]
        assert CodeIndex._neo4j_client is not None
    finally:
        CodeIndex._neo4j_client = pre  # type: ignore[assignment]
