"""Discover subdirectory rules for the paths a tool just touched
and suffix them onto a string tool result.

Extracted from ``ToolEventHook._maybe_suffix_rules`` — a single
55-line method that mixed three concerns:

1. Harvest candidate paths from the tool's args (any of
   ``file_path`` / ``path`` / ``filename`` / ``directory`` /
   ``dir``).
2. Consume the rules index for each path, collecting the newly-
   surfaced rules files.
3. Append a ``<discovered-rules>`` XML block to the result string
   AND fire ``InstructionsLoaded`` for observability.

Now each concern lives inside :class:`RulesSuffixer`. Non-string
tool results (binary returns, structured dicts) pass through
untouched, so the class is safe to install unconditionally.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.hook_firer import HookFirer
from ember_code.core.hooks.permission_pipeline import ToolCallContext
from ember_code.core.hooks.tool_events import InstructionsLoadedPayload
from ember_code.core.utils.rules_index import RulesIndex


class RulesSuffixer:
    """Post-process a tool result by appending any newly-discovered
    subdirectory rules for paths in the tool's args.

    Composition: takes a :class:`RulesIndex` + project dir +
    :class:`HookFirer`. All state is instance state — no module-
    level mutables.
    """

    #: Argument names from which we harvest a path to consult the
    #: rules index after a successful tool call. Covers every
    #: file-tool entrypoint in the toolkit (read / edit / save /
    #: create / list-dir / grep) without needing per-tool wiring.
    PATH_ARG_NAMES: tuple[str, ...] = (
        "file_path",
        "path",
        "filename",
        "directory",
        "dir",
    )

    def __init__(
        self,
        rules_index: RulesIndex | None,
        project_dir: Path | None,
        firer: HookFirer,
    ) -> None:
        self._rules_index = rules_index
        self._project_dir = project_dir
        self._firer = firer

    async def enrich(self, ctx: ToolCallContext, result: Any) -> Any:
        """Return ``result`` with any discovered-rules block
        appended. Non-string results and no-rules-index pass
        through untouched.
        """
        if self._rules_index is None or not isinstance(result, str):
            return result
        candidate_paths = self._candidate_paths(ctx.args)
        if not candidate_paths:
            return result
        discovered = self._discover(candidate_paths)
        if not discovered:
            return result
        return await self._emit(result, discovered)

    def _candidate_paths(self, args: dict[str, Any]) -> list[Path]:
        """Harvest absolute paths from ``args`` — relative paths
        are resolved against the project dir when one is
        configured."""
        candidates: list[Path] = []
        for key in self.PATH_ARG_NAMES:
            v = args.get(key)
            if not isinstance(v, str) or not v:
                continue
            p = Path(v)
            if not p.is_absolute() and self._project_dir is not None:
                p = self._project_dir / p
            candidates.append(p)
        return candidates

    def _discover(self, candidate_paths: list[Path]) -> list[tuple[Path, str]]:
        """Consume the rules index for each path — dedup is the
        index's job (repeat consumes return empty)."""
        assert self._rules_index is not None  # guarded by ``enrich``
        discovered: list[tuple[Path, str]] = []
        for p in candidate_paths:
            discovered.extend(self._rules_index.consume_path(p))
        return discovered

    async def _emit(self, result: str, discovered: list[tuple[Path, str]]) -> str:
        """Build the ``<discovered-rules>`` block, fire
        ``InstructionsLoaded``, and return the suffixed result.

        The event fires BEFORE the return so observers see the
        payload even if the caller drops the result (defensive
        parity with the pre-refactor sequencing).
        """
        parts: list[str] = []
        files_payload: list[str] = []
        total_bytes = 0
        for rules_path, content in discovered:
            label: Path | str = rules_path
            if self._project_dir is not None:
                with contextlib.suppress(ValueError):
                    label = rules_path.relative_to(self._project_dir)
            files_payload.append(str(label))
            total_bytes += len(content.encode("utf-8"))
            parts.append(
                f'<discovered-rules path="{label}">\n{content.strip()}\n</discovered-rules>'
            )
        await self._firer.fire(
            HookEvent.INSTRUCTIONS_LOADED,
            "",
            InstructionsLoadedPayload(
                source="rules_index",
                files=files_payload,
                bytes=total_bytes,
            ),
        )
        block = "\n".join(parts)
        return f"{result}\n\n{block}"
