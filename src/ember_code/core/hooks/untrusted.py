"""Mark tool output as untrusted data — not instructions (prompt-injection defense).

Every tool result the model sees is wrapped in a delimiter carrying a random
per-result nonce. The model, trained on the same convention in the SFT corpus,
treats everything inside as data and never acts on instructions found there; the
nonce means content inside the block cannot forge a convincing closing tag to
"break out" and have its trailing text read as a command.

Trusted, igni-generated annotations (a PostToolUse hook message, a rules-index
suffix) are appended OUTSIDE the block — they are not tool output and must not be
quarantined with it.

**This format is a train/serve contract.** It must stay byte-identical to the
corpus rewrite in the fine-tuning repo
(``igni__fine-tuning/finetune/tools/wrap_tool_output_untrusted.py``); if the two
drift, the model sees a delimiter at train time it never sees at run time (or vice
versa) and the signal is lost. Change them together.
"""

from __future__ import annotations

import inspect
import re
import secrets
from collections.abc import Callable
from typing import Any

from ember_code.core.hooks.queue.agno_adapter import AgnoCallableAdapter

#: Tools whose result is external / attacker-influenceable content the model
#: reads — a file's bytes, a web page, a shell command's stdout, a sub-agent's
#: findings. These get wrapped. Provenance is irrelevant: a base tool and an
#: EXTENSION-provided tool that both return outside content are wrapped the same
#: way — hence ``codeindex_cypher`` (graph/repo rows) and the artifact readers
#: below. igni's OWN control tools are deliberately NOT here: ``enter_plan_mode``
#: returns "investigate, then call exit_plan_mode", ``todo_write`` /
#: ``schedule_task`` / the loop tools return confirmations the model is MEANT to
#: act on — quarantining those as untrusted would break legitimate control flow.
#: Keep this set identical to the corpus rewrite's.
UNTRUSTED_OUTPUT_TOOLS: frozenset[str] = frozenset(
    {
        # base tools
        "read_file",
        "search_files",
        "grep",
        "glob",
        "list_files",
        "list_directory",
        "run_shell_command",
        "bash",
        "shell",
        "fetch_url",
        "fetch_json",
        "web_search",
        "spawn_agent",
        "spawn_team",
        "read_process_output",
        "await_process",
        "watch_process",
        # extension-provided content tools (CodeIndex / Artifacts): they return
        # graph rows, repo text and other-authored artifact bodies — the same
        # untrusted surface, so they wrap the same way when the extension runs.
        "codeindex_cypher",
        "artifact_read",
        "artifact_search",
        "artifact_lineage",
        "artifact_versions",
    }
)


def should_wrap(tool_name: str) -> bool:
    """True when ``tool_name``'s result is untrusted external content."""
    return tool_name in UNTRUSTED_OUTPUT_TOOLS


_OPEN = "<untrusted_tool_output nonce={nonce}>"
_CLOSE = "</untrusted_tool_output nonce={nonce}>"
#: Matches an already-wrapped payload so wrapping is idempotent (a result that
#: passed through here or was authored wrapped is not double-wrapped).
_ALREADY = re.compile(r"^<untrusted_tool_output nonce=[0-9a-f]+>\n")


def new_nonce() -> str:
    """A fresh, unpredictable nonce for one tool result."""
    return secrets.token_hex(4)


def wrap_untrusted(text: str, nonce: str | None = None) -> str:
    """Wrap ``text`` as untrusted tool output. Idempotent.

    Non-str callers should not reach here; the seam wraps only string results
    (the ones the model reads as text), matching the corpus rewrite.
    """
    if _ALREADY.match(text):
        return text
    n = nonce or new_nonce()
    return f"{_OPEN.format(nonce=n)}\n{text}\n{_CLOSE.format(nonce=n)}"


class UntrustedOutputHook(AgnoCallableAdapter):
    """Agno tool hook: an external-content tool's text result, wrapped.

    The main agent's :class:`~ember_code.core.hooks.tool_hook.ToolEventHook`
    wraps inside its own pipeline (before a PostToolUse hook message or a
    rules suffix is appended, so those land outside the block). Every other
    agent igni runs — a pool agent, so whatever ``spawn_agent`` /
    ``spawn_team`` starts at any depth — carries this hook instead. Both go
    through :meth:`quarantine`, so the two cannot wrap differently.

    Before this existed only the main agent wrapped: a sub-agent's shell
    output reached the model raw, outside the train/serve contract and
    outside the prompt-injection defence.
    """

    def __init__(self) -> None:
        self._mark_as_coroutine()

    async def __call__(
        self,
        name: str = "",
        func: Callable[..., Any] | None = None,
        args: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        # Parameter names are Agno's (see AgnoCallableAdapter).
        result = func(**(args or {})) if func is not None else None
        if inspect.isawaitable(result):
            result = await result
        return self.quarantine(name, result)

    @staticmethod
    def quarantine(tool_name: str, result: Any) -> Any:
        """``result`` as the model may read it: wrapped when it is the text
        of a tool whose output is external content, unchanged otherwise."""
        if isinstance(result, str) and should_wrap(tool_name):
            return wrap_untrusted(result)
        return result

    @classmethod
    def on(cls, hooks: list[Any] | None) -> list[Any]:
        """``hooks`` with this hook outermost — a new list, never the one
        passed (a spawn's agent is a shallow copy of the pool's, and must
        not add to the pool's list). Idempotent."""
        existing = list(hooks or [])
        if any(isinstance(hook, cls) for hook in existing):
            return existing
        return [cls(), *existing]
