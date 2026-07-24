"""Eval reporter — format eval results for terminal output.

The module used to expose two free functions (``_case_details`` and
``format_results``) that took a domain model as their first argument
and read its attributes to render Markdown. That is exactly the
state-first-arg shape the runner refactor eliminated. The behaviour
now lives on:

    * :class:`CaseResult` — owns :meth:`summary_line`,
      :meth:`status_badges`, and :meth:`failure_detail_lines`.
    * :class:`EvalReport` (this module) — owns ``list[SuiteResult]``
      and renders the full report via :meth:`render`. Totals are
      sourced from :class:`SuiteResult`'s computed properties, not
      re-derived here.

``format_results`` is kept as a thin shim so existing callers
(``backend/schemas_evals.py``, ``scripts/run_codeindex_eval.py``,
``tests/test_evals.py``) keep working without changes. New callers
should use ``EvalReport(results).render()`` directly.
"""

from __future__ import annotations

from ember_code.core.evals.schemas import SuiteResult

__all__ = ["EvalReport", "format_results"]


class EvalReport:
    """Renders a list of :class:`SuiteResult` as a Markdown report.

    Owns the suite list across the render lifetime so we don't thread
    ``results`` through every private helper as a state-first-arg.
    Totals come from :attr:`SuiteResult.passed`, :attr:`failed`,
    :attr:`total`, :attr:`elapsed` — the renderer never re-derives
    them in a local accumulator, which is how the old free-function
    version drifted away from the model's view of the same numbers.
    """

    def __init__(self, results: list[SuiteResult]) -> None:
        self._results = results

    def render(self) -> str:
        """Build the full Markdown report and return it as a single string."""
        lines: list[str] = ["## Eval Results", ""]
        for suite_result in self._results:
            lines.extend(self._suite_lines(suite_result))
            lines.append("")
        lines.extend(self._summary_lines())
        return "\n".join(lines)

    def _suite_lines(self, suite_result: SuiteResult) -> list[str]:
        """Render the header line, every case row, and the suite footer."""
        suite = suite_result.suite
        lines: list[str] = [f"**{suite.agent}** ({suite_result.total} cases)"]
        for case_result in suite_result.case_results:
            lines.append(case_result.summary_line())
            for detail in case_result.failure_detail_lines():
                lines.append(detail)
        lines.append(f"  {suite_result.passed}/{suite_result.total} passed")
        return lines

    def _summary_lines(self) -> list[str]:
        """Render the trailing ``---`` block and the failed-case list.

        Totals are pulled from each suite's computed properties and
        summed here — no parallel counters maintained alongside the
        model.
        """
        total_passed = sum(sr.passed for sr in self._results)
        total_failed = sum(sr.failed for sr in self._results)
        total = total_passed + total_failed
        total_elapsed = sum(sr.elapsed for sr in self._results)

        lines: list[str] = ["---"]
        pct = total_passed * 100 // total if total else 0
        lines.append(f"**Total: {total_passed}/{total} passed ({pct}%) in {total_elapsed:.1f}s**")
        if total_failed:
            failed_names = [
                f"{sr.suite.agent}.{cr.case.name}"
                for sr in self._results
                for cr in sr.case_results
                if not cr.passed
            ]
            lines.append(f"Failed: {', '.join(failed_names)}")
        return lines


def format_results(results: list[SuiteResult]) -> str:
    """Format eval results as a readable report string.

    Thin shim around :class:`EvalReport` for callers that haven't
    migrated yet. Prefer ``EvalReport(results).render()`` in new code.
    """
    return EvalReport(results).render()
