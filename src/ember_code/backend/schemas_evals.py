"""Typed view model for the ``/evals`` slash command's chat output.

Extracted from :mod:`ember_code.backend.command_handler` — the
old ``_cmd_evals`` inline body did the SuiteRunner-empty branch
inline. :class:`EvalRunView` wraps the results list; empty flows
through :meth:`to_command_result` as an info result, non-empty
renders via :class:`ember_code.core.evals.reporter.EvalReport`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from ember_code.backend.command_result import CommandResult
from ember_code.core.evals.reporter import EvalReport

if TYPE_CHECKING:
    pass


class EvalRunView(BaseModel):
    """Wraps the ``list`` returned by :meth:`SuiteRunner.run_all`.

    ``results`` is ``list[Any]`` on the model — the reporter
    consumes duck-typed objects; we only need to forward them.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    results: list[Any]

    def to_command_result(self) -> CommandResult:
        if not self.results:
            return CommandResult.info("No eval suites found. Add YAML files to .ember/evals/")
        return CommandResult.markdown(EvalReport(self.results).render())


__all__ = ["EvalRunView"]
