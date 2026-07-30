"""Tests for the ``codeindex_query`` empty-call guardrail.

``is_empty_call`` is the precondition check that catches the
case-11-shape failure: the agent reaches for ``codeindex_query``,
names the dimension it wants to narrow on (e.g. ``security=``),
then passes ``None`` instead of an actual list of severities.
Without the guard, the BE returns arbitrary ranked items and the
agent reads them as "the worst offenders" and confabulates a
triage.

The contract:
  * NO narrowing input → empty → guardrail fires
  * ``query_text`` OR ``ids`` set → NOT empty
  * any typed-filter set to a real value → NOT empty
  * ``False`` is meaningful for bool filters (it's the
    "items that don't need refactoring" query — unusual but
    real) → NOT empty
  * Output-control args (``sections`` / ``limit`` / ``commit``)
    are NOT narrowing input — the model knows the difference,
    so passing them alone still counts as empty.

The module-level ``is_empty_call`` remains as a compatibility
shim; the real implementation lives on
:meth:`QueryInput.is_empty_call`.
"""

from __future__ import annotations

from ember_code.core.tools.codeindex.empty_guard import is_empty_call


class TestEmptyCallEmptyShapes:
    """Shapes that should be flagged as empty (no narrowing input)."""

    def test_no_kwargs_is_empty(self):
        # The truly-empty call. Nothing to narrow on.
        assert is_empty_call() is True

    def test_all_none_is_empty(self):
        # Same as no-kwargs but spelled out — the agent's
        # actual failure mode is "named the filter, passed None".
        assert (
            is_empty_call(
                query_text=None,
                ids=None,
                kind=None,
                security=None,
                quality=None,
                vulnerabilities=None,
            )
            is True
        )

    def test_empty_list_filters_are_empty(self):
        # List-shaped multi-value categories with ``[]`` are also
        # no-narrowing. The guardrail treats ``security=[]`` the
        # same as ``security=None`` — both pass through to
        # "rank everything".
        assert is_empty_call(security=[], vulnerabilities=[]) is True

    def test_mixed_none_and_empty_list_is_empty(self):
        # Real-world shape — agent partially named filters with
        # ``None``, partially with ``[]``. Still empty.
        assert (
            is_empty_call(
                kind=None,
                security=[],
                quality=None,
                concerns=[],
            )
            is True
        )

    def test_output_control_only_is_empty(self):
        # Output-control args (``sections`` / ``limit`` / ``commit``)
        # aren't narrowing input — passing them alone still leaves
        # the call with no dimension to narrow on. The model owns
        # the field list, so it correctly distinguishes the two
        # kinds (unlike the pre-refactor ``**kwargs`` helper, which
        # required caller discipline to exclude output-control names
        # and silently broke when new args were added).
        assert (
            is_empty_call(
                sections=["preview", "graph"],
                limit=15,
                commit="abc123",
            )
            is True
        )


class TestEmptyCallNonEmptyShapes:
    """Shapes that should NOT trip the guardrail — real queries."""

    def test_query_text_set_is_not_empty(self):
        # Free-text query → the agent is doing a semantic search.
        # Even with every filter ``None``, this is a real query.
        assert is_empty_call(query_text="auth middleware") is False

    def test_ids_set_is_not_empty(self):
        # By-id lookup is a legitimate narrowing — the agent is
        # asking "give me these specific ids" rather than "rank
        # everything".
        assert is_empty_call(ids=["abc-123", "def-456"]) is False

    def test_single_filter_set_is_not_empty(self):
        # Naming any one filter is enough. Pin all the common
        # categories explicitly so a refactor that miscategorises
        # one (e.g. treats ``priority`` as output-control) trips.
        for filter_name in [
            "kind",
            "security",
            "quality",
            "vulnerabilities",
            "frameworks",
            "complexity",
            "priority",
            "issues",
        ]:
            kwargs = {filter_name: ["something"]}
            assert is_empty_call(**kwargs) is False, (
                f"{filter_name} filter should NOT count as empty"
            )

    def test_str_filter_set_is_not_empty(self):
        # Some filters take a string (file_extension, path_prefix).
        # A non-empty string is narrowing input.
        assert is_empty_call(file_extension="py") is False
        assert is_empty_call(path_prefix="src/auth/") is False


class TestEmptyCallBoolHandling:
    """The ``needs_refactoring`` (bool) filter — important edge case.

    The Pythonic ``if value:`` check would treat ``False`` as
    falsy and re-classify the call as empty. The guard's
    implementation explicitly uses ``value is None`` semantics
    so the user/agent can query for ``needs_refactoring=False``.
    """

    def test_needs_refactoring_true_is_not_empty(self):
        # The common case — narrow to items the BE marked as
        # "needs refactoring".
        assert is_empty_call(needs_refactoring=True) is False

    def test_needs_refactoring_false_is_NOT_empty(self):
        # Load-bearing — ``False`` is the "items that don't need
        # refactoring" query. Unusual but valid; the agent might
        # ask for it during a stability assessment.
        assert is_empty_call(needs_refactoring=False) is False


class TestEmptyCallPrecedence:
    """When BOTH narrowing AND output-control args are present,
    narrowing always wins."""

    def test_query_text_plus_output_args_is_not_empty(self):
        # The agent's healthy shape — narrow with a query and
        # control the output.
        assert (
            is_empty_call(
                query_text="auth",
                sections=["preview"],
                limit=15,
                commit="abc",
            )
            is False
        )

    def test_filter_plus_output_args_is_not_empty(self):
        # Categorical narrowing + output control = valid.
        assert (
            is_empty_call(
                security=["high", "critical"],
                limit=10,
                sections=["graph"],
            )
            is False
        )
