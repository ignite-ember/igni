"""Agno history scanner: locate the most recent ``exit_plan_mode`` call.

Extracted from :mod:`ember_code.backend.server_rehydrate` where the
40-line reverse walk over ``session.runs → messages → tool_calls``
lived inline as an untyped-dict expedition. The scanner absorbs
the ``isinstance`` / ``json.loads`` / attribute-probe mess once at
the Agno boundary and hands back a typed :class:`PlanArgs` model so
:meth:`RehydrateController.plan_store` can stay a short applier.

Sibling to :mod:`server_history_walker`, which carries similar
walking logic over the same Agno session shape for a different
purpose (chat history rebuild).

Design notes:

* The constructor takes the Agno session as ``Any`` (not a typed
  Agno import) — Agno's per-version shape drift is exactly what
  this class exists to hide, so leaking that type into
  ``ember_code`` schemas would defeat the point.
* :class:`PlanArgs` is a small local Pydantic schema. ``tasks``
  stays ``list[dict] | None`` because downstream code
  (:func:`ember_code.core.tools.todo._coerce_items`) does the
  per-item validation — re-declaring the todo shape here would
  duplicate that contract.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PlanArgs(BaseModel):
    """Typed view of one ``exit_plan_mode`` tool call's arguments.

    Only the two fields consumed by :class:`PlanStore` /
    :class:`TodoStore` are surfaced; unknown extras from Agno's
    tool-call blob are ignored on parse.
    """

    plan: str
    tasks: list[dict] | None = None


class AgnoHistoryPlanScanner:
    """Reverse-scan an Agno session for the most recent plan submission.

    Constructed with the Agno session returned by
    :meth:`Agent.aget_session`. :meth:`find_latest_plan` walks
    ``runs → messages → tool_calls`` in reverse and returns the
    first ``exit_plan_mode`` call whose arguments parse into a
    :class:`PlanArgs` with a non-empty ``plan`` string.
    """

    def __init__(self, agno_session: Any) -> None:
        self._agno_session = agno_session

    def find_latest_plan(self) -> tuple[PlanArgs, str] | None:
        """Return ``(plan_args, run_id)`` for the most recent plan
        submission, or ``None`` when no matching tool call exists.

        ``run_id`` is surfaced alongside the args so the caller can
        log which run the seed came from without a second walk.
        """
        runs = getattr(self._agno_session, "runs", None) or []
        for run in reversed(runs):
            messages = getattr(run, "messages", None) or []
            for m in reversed(messages):
                if getattr(m, "role", "") != "assistant":
                    continue
                tool_calls = getattr(m, "tool_calls", None) or []
                for tc in tool_calls:
                    parsed = self._parse_tool_call(tc)
                    if parsed is None:
                        continue
                    return parsed, str(getattr(run, "run_id", ""))
        return None

    def _parse_tool_call(self, tc: Any) -> PlanArgs | None:
        """Return :class:`PlanArgs` iff ``tc`` is an ``exit_plan_mode``
        call with a non-empty plan string; otherwise ``None``.

        Handles the two shapes Agno emits for ``arguments`` (JSON
        string or already-decoded dict) and the range of validation
        failure modes as a single ``return None``.
        """
        if not isinstance(tc, dict):
            return None
        fn = tc.get("function") or {}
        if not isinstance(fn, dict) or fn.get("name") != "exit_plan_mode":
            return None
        args_raw = fn.get("arguments")
        if isinstance(args_raw, str):
            try:
                decoded = json.loads(args_raw)
            except (ValueError, TypeError):
                return None
        elif isinstance(args_raw, dict):
            decoded = args_raw
        else:
            return None
        if not isinstance(decoded, dict):
            return None
        plan_text = str(decoded.get("plan", "")).strip()
        if not plan_text:
            return None
        tasks_raw = decoded.get("tasks")
        tasks: list[dict] | None = tasks_raw if isinstance(tasks_raw, list) else None
        return PlanArgs(plan=plan_text, tasks=tasks)
