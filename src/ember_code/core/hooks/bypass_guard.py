"""The last check before Bypass mode runs a tool call.

Bypass ("auto-approve every tool, including shell") exists so a person
can let the agent work without answering a prompt per command. It
should still not run ``rm -rf ~`` or pipe ``~/.ssh`` to a paste site
on the person's behalf. This is the layer between those two promises:
when Bypass would allow a call, the call is checked first, and one that
might be harmful is turned back into the ordinary approval prompt. A
yes runs it; a no tells the agent it was refused.

Two layers, because neither is enough alone:

1. **Rules** (:mod:`~ember_code.core.hooks.bypass_rules`) for actions
   that are dangerous by their shape — deleting outside the project,
   ``curl | sh``, force-pushing, writing to disks, sending credentials
   off the machine, persistence, publishing, destroying infrastructure.
   Precise, so they need no probability.
2. **Laya** (``convaiinnovations/laya-typed-decisions``, Apache-2.0, run
   locally like the embedder) for what no rule anticipated: four typed
   questions about the call in one forward pass, ~100 ms on Apple
   silicon. Flagged when the highest probability reaches
   :attr:`BypassGuardConfig.threshold`.

Measured on forty labelled tool calls (twenty harmful, twenty
look-alikes such as ``rm -rf ./build``): the rules caught 19/20 with no
false alarm; Laya alone separated them at AUC ~0.87–0.89. Together at
0.75 they caught 20/20 and asked about 2/20 harmless calls.

**Failing closed.** A check that cannot run — model not loadable, an
error mid-inference — asks rather than allows: a last line of defence
that quietly lets everything through when it breaks is not one. A call
that needs a yes when nobody can give one (headless ``-p``, CI) is
refused with the reason, never left waiting.

Only Bypass is checked. Every other mode already asks for anything not
explicitly allowed, so this would add latency and no protection.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ember_code.core.config.schemas.safety import BypassGuardConfig
from ember_code.core.hooks.bypass_rules import flag_shell, flag_url, flag_write

logger = logging.getLogger(__name__)

#: The fine-tuned decision checkpoint; ``laya`` pins its revision.
LAYA_MODEL = "typed-decisions"

#: Asked about every checked call, in one forward pass. Wording is the
#: one measured; changing it changes the threshold's meaning.
QUESTIONS: dict[str, Any] = {
    "destructive": {
        "type": "noul",
        "instructions": "Would running `tool_call` delete or overwrite data, or make changes that are hard to undo?",
    },
    "exfiltration": {
        "type": "noul",
        "instructions": "Would `tool_call` send secrets, credentials or private data to an outside party?",
    },
    "outside_scope": {
        "type": "noul",
        "instructions": (
            "Does `tool_call` change systems outside the developer's own project, such as production, "
            "cloud resources, other machines or operating-system settings?"
        ),
    },
    "harm_severity": {
        "type": "score",
        "instructions": "How much harm could running `tool_call` cause?",
        "criteria": [
            "none: routine development action",
            "minor: easily undone",
            "serious: data loss or unwanted external change",
            "severe: destructive, irreversible or leaks secrets",
        ],
    },
}

_PLAIN = {
    "destructive": "might delete or overwrite data",
    "exfiltration": "might send secrets or private data elsewhere",
    "outside_scope": "might change systems outside this project",
    "harm_severity": "might cause serious harm",
}

#: Tools that only read or report. Everything else is checked — an
#: unfamiliar tool (an MCP server's, a plugin's) is checked, not trusted.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "list_files",
        "list_directory",
        "grep",
        "glob",
        "search_files",
        "read_process_output",
        "await_process",
        "watch_process",
        "list_processes",
        "loop_status",
        "loop_progress_get",
        "loop_progress_list",
        "monitor_status",
        "monitor_output",
        "list_scheduled_tasks",
        "lsp_query",
        "lsp_list_servers",
        "artifact_search",
        "artifact_read",
        "artifact_list",
        "artifact_versions",
        "artifact_lineage",
        "artifact_ask",
        "artifact_status",
        "artifact_cypher",
        "artifact_stale",
        "artifact_pending_sources",
        "web_search",
        "todo_write",
        "visualize",
        # Delegation is not an action; the sub-agent's own calls are checked.
        "spawn_agent",
        "spawn_team",
    }
)

_SHELL_TOOLS = frozenset({"run_shell_command", "bash", "shell"})
_WRITE_TOOLS = frozenset({"edit_file", "edit_file_replace_all", "create_file", "write_file"})


class Verdict(BaseModel):
    """Why a call needs a yes, or that it does not."""

    needs_approval: bool
    reason: str = ""
    score: float | None = None
    #: True when the reason is the model's judgment rather than a rule's.
    judged: bool = False


Ask = Callable[[str, dict[str, Any], str, list[str]], Awaitable[bool | None]]
"""``(tool_name, args, reason, agent_path) -> True | False | None`` — None means nobody can answer."""


class HarmJudge:
    """Laya, loaded once per process on first use, run off the event loop."""

    _shared: HarmJudge | None = None
    _shared_lock = threading.Lock()

    def __init__(self, model: str = LAYA_MODEL) -> None:
        self._model = model
        self._router: Any = None
        self._lock = threading.Lock()

    @classmethod
    def shared(cls) -> HarmJudge:
        with cls._shared_lock:
            if cls._shared is None:
                cls._shared = cls()
            return cls._shared

    async def score(self, text: str) -> tuple[float, str]:
        """``(highest violation probability, which question)`` for ``text``."""
        return await asyncio.to_thread(self._score_sync, text)

    def _score_sync(self, text: str) -> tuple[float, str]:
        answers = self._load().predict({"tool_call": text}, QUESTIONS)["answers"]
        probabilities = {
            name: float(answers[name]["noul"])
            for name in ("destructive", "exfiltration", "outside_scope")
        }
        levels = answers["harm_severity"].get("probabilities") or {}
        values = list(levels.values()) if isinstance(levels, dict) else list(levels)
        if len(values) == 4:
            probabilities["harm_severity"] = float(values[2]) + float(values[3])
        worst = max(probabilities, key=probabilities.__getitem__)
        return probabilities[worst], worst

    def _load(self) -> Any:
        if self._router is not None:
            return self._router
        with self._lock:
            if self._router is None:
                self._router = self._build()
        return self._router

    def _build(self) -> Any:
        """The router, with download chatter kept off stdout.

        Same reason as :class:`~ember_code.core.embeddings.Embedder`:
        stdout is the backend's handshake pipe, and a progress bar on it
        corrupts the READY line. Tried offline first when the bundle is
        cached, so a warm load does not wait on a network probe; a cached
        bundle need not hold this checkpoint's subfolder, so a failed
        offline load falls back to fetching it.
        """
        from laya import Router  # noqa: PLC0415 — torch-heavy, load on first use
        from laya.router import BUNDLE_REPO  # noqa: PLC0415

        from ember_code.core.embeddings import Embedder  # noqa: PLC0415

        if os.environ.get("HF_HUB_OFFLINE") is None and Embedder.is_model_cached(BUNDLE_REPO):
            os.environ["HF_HUB_OFFLINE"] = "1"
            try:
                return self._quietly(lambda: Router(preload=True, default=self._model))
            except Exception as exc:  # noqa: BLE001 — retried online below
                logger.debug("laya offline load failed (%s); fetching", type(exc).__name__)
            finally:
                del os.environ["HF_HUB_OFFLINE"]
        return self._quietly(lambda: Router(preload=True, default=self._model))

    @staticmethod
    def _quietly(build: Callable[[], Any]) -> Any:
        devnull = os.open(os.devnull, os.O_WRONLY)
        saved = os.dup(1)
        bars = os.environ.get("HF_HUB_DISABLE_PROGRESS_BARS")
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        try:
            os.dup2(devnull, 1)
            with contextlib.redirect_stdout(io.StringIO()):
                return build()
        finally:
            os.dup2(saved, 1)
            os.close(saved)
            os.close(devnull)
            if bars is None:
                del os.environ["HF_HUB_DISABLE_PROGRESS_BARS"]
            else:
                os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = bars


class BypassGuard:
    """Decides whether a Bypass-allowed call runs, or is put to the person first."""

    def __init__(
        self,
        *,
        config: BypassGuardConfig,
        project_dir: Path | None,
        in_bypass: Callable[[], bool],
        ask: Ask | None,
        judge: HarmJudge | None = None,
    ) -> None:
        self._config = config
        self._project_dir = project_dir
        self._in_bypass = in_bypass
        self._ask = ask
        self._judge = (
            judge if judge is not None else (HarmJudge.shared() if config.use_model else None)
        )

    async def check(
        self, tool_name: str, args: dict[str, Any], agent_path: list[str] | None = None
    ) -> str | None:
        """A block message when the call must not run, otherwise None."""
        if not self._config.enabled or not self._in_bypass() or tool_name in READ_ONLY_TOOLS:
            return None
        verdict = await self.verdict(tool_name, args)
        if not verdict.needs_approval:
            return None

        answer = (
            await self._ask(tool_name, args, explain(tool_name, verdict), list(agent_path or []))
            if self._ask
            else None
        )
        if answer is True:
            logger.info("bypass guard: %s approved by the user (%s)", tool_name, verdict.reason)
            return None
        if answer is None:
            logger.warning(
                "bypass guard: %s refused, nobody to ask (%s)", tool_name, verdict.reason
            )
            return (
                f"Blocked: this call {verdict.reason}, and Bypass mode puts a call like that to a "
                "person first — but this session has nobody to ask. Run it outside Bypass, or "
                "allow it explicitly in settings."
            )
        logger.info("bypass guard: %s declined by the user (%s)", tool_name, verdict.reason)
        return (
            f"Blocked: the user declined this call ({verdict.reason}). Do not retry it another way."
        )

    async def verdict(self, tool_name: str, args: dict[str, Any]) -> Verdict:
        """Rules first; Laya for what no rule names; ask on any failure to judge."""
        rule = self._rule(tool_name, args)
        if rule:
            return Verdict(needs_approval=True, reason=rule)
        if self._judge is None:
            return Verdict(needs_approval=False)
        try:
            probability, question = await self._judge.score(_describe(tool_name, args))
        except Exception as exc:  # noqa: BLE001 — fail closed, whatever went wrong
            logger.warning(
                "bypass guard: could not judge %s (%s); asking", tool_name, type(exc).__name__
            )
            return Verdict(needs_approval=True, reason="could not be checked for safety")
        if probability >= self._config.threshold:
            return Verdict(
                needs_approval=True,
                reason=_PLAIN[question],
                score=probability,
                judged=True,
            )
        return Verdict(needs_approval=False, score=probability)

    def _rule(self, tool_name: str, args: dict[str, Any]) -> str | None:
        if tool_name in _SHELL_TOOLS:
            return flag_shell(str(args.get("command") or args.get("cmd") or ""), self._project_dir)
        if tool_name in _WRITE_TOOLS:
            return flag_write(
                str(args.get("path") or args.get("file_path") or ""), self._project_dir
            )
        if tool_name in ("fetch_url", "fetch_json"):
            return flag_url(str(args.get("url") or ""))
        return None


def explain(tool_name: str, verdict: Verdict) -> str:
    """What the person reads in the dialog: what the call does, who flagged it, and why it stopped.

    The bare reason ("force-pushes, rewriting shared history") under
    "Requested by safety check" left them to work out that Bypass had
    paused on purpose and on what grounds.
    """
    if tool_name in _SHELL_TOOLS:
        subject = "This command"
    elif tool_name in _WRITE_TOOLS:
        subject = "This file write"
    elif tool_name in ("fetch_url", "fetch_json"):
        subject = "This request"
    else:
        subject = "This call"
    if verdict.judged and verdict.score is not None:
        grounds = (
            f"{subject} {verdict.reason}, in the safety model's judgment "
            f"(risk score {verdict.score:.2f} of 1)."
        )
    else:
        grounds = f"{subject} {verdict.reason}."
    return f"{grounds} Bypass mode skips approvals, but the safety check stops here so you decide."


def _describe(tool_name: str, args: dict[str, Any]) -> str:
    """The call as Laya was measured on it: ``Tool: argument``."""
    if tool_name in _SHELL_TOOLS:
        return f"Bash: {args.get('command') or args.get('cmd') or ''}"
    if tool_name in _WRITE_TOOLS:
        content = str(args.get("content") or args.get("new_string") or "")[:400]
        return f"Write: {args.get('path') or args.get('file_path') or ''}  content: {content!r}"
    if tool_name in ("fetch_url", "fetch_json"):
        return f"WebFetch: {args.get('url') or ''}"
    return f"{tool_name}: {json.dumps(args, default=str)[:1500]}"
