"""Backend server — processes FE messages and streams BE events.

Owns the Session object and all Agno/AI logic. The FE never touches
Session directly — everything goes through protocol messages.

In Phase 2 (single-process), this is called in-process by the TUI.
In Phase 4 (multi-process), this runs as a separate process with
socket transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ember_code.protocol import messages as msg
from ember_code.protocol.serializer import serialize_event

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings
    from ember_code.core.plugins.models import MarketplaceInfo, PluginInfo
    from ember_code.core.pool import AgentInfo
    from ember_code.core.skills.parser import SkillInfo

logger = logging.getLogger(__name__)


def _is_within(child: Path, root: Path) -> bool:
    """True iff ``child`` (already resolved) sits under ``root``."""
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


_LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".markdown": "markdown",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
}


def _guess_language(suffix: str) -> str:
    return _LANG_BY_EXT.get(suffix.lower(), "")


def _scan_plugin_dir(root: Path, *, name: str) -> dict:
    """Walk *root* and pull out the bundled-contents inventory: skills,
    agents, hooks, MCP servers, custom tools, plus a README excerpt.

    Shared between :meth:`BackendServer.get_plugin_contents` (installed
    plugins) and :meth:`BackendServer.preview_plugin` (uninstalled
    catalog entries, scanned from a shallow clone). Pure on the
    filesystem — no plugin loader / session state needed.
    """
    import json

    result: dict = {
        "name": name,
        "root_path": str(root),
        "skills": [],
        "agents": [],
        "hooks": [],
        "mcp_servers": [],
        "tools": [],
        "readme": "",
    }

    def _frontmatter_field(md_text: str, field: str) -> str:
        if not md_text.startswith("---"):
            return ""
        end = md_text.find("\n---", 4)
        if end <= 0:
            return ""
        for line in md_text[4:end].splitlines():
            if line.lower().startswith(f"{field}:"):
                return line.split(":", 1)[1].strip().strip('"')
        return ""

    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for sd in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            skill_md = sd / "SKILL.md"
            desc = ""
            if skill_md.is_file():
                with contextlib.suppress(OSError):
                    desc = _frontmatter_field(skill_md.read_text(errors="replace"), "description")
            result["skills"].append({"name": sd.name, "description": desc})

    agents_dir = root / "agents"
    if agents_dir.is_dir():
        for af in sorted(agents_dir.glob("*.md")):
            desc = ""
            with contextlib.suppress(OSError):
                desc = _frontmatter_field(af.read_text(errors="replace"), "description")
            result["agents"].append({"name": af.stem, "description": desc})

    hooks_json = root / "hooks" / "hooks.json"
    if hooks_json.is_file():
        try:
            data = json.loads(hooks_json.read_text())
            for event, handlers in (data.get("hooks") or {}).items():
                if isinstance(handlers, list):
                    result["hooks"].append({"event": event, "count": len(handlers)})
        except (OSError, json.JSONDecodeError):
            pass

    for mcp_name in (".mcp.json", "mcp.json"):
        mcp_path = root / mcp_name
        if mcp_path.is_file():
            try:
                data = json.loads(mcp_path.read_text())
                for srv_name, cfg in (data.get("mcpServers") or {}).items():
                    result["mcp_servers"].append(
                        {
                            "name": srv_name,
                            "transport": cfg.get("type", "stdio"),
                            "command": cfg.get("command") or cfg.get("url") or "",
                        }
                    )
            except (OSError, json.JSONDecodeError):
                pass
            break

    tools_dir = root / "tools"
    if tools_dir.is_dir():
        for tf in sorted(tools_dir.glob("*.py")):
            if tf.name.startswith("_"):
                continue
            result["tools"].append({"name": tf.stem})

    # README — capped generously so even long docs render in full
    # but a runaway file can't blow up the wire. Plugin READMEs
    # in the wild top out well under this.
    README_CAP = 200_000
    for readme_name in ("README.md", "Readme.md", "readme.md"):
        rp = root / readme_name
        if rp.is_file():
            try:
                text = rp.read_text(errors="replace")
                if len(text) > README_CAP:
                    result["readme"] = (
                        text[:README_CAP]
                        + "\n\n_…README truncated — open the source repo for the rest._"
                    )
                else:
                    result["readme"] = text
            except OSError:
                pass
            break

    return result


# Cap on the in-process search_code cache. Keyed by
# (project_root, max_results, snippet) — a few dozen entries is plenty
# for normal usage and the entries themselves are small JSON-shaped
# dicts. Old entries fall off in insertion order (Python dicts preserve
# insertion order, so a pop + re-set bumps to MRU).
_SEARCH_CODE_CACHE_MAX = 64


def _search_code_cache_put(cache: dict, key: str, value: dict) -> None:
    cache[key] = value
    while len(cache) > _SEARCH_CODE_CACHE_MAX:
        cache.pop(next(iter(cache)))


# Width on either side of a match for the snippet we ship to the FE.
# Generous enough for the user to see context but tight enough to
# keep the search-results dropdown skimmable.
_SEARCH_CHAT_SNIPPET_HALF_WIDTH = 80


def _search_history(history: list[dict], needle: str, limit: int) -> list[dict]:
    """Substring scan over a get_chat_history result.

    Extracted from BackendServer so it can be unit-tested without
    spinning up an Agno session.
    """
    needle_lower = needle.lower()
    needle_len = len(needle)
    if needle_len == 0:
        # Defense in depth — caller already strips, but ``find("")``
        # returns 0 for every string and would emit a match for every
        # turn. Empty query → no matches.
        return []
    matches: list[dict] = []
    for idx, turn in enumerate(history):
        content = turn.get("content")
        if not isinstance(content, str) or not content:
            continue
        pos = content.lower().find(needle_lower)
        if pos < 0:
            continue
        start = max(0, pos - _SEARCH_CHAT_SNIPPET_HALF_WIDTH)
        end = min(len(content), pos + needle_len + _SEARCH_CHAT_SNIPPET_HALF_WIDTH)
        snippet = content[start:end]
        leading_ellipsis = "…" if start > 0 else ""
        trailing_ellipsis = "…" if end < len(content) else ""
        snippet = f"{leading_ellipsis}{snippet}{trailing_ellipsis}"
        # Position of the match within the snippet string (not the
        # original content) — keeps the FE highlight logic trivial.
        match_start = (pos - start) + len(leading_ellipsis)
        matches.append(
            {
                "history_index": idx,
                "role": str(turn.get("role") or ""),
                "run_id": str(turn.get("run_id") or ""),
                "snippet": snippet,
                "match_start": match_start,
                "match_end": match_start + needle_len,
                # Epoch seconds (Agno-issued) — the FE formats it into
                # a relative "2h ago" / locale time string per row.
                "created_at": int(turn.get("created_at") or 0),
            }
        )
        if len(matches) >= limit:
            break
    return matches


# Inline ``<think>...</think>`` block — many models emit reasoning
# in the assistant content with these tags instead of Agno's
# ``reasoning_content`` field. The trailing ``|$`` allows a final
# unclosed block (cancelled run) to be captured up to end-of-content.
_THINK_BLOCK_RE = re.compile(r"<think>([\s\S]*?)(?:</think>|$)")


def _split_assistant_content_for_restore(content: str) -> list[tuple[str, str]]:
    """Split an assistant message's content into interleaved
    ``(role, text)`` segments, where ``role`` is ``"thinking"`` for
    ``<think>...</think>`` blocks and ``"assistant"`` for everything
    else. Preserves order so the rebuilt chat reads the same as the
    live stream.

    Returns ``[]`` when content has only whitespace / empty think
    blocks (degenerate runs); the caller should emit nothing then.
    """
    if "<think>" not in content:
        stripped = content.strip()
        return [("assistant", stripped)] if stripped else []
    parts: list[tuple[str, str]] = []
    cursor = 0
    for match in _THINK_BLOCK_RE.finditer(content):
        before = content[cursor : match.start()].strip()
        if before:
            parts.append(("assistant", before))
        thinking = match.group(1).strip()
        if thinking:
            parts.append(("thinking", thinking))
        cursor = match.end()
    trailing = content[cursor:].strip()
    if trailing:
        parts.append(("assistant", trailing))
    return parts


def _format_tool_args_for_restore(args: Any) -> str:
    """One-line argument summary for restored tool cards.

    Matches the live ``args_summary`` shape: ``key=value`` pairs joined
    by spaces, with long values truncated. Strings are shown raw (not
    JSON-quoted) so a shell ``command="ls -la"`` reads like a command,
    not like JSON.
    """
    if isinstance(args, dict):
        parts: list[str] = []
        for k, v in args.items():
            if isinstance(v, str):
                v_str = v if len(v) <= 80 else v[:77] + "..."
            elif isinstance(v, (int, float, bool)) or v is None:
                v_str = str(v)
            else:
                try:
                    v_str = json.dumps(v, separators=(",", ":"))
                except Exception:
                    v_str = str(v)
                if len(v_str) > 80:
                    v_str = v_str[:77] + "..."
            parts.append(f"{k}={v_str}")
        return " ".join(parts)
    if isinstance(args, list):
        try:
            return json.dumps(args, separators=(",", ":"))
        except Exception:
            return str(args)
    return str(args)


class BackendServer:
    """Wraps Session and handles all FE→BE protocol messages."""

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
    ):
        from ember_code.core.code_index.paths import state_db_path
        from ember_code.core.session import Session
        from ember_code.core.session.session_preferences import SessionPreferencesStore

        # Per-session prefs need to be consulted BEFORE the Session
        # builds its main team, since the team binds whatever model
        # is in ``settings.models.default`` at construction time. The
        # store lives in the project-local ``state.db``, so we have
        # to know the project_dir up front — fall back to cwd to
        # mirror what Session does internally.
        resolved_project_dir = project_dir or Path.cwd()
        self._session_prefs = SessionPreferencesStore(
            state_db_path(resolved_project_dir, data_dir=settings.storage.data_dir),
        )
        if resume_session_id:
            persisted_model = self._session_prefs.get_model(resume_session_id)
            if persisted_model and persisted_model in settings.models.registry:
                settings.models.default = persisted_model

        self._session = Session(
            settings,
            project_dir=project_dir,
            resume_session_id=resume_session_id,
            additional_dirs=additional_dirs,
        )
        self._settings = settings
        self._pending_requirements: dict[str, Any] = {}  # requirement_id → Agno requirement
        # Auto-resolved requirements waiting to merge into the next
        # ``acontinue_run`` call. Populated by ``_handle_pause`` when
        # the permission evaluator (plan / acceptEdits / bypass / deny
        # rules) decides a paused tool BEFORE the user is asked.
        # ``resolve_hitl_batch`` drains the bucket for the same run_id
        # and includes them alongside the user-resolved reqs so Agno
        # gets the full resolution set in one resume.
        self._auto_resolved_requirements: dict[str, list[Any]] = {}
        self._processing = False
        self._current_team: Any = None  # held during HITL pause
        # Task currently iterating run_message → tool calls → events.
        # ``cancel_run`` calls ``.cancel()`` on this to bail out of any
        # awaits — the most reliable way to stop a streamed run when
        # Agno's cooperative cancellation doesn't propagate (e.g. a
        # broadcast sub-agent is mid-tool-call). Set when iteration
        # starts, cleared in ``finally``.
        self._current_run_task: asyncio.Task | None = None
        # Serialises concurrent ``run_message`` calls. The FE unblocks
        # user input on ``StreamingDone`` (emitted when Agno's content
        # stream ends) but the previous run's Agno tail —
        # compression, memory/learning extraction, final persistence —
        # is still draining. Two ``team.arun()`` calls in flight on the
        # same Agno team would race on session/memory state, so the
        # lock makes the second call wait silently until the previous
        # tail finishes. From the user's POV the second submit just
        # shows the normal "Thinking" UI for a beat longer than usual.
        self._run_lock = asyncio.Lock()
        # Set during ``startup`` if the resumed session's last run
        # had ``status=running`` — i.e. the previous process crashed
        # mid-chain. The next ``run_message`` injects a system note
        # so the agent knows it was interrupted and can decide
        # whether to recap, retry, or pick up where it left off.
        # Cleared after the note is consumed (one-shot per launch).
        self._interrupted_run_summary: str | None = None
        # Pending-message ids surfaced on the next ``--continue``
        # boot. Kept alive in the store (not discarded by
        # ``_detect_interrupted_run``) so the FE can fetch them via
        # ``get_pending_messages`` and render the interrupted prompt
        # as a chat-history entry. Cleared from the store after the
        # next ``run_message`` consumes the summary.
        self._pending_message_ids_to_drop: list[str] = []
        # Latched from RunCompleted.input_tokens for the live ctx
        # footer — kept on the Session so /clear (which goes through
        # CommandHandler with only a session ref) can reset it.
        self._session._last_input_tokens = 0
        # Pre-persist user messages BEFORE handing them to Agno.
        # Agno's streaming runs don't write to disk until the run
        # completes, so a kill mid-stream loses the user's prompt
        # entirely — the partial response, sure, but also the
        # question they asked. The store lives in the same
        # state.db file Agno uses; the table is created on first
        # touch via ``CREATE TABLE IF NOT EXISTS``.
        from ember_code.core.session.pending_messages import PendingMessageStore

        self._pending_store = PendingMessageStore(
            state_db_path(
                self._session.project_dir,
                data_dir=settings.storage.data_dir,
            ),
        )

    # No .session property — all access goes through backend methods

    @property
    def project_dir(self) -> Path:
        return self._session.project_dir

    async def startup(self) -> None:
        """Async post-construction hook.

        ``Session.__init__`` is sync (lots of synchronous wiring) but
        a few subsystems need an awaited initialization step. Right
        now this hydrates the persisted ``/loop`` state — if the CLI
        was killed mid-loop, the prompt + counters are restored from
        ``state.db`` so the panel reflects the interrupted run.

        Also probes the resumed session for an in-flight run that
        never reached ``status=completed`` — that's the signature of
        a crash mid-chain. When detected, a one-shot summary is
        stashed so the next ``run_message`` can hand it to the
        agent as system context.
        """
        await self._session.load_persisted_loop_state()
        await self._detect_interrupted_run()
        await self._rehydrate_plan_store()

    async def _rehydrate_plan_store(self) -> None:
        """Repopulate ``session.plan_store`` from the persisted history.

        ``PlanStore`` is in-memory only — submitted via the agent's
        ``exit_plan_mode`` tool, never written to its own table.
        On BE restart the store is empty even when the previous run
        clearly produced a plan. We walk the Agno session for the
        most recent ``exit_plan_mode`` tool call and pull its
        ``plan`` / ``tasks`` arguments back into the live stores, so
        the FE's restore path sees the same PlanCard it did before
        close.

        Best-effort: any parse error / missing session / unexpected
        shape is swallowed and leaves the stores empty (no plan
        renders on restore, same as a fresh session).
        """
        store = getattr(self._session, "plan_store", None)
        if store is None or store.latest:
            return  # nothing to do (already populated or absent)
        try:
            agent = self._session.main_team
            agno_session = await agent.aget_session(
                session_id=self._session.session_id,
                user_id=self._session.user_id,
            )
        except Exception as exc:
            logger.debug("plan rehydrate: aget_session failed: %s", exc)
            return
        if agno_session is None:
            return
        runs = getattr(agno_session, "runs", None) or []
        for run in reversed(runs):
            messages = getattr(run, "messages", None) or []
            for m in reversed(messages):
                if getattr(m, "role", "") != "assistant":
                    continue
                tool_calls = getattr(m, "tool_calls", None) or []
                for tc in tool_calls:
                    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                    if fn.get("name") != "exit_plan_mode":
                        continue
                    args_raw = fn.get("arguments")
                    if isinstance(args_raw, str):
                        try:
                            args = json.loads(args_raw)
                        except Exception:
                            continue
                    elif isinstance(args_raw, dict):
                        args = args_raw
                    else:
                        continue
                    plan_text = str(args.get("plan", "")).strip()
                    if not plan_text:
                        continue
                    store.set_plan(plan_text)
                    tasks_raw = args.get("tasks")
                    todo = getattr(self._session, "todo_store", None)
                    if todo is not None and isinstance(tasks_raw, list):
                        try:
                            from ember_code.core.tools.todo import _coerce_items

                            items, _errs = _coerce_items(tasks_raw)
                            if items:
                                todo.set(items)
                        except Exception as exc:
                            logger.debug("plan rehydrate: todo coerce failed: %s", exc)
                    logger.info(
                        "Rehydrated plan_store from history (run_id=%s, plan=%d chars)",
                        getattr(run, "run_id", ""),
                        len(plan_text),
                    )
                    return  # most recent plan wins; stop scanning

    async def _detect_interrupted_run(self) -> None:
        """Build a system-context note if the previous launch crashed mid-run.

        Two independent signals are consulted, in order of how much
        they tell us:

        1. **Agno's session** — if ``aget_session`` returns a session
           whose latest run has ``status=running``, we have rich
           partial state (tool calls, partial content). Use that.
        2. **Pending-message store** — ours. If Agno's session has
           no interrupted run but the pre-persistence layer has a
           ``pending`` row, the previous process died before Agno
           ever wrote anything (the common case for text-only
           responses). We still know the user's question and that
           it didn't finish, which is enough to nudge the agent
           into recapping.

        The summary is one-shot per launch — consumed and cleared
        on the next ``run_message``. Pending rows are then
        discarded so they don't surface again on a subsequent
        restart.
        """
        try:
            from agno.run.base import RunStatus

            session = await self._session.main_team.aget_session(
                session_id=self._session.session_id,
            )
            interrupted_run = None
            if session is not None:
                runs = getattr(session, "runs", None) or []
                if runs and getattr(runs[-1], "status", None) == RunStatus.running:
                    interrupted_run = runs[-1]

            # Pending pre-persisted user messages — the only signal
            # we have when Agno never wrote anything for the
            # crashed run.
            try:
                pending = await self._pending_store.alist_pending(self._session.session_id)
            except Exception:
                pending = []

            if interrupted_run is None and not pending:
                return  # nothing to recover from — clean shutdown

            parts = ["Previous run was interrupted before completion."]
            if pending:
                # The pre-persisted question(s) the user actually
                # typed last time. Quoting verbatim so the agent
                # can recap their words rather than paraphrasing.
                if len(pending) == 1:
                    parts.append(f"The user had asked: {pending[0].text!r}.")
                else:
                    qs = "; ".join(p.text for p in pending)
                    parts.append(f"The user had pending question(s): {qs!r}.")

            if interrupted_run is not None:
                tool_names: list[str] = []
                for t in getattr(interrupted_run, "tools", None) or []:
                    name = getattr(t, "tool_name", None) or "?"
                    tool_names.append(str(name))
                content = (getattr(interrupted_run, "content", None) or "")[:400]
                if tool_names:
                    parts.append(f"Tool calls completed: {', '.join(tool_names)}.")
                if content.strip():
                    parts.append(f"Partial response so far: {content!r}.")

            parts.append(
                "The user has not yet sent a new message. Decide whether to "
                "continue, recap what you found, or ask for direction."
            )
            self._interrupted_run_summary = " ".join(parts)
            # Pending IDs are stashed so the next ``run_message`` can
            # discard them after the agent acknowledges the resume.
            # We deliberately do NOT discard here — the FE needs to
            # read the pending text via ``get_pending_messages`` to
            # render the interrupted question in the conversation
            # pane on ``--continue``. If we discarded eagerly the FE
            # would only have a single ``Info`` line referencing a
            # question the user could no longer see.
            self._pending_message_ids_to_drop = [p.message_id for p in pending]

            logger.info(
                "detected interrupted previous run "
                "(agno_run=%s, pending=%d); summary will be injected on next user message",
                getattr(interrupted_run, "run_id", None),
                len(pending),
            )
        except Exception as exc:
            logger.debug("interrupted-run detection failed: %s", exc)

    # ── Run a user message (streaming) ────────────────────────────

    async def run_message(
        self, text: str, media: dict[str, Any] | None = None
    ) -> AsyncIterator[msg.Message]:
        """Execute a user message and yield protocol messages.

        This is the main streaming entry point. The FE iterates over
        the yielded messages and renders them.

        Serialised by ``self._run_lock``: when the FE submits a new
        message before the previous run's Agno tail has finished
        (compression, memory extraction, final persistence), the new
        call waits silently on the lock. The FE has already cleared
        its "processing" state on ``StreamingDone`` so the user can
        type, but two concurrent ``team.arun()`` calls on the same
        Agno team would race on session/memory state. The lock makes
        the second turn appear as a normal beat of "Thinking" from
        the user's POV; the wait is invisible apart from that.
        """
        async with self._run_lock:
            # Track the task so cancel_run can ``task.cancel()`` it.
            # ``current_task()`` returns the task running this async
            # generator (the one consuming ``_run_message_locked``).
            self._current_run_task = asyncio.current_task()
            try:
                async for proto in self._run_message_locked(text, media):
                    yield proto
            except asyncio.CancelledError:
                # User-initiated cancel — emit a soft notice and return
                # gracefully so the FE clears its "Thinking" state.
                yield msg.Info(text="Run cancelled by user.")
            finally:
                self._current_run_task = None

    async def _run_message_locked(
        self, text: str, media: dict[str, Any] | None
    ) -> AsyncIterator[msg.Message]:
        """Body of ``run_message`` — runs only when the serial lock is held."""
        from ember_code.core.hooks.events import HookEvent

        self._processing = True
        team = self._session.main_team

        # Process @file mentions
        from ember_code.core.utils.mentions import process_file_mentions

        text, mentioned_files = process_file_mentions(text)
        if mentioned_files:
            yield msg.Info(text=f"Referenced: {', '.join(mentioned_files)}")

        # Resolve bare filenames and attach media for vision-capable models
        from ember_code.core.utils.media import resolve_file_references

        model_name = self._session.settings.models.default
        model_cfg = self._session.settings.models.registry.get(model_name, {})
        is_vision = model_cfg.get("vision", False)

        text, resolved_files = resolve_file_references(text, project_dir=self._session.project_dir)
        if resolved_files:
            if is_vision:
                from ember_code.core.utils.media import attach_resolved_files

                parsed_media = attach_resolved_files(resolved_files)
                if parsed_media:
                    media = parsed_media
                    yield msg.Info(text=f"Attached: {len(resolved_files)} file(s)")
                else:
                    yield msg.Info(text=f"Resolved: {', '.join(resolved_files)}")
            else:
                yield msg.Info(text=f"Resolved: {', '.join(resolved_files)}")

        # Attach media URLs (images, etc.) for vision models
        if is_vision:
            from ember_code.core.utils.media import extract_media_urls

            url_media = extract_media_urls(text)
            if url_media:
                if media:
                    for k, v in url_media.items():
                        media.setdefault(k, []).extend(v)
                else:
                    media = url_media
                count = sum(len(v) for v in url_media.values())
                yield msg.Info(text=f"Attached {count} URL(s)")

        # Inject learnings
        await self._session._inject_learnings()

        # If the previous process died mid-chain, surface the
        # incomplete-run summary built during ``startup`` so the
        # agent knows it was interrupted. One-shot per launch — the
        # next iteration of this turn will see ``None``. Pending
        # rows surfaced by ``get_pending_messages`` to the FE on
        # resume are discarded here too, after the agent has been
        # nudged about them — that way a SECOND restart before the
        # user actually responds doesn't surface them again.
        interrupted_summary = self._interrupted_run_summary
        self._interrupted_run_summary = None
        for pending_id_to_drop in self._pending_message_ids_to_drop:
            await self._pending_store.adiscard(pending_id_to_drop)
        self._pending_message_ids_to_drop = []

        # Add timestamp (and the interrupted-run note, if any)
        from datetime import datetime

        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        ctx_parts = [f"Current datetime: {timestamp}"]
        if interrupted_summary:
            ctx_parts.append(interrupted_summary)
            yield msg.Info(text="(continuing from an interrupted previous run)")
        message = f"<system-context>{' '.join(ctx_parts)}</system-context>\n{text}"

        # Fire UserPromptSubmit hook
        hook_result = await self._session.hook_executor.execute(
            event=HookEvent.USER_PROMPT_SUBMIT.value,
            payload={"message": text, "session_id": self._session.session_id},
        )
        if not hook_result.should_continue:
            yield msg.Error(text=hook_result.message or "Message blocked by hook.")
            self._processing = False
            return
        if hook_result.message:
            # Queue hook context for injection
            message = f"{message}\n<hook-context>{hook_result.message}</hook-context>"

        # Stream events from Agno. We multiplex the team's stream with
        # the sub-agent HITL coordinator — see ``_stream_with_subagent_hitl``
        # for the full rationale. The same multiplexer is also used by
        # ``resolve_hitl`` so a parent that pauses (top-level Bash) and
        # then spawns a sub-agent on resume still gets the sub-agent's
        # pauses surfaced — an earlier version only multiplexed inside
        # ``run_message`` and the sub-agent's pauses silently sat in the
        # coordinator forever.
        # Pre-persist the user message so a kill mid-stream doesn't
        # lose it (Agno doesn't write to disk until end-of-run).
        # The id is opaque; we use it on the success path to mark
        # the row completed. On a crash the row stays ``pending``
        # and ``_detect_interrupted_run`` surfaces it on the next
        # ``--continue`` boot.
        pending_id = await self._pending_store.arecord_received(self._session.session_id, text)

        # Periodic checkpoint task — fires ``asave_session`` every
        # few seconds during the run so streaming responses (which
        # otherwise see zero disk writes between RunStarted and
        # RunCompleted) survive a crash within ~3 seconds of
        # whatever Agno had assembled by then. Cancelled in the
        # finally below regardless of how the run exits.
        checkpoint_task = asyncio.create_task(self._periodic_checkpoint(team))

        media_kwargs = media or {}
        try:
            async for proto in self._stream_with_subagent_hitl(
                team.arun(message, stream=True, **media_kwargs)
            ):
                # Latch the top-level run's input-token count as the
                # current context size. ``input_tokens`` is the prompt
                # Agno sent to the model — which IS the live context.
                # Computing it lazily from ``aget_session`` (the old
                # path) hung after a run while Agno's post-stream tail
                # held session state; this is O(1) and never blocks.
                if (
                    isinstance(proto, msg.RunCompleted)
                    and not proto.parent_run_id
                    and proto.input_tokens
                ):
                    self._session._last_input_tokens = proto.input_tokens
                yield proto
                # Checkpoint after each tool completion. Agno's default
                # persistence model is end-of-run only — if the process
                # crashes mid-chain, the in-flight ``RunOutput`` is lost
                # and ``--continue`` can't surface the partial work to
                # the agent. Forcing ``asave_session`` after every
                # tool-completed write means a crash leaves a session
                # with ``status=running`` containing every tool call
                # that finished. On a successful run the natural
                # end-of-run save overwrites the last partial snapshot
                # via Agno's upsert semantics, so no separate "drop
                # partial" cleanup is needed. The cost is one ~1-5ms
                # SQLite upsert per tool call — negligible compared to
                # the model latency, and only fires on actual tool
                # completion events (not every ContentDelta).
                if isinstance(proto, (msg.ToolCompleted, msg.ToolError)):
                    await self._checkpoint_session(team)
            # Run reached natural end → mark the pre-persisted user
            # message as completed so it doesn't get surfaced as
            # "interrupted" on the next boot.
            await self._pending_store.amark_completed(pending_id)
        finally:
            self._processing = False
            checkpoint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await checkpoint_task
            await self._close_model_http_client(team)

        # Fire Stop hook
        stop_result = await self._session.hook_executor.execute(
            event=HookEvent.STOP.value,
            payload={"session_id": self._session.session_id},
        )
        if stop_result.message and not stop_result.should_continue:
            yield msg.Info(text=stop_result.message)

    async def _stream_with_subagent_hitl(
        self, team_stream: AsyncIterator[Any]
    ) -> AsyncIterator[msg.Message]:
        """Multiplex a team's event stream with the sub-agent coordinator.

        The team's stream and the sub-agent HITL coordinator are two
        independent producers of messages we need to forward to the FE:

        * ``team_stream`` is whatever Agno is currently driving — the
          initial ``team.arun`` call from ``run_message``, or a
          ``team.acontinue_run`` resumption from ``resolve_hitl``.
        * The coordinator wakes whenever a sub-agent (running inside a
          ``spawn_agent`` tool) hits a ``RunPausedEvent``. We have to
          surface that pause to the FE as a ``RunPaused`` message so the
          dialog appears.

        Both paths must run concurrently with the team stream; otherwise
        a sub-agent that pauses while the parent is still streaming
        events would have its requirement sitting in the coordinator
        forever with no one to forward it. Centralising this here means
        ``run_message`` AND ``resolve_hitl`` both get the multiplexer —
        a previous version had it only in ``run_message`` so any sub-
        agent spawn that happened during a resumed run (parent paused
        for top-level Bash, user approved, parent resumed and then
        spawned an architect) silently dropped the architect's pauses.

        The team's own ``RunPausedEvent`` (parent pauses for its own
        tool) terminates this stream and is forwarded as ``RunPaused``;
        the FE then routes resolution back through ``resolve_hitl``,
        which calls this helper again with the resumed stream.
        """
        from ember_code.protocol.agno_events import (
            RUN_COMPLETED_EVENTS,
            RUN_ERROR_EVENTS,
            RUN_PAUSED_EVENTS,
        )

        sub_hitl = self._session.sub_agent_hitl
        agno_queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()
        import logging as _log

        llm_log = _log.getLogger("ember_code.llm_calls")

        # Direct-write trace bypassing the logging stack — see earlier
        # debugging note. Cheap; one flushed write per pump iteration.
        import os as _os
        from pathlib import Path as _Path

        _trace_path = _Path(_os.path.expanduser("~/.ember/hitl_trace.log"))
        _trace_path.parent.mkdir(parents=True, exist_ok=True)

        def _trace(msg_text: str) -> None:
            try:
                with open(_trace_path, "a") as _f:
                    import time as _t

                    _f.write(f"{_t.strftime('%H:%M:%S')} pid={_os.getpid()} {msg_text}\n")
            except Exception:
                pass

        async def _drain_team() -> None:
            try:
                async for event in team_stream:
                    await agno_queue.put(("event", event))
            except Exception as e:
                await agno_queue.put(("error", e))
            finally:
                await agno_queue.put(("done", SENTINEL))

        async def _drain_subagent_hitl() -> None:
            _trace(f"_stream_mux: drain STARTED (coord_id={id(sub_hitl)})")
            try:
                while True:
                    await sub_hitl.new_arrival.wait()
                    entries = sub_hitl.list_new_pending()
                    _trace(f"_stream_mux: drain woke, {len(entries)} entries")
                    if entries:
                        await agno_queue.put(("subagent_pause", entries))
                        _trace(f"_stream_mux: drain enqueued {[rid for rid, _ in entries]}")
            except asyncio.CancelledError:
                _trace("_stream_mux: drain cancelled")
                return

        _trace(f"_stream_mux: starting (coord_id={id(sub_hitl)})")
        # Hold the team-drain reference so the task isn't GC'd mid-run
        # (asyncio only weakly references background tasks). We
        # deliberately don't cancel it in ``finally`` — see comment
        # below.
        _team_task = asyncio.create_task(_drain_team())  # noqa: F841
        sub_task = asyncio.create_task(_drain_subagent_hitl())

        try:
            while True:
                kind, payload = await agno_queue.get()
                if kind == "done":
                    return
                if kind == "error":
                    raise payload
                if kind == "subagent_pause":
                    entries = payload
                    rp = self._build_subagent_run_paused(entries)
                    llm_log.info(
                        "subagent_hitl: yielding RunPaused to FE with %d req(s)",
                        len(entries),
                    )
                    yield rp
                    continue
                event = payload
                if isinstance(event, RUN_PAUSED_EVENTS):
                    pause_msgs, auto_resolved, paused_run_id = self._handle_pause(event)
                    for pause_msg in pause_msgs:
                        yield pause_msg
                    if pause_msgs:
                        # Mixed pause: some reqs still need the user.
                        # Stash the auto-resolved ones so
                        # ``resolve_hitl_batch`` can merge them into the
                        # eventual ``acontinue_run`` call.
                        if auto_resolved and paused_run_id:
                            self._auto_resolved_requirements.setdefault(paused_run_id, []).extend(
                                auto_resolved
                            )
                        return
                    if auto_resolved and paused_run_id:
                        # Every req was decided by the evaluator. Resume
                        # the team immediately without ever bothering
                        # the FE — this is the plan-mode / acceptEdits /
                        # bypass / deny-rule short-circuit.
                        team = self._session.main_team
                        llm_log.info(
                            "auto-resuming run_id=%s with %d evaluator-resolved req(s)",
                            paused_run_id,
                            len(auto_resolved),
                        )
                        async for proto in self._stream_with_subagent_hitl(
                            team.acontinue_run(
                                run_id=paused_run_id,
                                session_id=self._session.session_id,
                                requirements=auto_resolved,
                                stream=True,
                                stream_events=True,
                            )
                        ):
                            yield proto
                        return
                    # No requirements at all — defensive; shouldn't
                    # normally happen but don't strand the stream.
                    return
                # If a run completes/errors without going through HITL
                # resolution (e.g. tool didn't require approval, or the
                # whole run was cancelled) sweep any stale pending
                # requirements for that run_id so they don't pile up on
                # the session. ``resolve_hitl_batch`` already pops the
                # entries it resolves; this catches the "user closed the
                # UI mid-pause and the run later wrapped up" path.
                run_finished = isinstance(event, RUN_COMPLETED_EVENTS + RUN_ERROR_EVENTS)
                if run_finished:
                    finished_run_id = getattr(event, "run_id", None)
                    if finished_run_id:
                        self._drop_pending_for_run(finished_run_id)
                proto = serialize_event(event)
                if proto is not None:
                    yield proto
                if run_finished:
                    # Drain post-run broadcasts (e.g. ``plan_submitted``
                    # queued by ``exit_plan_mode``). The push fires AFTER
                    # the run's content has flushed so the PlanCard lands
                    # at the bottom of the agent's reply, not mid-stream.
                    drain = getattr(self._session, "drain_post_run_broadcasts", None)
                    if drain is not None:
                        try:
                            drain()
                        except Exception as exc:
                            logger.debug("post-run broadcast drain raised: %s", exc)
        except asyncio.TimeoutError:
            yield msg.Error(text="Request timed out — the model took too long to respond.")
        except Exception as e:
            yield msg.Error(text=str(e))
        finally:
            sub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sub_task
            # Don't cancel team_task — the team's stream may still have
            # a paused tool we want to drive to completion via
            # ``resolve_hitl``. The team task naturally exits when the
            # team's stream ends or errors.

    def _build_subagent_run_paused(self, entries: list) -> msg.Message:
        """Wrap a batch of sub-agent coordinator entries in a ``RunPaused``.

        The FE renders the confirmation dialog only when it sees
        ``RunPaused``; bare ``HITLRequest`` falls through. Sub-agent pauses
        match the FE's expected shape so the same dialog flow applies.
        Resolution still routes through the coordinator (not the main
        team's ``acontinue_run``) — see ``resolve_hitl``.
        """
        from ember_code.protocol.agno_events import TOOL_NAMES

        requirements = []
        for req_id, entry in entries:
            req = entry.requirement
            tool_exec = getattr(req, "tool_execution", None)
            raw_name = str(getattr(tool_exec, "tool_name", "") if tool_exec else "")
            requirements.append(
                msg.HITLRequest(
                    requirement_id=req_id,
                    tool_name=raw_name,
                    friendly_name=TOOL_NAMES.get(raw_name, raw_name),
                    tool_args=dict(getattr(tool_exec, "tool_args", {}) if tool_exec else {}),
                    agent_path=list(getattr(entry, "agent_path", []) or []),
                )
            )
        # ``run_id`` here is the sub-agent's id; the FE doesn't currently
        # use it for sub-agent pauses (it routes through ``resolve_hitl``
        # which picks the coordinator path), but we forward it for logs.
        sub_run_id = entries[0][1].run_id if entries else ""
        return msg.RunPaused(run_id=sub_run_id, requirements=requirements)

    async def _periodic_checkpoint(self, team: Any, interval: float = 3.0) -> None:
        """Background loop that snapshots the session every ``interval`` seconds.

        Agno's streaming runs don't write to disk between
        RunStarted and RunCompleted. For a pure text-only response
        (no tools, so no tool-completed event for us to hook), the
        in-flight ``RunOutput`` would never reach SQLite — a crash
        mid-stream would lose the user's prompt AND the partial
        response. The pre-persistence in ``_run_message_locked``
        saves the prompt unconditionally; this task takes care of
        the partial response by forcing ``asave_session`` on a
        cadence.

        Cancellation is the normal stop signal — the streaming
        loop cancels this task in its finally. We swallow
        ``CancelledError`` cleanly and exit; anything else is
        logged but never propagated.
        """
        try:
            while True:
                await asyncio.sleep(interval)
                await self._checkpoint_session(team)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("periodic checkpoint task crashed: %s", exc)

    async def _checkpoint_session(self, team: Any) -> None:
        """Force Agno to persist the in-flight session to SQLite.

        Agno saves the session blob only at end-of-run via
        ``_cleanup_and_store``. Mid-run, ``upsert_run`` writes to the
        *in-memory* session but never touches disk. A process crash
        between user message and run completion therefore loses
        everything Agno did so far — tool calls, partial responses,
        intermediate planning. By snapshotting the cached session
        after every tool completion we keep the disk copy within one
        tool-result of the live state, so ``--continue`` after a
        crash surfaces the in-flight ``RunOutput`` (with
        ``status=running``) and the agent can pick up where it left
        off. On a clean completion, Agno's own end-of-run save
        overwrites these snapshots via upsert semantics — no
        explicit cleanup needed.

        Best-effort: a transient persistence failure must not abort
        the live stream. If the session blob is unavailable (e.g.
        Agno hasn't created the cached_session yet on a very early
        event) we log and move on.
        """
        try:
            session = getattr(team, "cached_session", None)
            if session is None:
                return
            await team.asave_session(session)
        except Exception as exc:
            logger.debug("incremental session checkpoint failed: %s", exc)

    def _drop_pending_for_run(self, run_id: str) -> None:
        """Remove any pending HITL entries tied to a finished run.

        Called when a run completes/errors without going through
        ``resolve_hitl_batch`` (which would've popped them). Guards
        against per-session accumulation of dead requirement entries
        when the user closes the pause UI and the run later wraps up
        on its own.
        """
        stale = [
            rid for rid, (_req, rid_run) in self._pending_requirements.items() if rid_run == run_id
        ]
        for rid in stale:
            self._pending_requirements.pop(rid, None)
        if stale:
            logger.debug(
                "_drop_pending_for_run: dropped %d stale requirement(s) for run_id=%s",
                len(stale),
                run_id,
            )
        # Same sweep for the auto-resolved bucket — if the run finished
        # without ``resolve_hitl_batch`` draining it, drop the entry so
        # it doesn't leak across sessions.
        auto_bucket = getattr(self, "_auto_resolved_requirements", None)
        if auto_bucket is not None:
            auto_bucket.pop(run_id, None)

    def _handle_pause(self, event: Any) -> tuple[list[msg.Message], list[Any], str | None]:
        """Convert a RunPausedEvent into protocol messages and store requirements.

        Returns ``(messages, auto_resolved_reqs, run_id)``:

        * ``messages`` — what to forward to the FE. Either a single
          ``RunPaused`` (when at least one requirement still needs the
          user) or empty (when the evaluator decided every req).
        * ``auto_resolved_reqs`` — Agno requirement objects that the
          evaluator already confirmed or rejected. Caller resumes Agno
          with these via ``acontinue_run`` (all-auto case) or stashes
          them on ``_auto_resolved_requirements`` for the eventual
          ``resolve_hitl_batch`` (mixed case).
        * ``run_id`` — the paused run; needed by the resume.

        Why do this here: Agno's ``requires_confirmation`` gate pauses
        every "ask"-level tool indiscriminately. Without this pre-step,
        plan-mode-deny, acceptEdits-allow, bypass-allow, and ``deny:``
        rules can never short-circuit the dialog — the user sees an
        approval prompt for tools the policy already decided about.
        """
        from ember_code.core.config.permission_eval import (
            PermissionDecision,
            explain_deny,
        )
        from ember_code.protocol.agno_events import TOOL_NAMES

        run_id_raw = getattr(event, "run_id", None)
        run_id = str(run_id_raw) if run_id_raw else None
        evaluator = getattr(self._session, "permission_evaluator", None)
        requirements: list[msg.HITLRequest] = []
        auto_resolved: list[Any] = []

        for req in getattr(event, "active_requirements", []) or []:
            req_id = str(uuid.uuid4())[:8]
            tool_exec = getattr(req, "tool_execution", None)
            raw_name = str(getattr(tool_exec, "tool_name", "") if tool_exec else "")
            tool_args = dict(getattr(tool_exec, "tool_args", {}) if tool_exec else {})

            auto_decision: str | None = None  # "confirm" | "reject" | None
            if evaluator is not None:
                try:
                    pd = evaluator.evaluate(raw_name, tool_args)
                except Exception as exc:
                    logger.warning(
                        "permission_evaluator.evaluate(%s) raised %s — falling back to user prompt",
                        raw_name,
                        exc,
                    )
                else:
                    if pd is PermissionDecision.DENY:
                        auto_decision = "reject"
                    elif pd is PermissionDecision.ALLOW:
                        auto_decision = "confirm"

            if auto_decision == "confirm":
                try:
                    req.confirm()
                except Exception as exc:
                    logger.warning(
                        "auto-confirm raised for %s: %s — falling back to user prompt",
                        raw_name,
                        exc,
                    )
                else:
                    logger.info(
                        "Auto-confirmed %s by permission policy (run_id=%s)",
                        raw_name,
                        run_id,
                    )
                    auto_resolved.append(req)
                    continue
            elif auto_decision == "reject":
                reason = explain_deny(evaluator, raw_name, tool_args)
                try:
                    req.reject(note=f"Blocked: {reason}")
                except Exception as exc:
                    logger.warning(
                        "auto-reject raised for %s: %s — falling back to user prompt",
                        raw_name,
                        exc,
                    )
                else:
                    logger.info(
                        "Auto-rejected %s (%s) run_id=%s",
                        raw_name,
                        reason,
                        run_id,
                    )
                    auto_resolved.append(req)
                    continue

            # Defer: ask the user as before.
            self._pending_requirements[req_id] = (req, run_id)
            requirements.append(
                msg.HITLRequest(
                    requirement_id=req_id,
                    tool_name=raw_name,
                    friendly_name=TOOL_NAMES.get(raw_name, raw_name),
                    tool_args=tool_args,
                )
            )

        messages: list[msg.Message] = []
        if requirements:
            messages.append(msg.RunPaused(run_id=run_id or "", requirements=requirements))
        return messages, auto_resolved, run_id

    async def resolve_hitl_batch(
        self, decisions: list[msg.HITLDecision]
    ) -> AsyncIterator[msg.Message]:
        """Resolve every requirement from a multi-req pause in one shot.

        Agno's ``acontinue_run`` denies anything not in the resolved-
        requirements list. The earlier per-req ``resolve_hitl`` loop
        therefore meant: only the first user-approved tool actually
        ran; the rest of an 8-tool batch came back as "User denied"
        and the LLM reported them as REJECTED. This batch method:

        1. Splits each decision between the sub-agent coordinator
           (its own resolve path) and the main team's pending list.
        2. Confirms/rejects every main-team requirement object so
           Agno sees the full resolution set.
        3. Calls ``acontinue_run`` exactly once with all resolved
           reqs, then streams the continuation.

        Sub-agent reqs don't need ``acontinue_run`` — their
        coordinator wakes the spawning stream directly.
        """
        if not decisions:
            return

        main_resolved_reqs: list[Any] = []
        run_id: str | None = None
        for d in decisions:
            # Belt-and-suspenders: even if the sub-agent coordinator
            # claims the requirement, drop any main-team entry under
            # the same id so it can't strand the dict in case of a
            # double-registration bug elsewhere.
            if self._session.sub_agent_hitl.resolve(d.requirement_id, d.action):
                self._pending_requirements.pop(d.requirement_id, None)
                continue
            entry = self._pending_requirements.pop(d.requirement_id, None)
            if entry is None:
                yield msg.Error(text=f"Unknown requirement: {d.requirement_id}")
                continue
            req, this_run_id = entry
            # All reqs from a single RunPaused share a run_id. Reject
            # any cross-pause batch — passing mixed run_ids to
            # ``acontinue_run`` would silently resume the wrong run.
            if run_id is None:
                run_id = this_run_id
            elif this_run_id != run_id:
                yield msg.Error(
                    text=(
                        f"Cross-run HITL batch rejected: "
                        f"requirement {d.requirement_id} belongs to run "
                        f"{this_run_id} but batch is for run {run_id}"
                    )
                )
                # Put the requirement back so a later batch can resolve
                # it correctly.
                self._pending_requirements[d.requirement_id] = entry
                continue
            # Isolate per-req failures: one Agno requirement raising
            # on confirm()/reject() must not strand the remaining reqs
            # in the pause. Without this, a single bad req leaves the
            # whole run waiting forever.
            try:
                if d.action == "confirm":
                    req.confirm()
                else:
                    req.reject(note="User denied")
            except Exception as exc:
                logger.warning(
                    "resolve_hitl_batch: requirement %s %s() raised %s; skipping",
                    d.requirement_id,
                    d.action,
                    exc,
                )
                yield msg.Error(text=f"Failed to {d.action} requirement {d.requirement_id}: {exc}")
                continue
            main_resolved_reqs.append(req)

        # Merge in any auto-resolved (plan/acceptEdits/bypass/deny)
        # requirements that were decided in ``_handle_pause`` for the
        # same run. Agno's ``acontinue_run`` denies anything not in
        # the resolved set, so we MUST pass them all in one call.
        # ``getattr`` default keeps tests that build the server via
        # ``__new__`` (skipping ``__init__``) working unchanged.
        auto_bucket = getattr(self, "_auto_resolved_requirements", None)
        if run_id is not None and auto_bucket is not None:
            stashed = auto_bucket.pop(run_id, [])
            if stashed:
                main_resolved_reqs.extend(stashed)

        if not main_resolved_reqs:
            return  # everything was sub-agent or failed

        team = self._session.main_team
        import logging as _log

        _llm = _log.getLogger("ember_code.llm_calls")
        _llm.info(
            "resolve_hitl_batch: %d req(s) resolved, run_id=%s",
            len(main_resolved_reqs),
            run_id,
        )
        async for proto in self._stream_with_subagent_hitl(
            team.acontinue_run(
                run_id=run_id,
                session_id=self._session.session_id,
                requirements=main_resolved_reqs,
                stream=True,
                stream_events=True,
            )
        ):
            yield proto

        # Fire Stop hook after continuation completes.
        from ember_code.core.hooks.events import HookEvent

        stop_result = await self._session.hook_executor.execute(
            event=HookEvent.STOP.value,
            payload={"session_id": self._session.session_id},
        )
        if stop_result.message and not stop_result.should_continue:
            yield msg.Info(text=stop_result.message)

    async def resolve_hitl(
        self, requirement_id: str, action: str, choice: str = "once"
    ) -> AsyncIterator[msg.Message]:
        """Resolve a single HITL requirement.

        Implemented as a thin shim over ``resolve_hitl_batch`` so the
        dangerous ``acontinue_run(requirements=[req])`` callsite only
        exists in *one* place — the batch method — which always
        passes the *full* set of resolved requirements. This way a
        future caller that hits a multi-req pause via the legacy
        single-req path doesn't silently re-introduce the v0.5.11
        "User denied" cascade. For a 1-req pause this behaves
        identically to the old direct-call implementation.
        """
        decision = msg.HITLDecision(requirement_id=requirement_id, action=action, choice=choice)
        async for proto in self.resolve_hitl_batch([decision]):
            yield proto

    # ── Commands ──────────────────────────────────────────────────

    async def handle_command(self, text: str) -> msg.CommandResult:
        """Process a slash command and return the result."""
        from ember_code.backend.command_handler import CommandHandler

        handler = CommandHandler(self._session)
        result = await handler.handle(text)
        return msg.CommandResult(
            kind=result.kind,
            content=result.content,
            display_content=getattr(result, "display_content", "") or "",
            action=result.action or "",
        )

    # ── Session management ────────────────────────────────────────

    async def list_sessions(self) -> msg.SessionListResult:
        """List available sessions."""
        raw = await self._session.persistence.list_sessions(limit=20)
        return msg.SessionListResult(sessions=raw)

    async def maybe_auto_name_session(self) -> str | None:
        """Auto-generate a name for the current session if it has none.

        Called after a run completes — Agno derives the name from the
        conversation so far. Returns the new name, or None when the
        session is already named (or naming failed).
        """
        try:
            if await self._session.persistence.get_name():
                return None
            await self._session.persistence.auto_name(self._session.main_team)
            name = await self._session.persistence.get_name() or ""
            # Models sometimes wrap the title in markdown ("**Title**").
            clean = re.sub(r"^[\s*_`'\"#]+|[\s*_`'\"]+$", "", name)
            if clean and clean != name:
                await self._session.persistence.rename(clean)
            return clean or None
        except Exception as exc:
            logger.debug("session auto-name failed: %s", exc)
            return None

    async def switch_session(self, session_id: str) -> msg.Info:
        """Switch to a different session."""
        self._session.session_id = session_id
        self._session.session_named = True
        self._session.main_team.session_id = session_id
        self._session.persistence.session_id = session_id

        # Load history — aget_session triggers Agno to restore conversation
        agent = self._session.main_team
        await agent.aget_session(
            session_id=session_id,
            user_id=self._session.user_id,
        )
        name = await self._session.persistence.get_name()
        return msg.Info(text=f"Switched to session: {name or session_id}")

    # ── MCP ───────────────────────────────────────────────────────

    async def ensure_mcp(self) -> None:
        """Initialize MCP connections."""
        await self._session.ensure_mcp()

    async def toggle_mcp(self, server_name: str, connect: bool) -> msg.Info:
        """Connect or disconnect an MCP server."""
        mgr = self._session.mcp_manager
        if connect:
            await mgr.connect(server_name)
        else:
            await mgr.disconnect_one(server_name)
        self._session.rebuild_mcp()
        return msg.Info(text=f"MCP {'connected' if connect else 'disconnected'}: {server_name}")

    def get_mcp_status(self) -> list[tuple[str, bool]]:
        """Get MCP server connection status."""
        return self._session.get_mcp_status()

    def set_mcp_tool_enabled(self, server: str, tool: str, enabled: bool) -> dict:
        """Enable or disable a single tool on an MCP server.

        Disabled tools are still listed by ``get_mcp_server_details``
        with ``disabled: true`` so the panel can render them muted,
        but they're removed from the live ``MCPTools.functions`` dict
        so the next agent run won't see them. State persists to
        ``<project>/.ember/mcp-tool-state.json``.
        """
        self._session.mcp_manager.set_tool_enabled(server, tool, enabled)
        return {"server": server, "tool": tool, "enabled": enabled}

    # ── Permissions ────────────────────────────────────────────────

    def check_permission(self, tool_name: str, func_name: str, tool_args: dict) -> str:
        """Check permission level for a tool call. Returns 'allow'/'deny'/'ask'."""
        from ember_code.core.config.tool_permissions import FUNC_TO_TOOL, ToolPermissions

        perms = ToolPermissions(project_dir=self._session.project_dir)
        registry_name = FUNC_TO_TOOL.get(func_name, tool_name)
        return perms.check(registry_name, func_name, tool_args)

    def save_permission_rule(self, rule: str, level: str) -> None:
        """Persist a permission rule."""
        from ember_code.core.config.tool_permissions import ToolPermissions

        perms = ToolPermissions(project_dir=self._session.project_dir)
        perms.save_rule(rule, level)

    # ── Model ─────────────────────────────────────────────────────

    def switch_model(self, model_name: str) -> msg.Info:
        """Switch the active model and persist the choice.

        Two layers of persistence so the choice survives both an
        app restart AND a session resume:

        * **User-level default** — written to
          ``~/.ember/config.yaml`` so any new session opened next
          launch uses this model. Best-effort: a save failure is
          logged but doesn't fail the switch (the in-memory state
          is already updated).
        * **Per-session preference** — written to
          ``state.db``'s ``ember_session_preferences`` table keyed
          by session_id. ``--continue`` consults this on startup
          and overrides the user-level default for the resumed
          session.
        """
        from ember_code.core.config.settings import save_default_model

        old_name = self._session.settings.models.default
        old_cfg = self._session.settings.models.registry.get(old_name, {})
        new_cfg = self._session.settings.models.registry.get(model_name, {})

        self._session.settings.models.default = model_name
        self._session.main_team = self._session._build_main_agent()

        # User-level persistence.
        try:
            save_default_model(model_name)
        except Exception as exc:
            logger.warning("failed to persist model choice to user config: %s", exc)

        # Per-session persistence.
        try:
            self._session_prefs.set_model(self._session.session_id, model_name)
        except Exception as exc:
            logger.debug("failed to persist per-session model preference: %s", exc)

        note = f"Switched to {model_name}"
        # Warn if switching from vision to non-vision with media in history
        if old_cfg.get("vision") and not new_cfg.get("vision"):
            note += (
                "\nNote: previous messages may contain images/files. "
                "Use /clear to reset if you get errors."
            )
        return msg.Info(text=note)

    # ── Login/Logout ──────────────────────────────────────────────

    async def login(self, on_status=None) -> tuple[bool, str]:
        """Run the browser-callback login flow.

        Args:
            on_status: optional callback(str) for status updates to FE

        Returns:
            (success, email) tuple
        """
        import webbrowser

        from ember_code.core.auth.client import (
            get_login_url,
            start_callback_server,
            validate_token,
            wait_for_token,
        )
        from ember_code.core.auth.credentials import decode_jwt_claims, save_credentials

        def _status(text: str) -> None:
            if on_status:
                result = on_status(text)
                # Support both sync and async callbacks
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)

        server = None
        try:
            _status("Starting local server...")
            server, callback_url = start_callback_server()
            port = int(callback_url.split(":")[2].split("/")[0])
            login_url = get_login_url(port)

            with contextlib.suppress(Exception):
                webbrowser.open(login_url)

            _status(
                f"Waiting for login in browser...\nIf the browser didn't open, go to:\n{login_url}"
            )

            try:
                token = await wait_for_token(server, timeout=300)
            except TimeoutError:
                return False, "Login timed out"

            _status("Fetching user info...")
            user_info = await validate_token(token, self._settings.api_url)
            email = user_info.get("email", "") if user_info else ""

            # Read expiry from JWT for accurate TTL
            claims = decode_jwt_claims(token)
            if claims.get("exp"):
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)
                exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
                ttl = max(int((exp - now).total_seconds()), 0)
                save_credentials(token, email, ttl=ttl)
            else:
                save_credentials(token, email)

            self.reload_cloud_credentials()
            return True, email

        except Exception as exc:
            return False, str(exc)
        finally:
            # Always close the callback server to free the port
            if server is not None:
                with contextlib.suppress(Exception):
                    server.server_close()

    def reload_cloud_credentials(self) -> msg.StatusUpdate:
        """Reload cloud credentials after login."""
        from ember_code.core.auth.credentials import CloudCredentials

        self._session._cloud = CloudCredentials(self._settings.auth.credentials_file)
        self._session.main_team = self._session._build_main_agent()
        return self.get_status()

    def clear_cloud_credentials(self) -> msg.StatusUpdate:
        """Clear cloud credentials on logout."""
        from ember_code.core.auth.credentials import CloudCredentials

        # Point at a path that doesn't exist so all properties resolve to None.
        self._session._cloud = CloudCredentials(path="/dev/null")
        self._session.main_team = self._session._build_main_agent()
        return self.get_status()

    async def get_cloud_plan(self) -> dict | None:
        """Fetch the current user's plan tier from the cloud.

        Hits ``/portal/me`` with the stored JWT (same endpoint the
        client uses to validate the token on login). The response
        includes the user's tier from their active org membership —
        ``lite`` / ``pro`` / ``max`` / ``codeindex``. FE renders
        this as "Plan: Pro" in the org popover and refreshes on
        every popover open so users see seat/tier changes without
        having to restart the app.

        Returns ``None`` when there are no credentials (logged out)
        or the call fails — FE hides the row in that case.
        """
        token = self._session._cloud.access_token
        if not token:
            return None
        from ember_code.core.auth.client import DEFAULT_API_URL, validate_token

        api_url = getattr(self._settings.auth, "api_url", DEFAULT_API_URL) or DEFAULT_API_URL
        info = await validate_token(token, api_url=api_url)
        if not info:
            return None
        return {"tier": info.get("tier"), "org_name": info.get("org_display_name")}

    # ── Status ────────────────────────────────────────────────────

    def get_status(self) -> msg.StatusUpdate:
        """Get current status bar data.

        Context size comes from the last run's ``input_tokens`` (the
        prompt Agno sent — i.e. the live context). O(1), no DB hit;
        an earlier async implementation called ``aget_session`` and
        hung after a run while Agno's post-stream tail held session
        state.
        """
        # Defensive ``isinstance`` guards: production code always
        # gets a real ``PermissionEvaluator`` here, but test
        # fixtures often pass a ``MagicMock`` session whose
        # ``permission_evaluator.mode.value`` returns a MagicMock
        # — pydantic then rejects the StatusUpdate. The check
        # falls back to ``"default"`` for any non-string value so
        # the wire shape stays valid.
        evaluator = getattr(self._session, "permission_evaluator", None)
        raw_mode = getattr(getattr(evaluator, "mode", None), "value", None)
        mode = raw_mode if isinstance(raw_mode, str) else "default"
        return msg.StatusUpdate(
            model=self._settings.models.default,
            cloud_connected=self._session.cloud_connected,
            cloud_org=self._session.cloud_org_name or "",
            context_tokens=getattr(self._session, "_last_input_tokens", 0),
            max_context=self._settings.models.max_context_window,
            permission_mode=mode,
        )

    # ── /loop continuation ────────────────────────────────────────

    async def pop_pending_loop_iteration(self) -> dict | None:
        """Pop the next ``/loop`` iteration descriptor (or completion).

        Thin wrapper over :py:meth:`Session.advance_loop` — that
        method owns the counter math and the persistence write so
        a CLI restart sees the correct in-flight iteration.
        Returns shapes match what the FE's run controller expects.
        """
        return await self._session.advance_loop()

    async def cancel_pending_loop(self) -> bool:
        """Clear ``/loop`` state. Returns whether anything was actually
        cancelled.

        Called by the FE's ``process_message`` cancel guard when the
        user types a non-``/loop`` message — user input takes
        precedence over an *actively pumping* loop.

        Paused loops (loaded from disk on startup, not yet resumed)
        are intentionally NOT cancelled here: the user might be
        about to type ``/loop resume`` or simply asking something
        unrelated. If they want to discard the paused state they
        say ``/loop stop`` explicitly. Without this guard, the
        first character typed after a restart would destroy the
        very state the user might want to continue.
        """
        if self._session.loop_paused:
            return False
        return await self._session.cancel_loop()

    async def loop_pause(self) -> bool:
        """Pause the active loop without advancing the counter.

        Called by the FE's ``_check_loop_continuation`` when an
        iteration's ``_run`` raised (e.g. a 429 from the model
        API). Keeping the counter at the failing iteration N
        means a subsequent ``/loop resume`` retries N, not skips
        to N+1.
        """
        return await self._session.pause_loop()

    async def loop_resume(self) -> str:
        """Flip the loop from paused to pumping and return the prompt.

        Returns the prompt verbatim so the panel-side app handler
        can fire ``_run(prompt)`` directly — same trick the slash
        ``/loop resume`` uses to bypass ``process_message``'s
        cancel guard. Returns an empty string when there's nothing
        to resume (no loop or not paused); the caller surfaces an
        appropriate message.
        """
        prompt = await self._session.resume_loop()
        return prompt or ""

    async def loop_status(self) -> dict:
        """Snapshot for the ``/loop`` panel header.

        Cheap read of the three session fields — safe to poll at 1Hz
        from the panel while a loop is running. ``active`` mirrors
        ``pending_loop_prompt is not None`` so the panel can pick
        empty-state vs. live-state without inspecting the prompt.

        ``iteration_index`` is the count of iterations already
        *fired* (0-based when no iteration has run yet), and
        ``iterations_remaining`` is how many more *will* fire if
        the cap isn't shortened. Their sum on a running loop is the
        configured cap.
        """
        sess = self._session
        # Read the agent's announced iteration total (if any) from
        # the loop_progress reserved key. Cheap (one indexed
        # lookup); safe to do on every poll. ``None`` when the
        # agent hasn't called ``loop_set_total`` yet.
        announced_total: int | None = None
        if sess.loop_run_id:
            from ember_code.core.tools.loop import LoopTools

            raw = await sess.loop_progress_store.get(
                sess.loop_run_id, LoopTools._ANNOUNCED_TOTAL_KEY
            )
            if raw:
                try:
                    announced_total = int(raw)
                except ValueError:
                    announced_total = None
        return {
            "active": sess.pending_loop_prompt is not None,
            "paused": sess.loop_paused,
            "prompt": sess.pending_loop_prompt or "",
            "iteration_index": sess.loop_iteration_index,
            "iterations_remaining": sess.loop_iterations_remaining,
            # When False, the cap is a safety net — the panel hides
            # the "total" entirely. When True, the cap is the
            # intended total and the panel renders ``N / M``.
            "cap_explicit": sess.loop_cap_explicit,
            # Agent-announced iteration total via ``loop_set_total``
            # — takes precedence over ``cap_explicit`` when set
            # because it reflects the *actual* item count the
            # agent derived from the work, not just a bound.
            "announced_total": announced_total,
        }

    # ── Compaction ────────────────────────────────────────────────

    async def count_context_tokens(self) -> int:
        """Locally count the tokens of the current conversation.

        Agno's ``Model.count_tokens`` picks the right tokenizer per model
        (tiktoken for OpenAI-likes, HF for known HF models, character
        estimation otherwise) so we don't roll our own per-provider
        logic. Used by the status-bar context indicator and the
        ``compact_if_needed`` trigger — both used to read
        ``input_tokens`` off the wire, which on prompt-caching providers
        (Anthropic) compounds ``cache_read_input_tokens`` across tool
        iterations into millions of tokens and was triggering the 80%
        auto-compaction → history wipe path on basically every turn.
        """
        try:
            agno_session = await self._session.main_team.aget_session(
                session_id=self._session.session_id,
                user_id=self._session.user_id,
            )
        except Exception as exc:
            logger.debug("aget_session failed (%s); reporting 0", exc)
            return 0
        if agno_session is None:
            return 0
        try:
            messages = agno_session.get_messages()
        except Exception as exc:
            logger.debug("get_messages failed (%s); reporting 0", exc)
            return 0
        try:
            n = int(self._session.main_team.model.count_tokens(messages))
        except Exception as exc:
            logger.debug("count_tokens failed (%s); reporting 0", exc)
            return 0
        # Latch only when we actually measured something. Latching 0
        # turns a transient "session not loaded yet / aget_session
        # raced with attach" into a permanent 0 in the footer until
        # the next run fires — exactly the bug the field saw on
        # session-switch. ``0`` from this RPC means "couldn't
        # measure right now"; leave the previous good value alone
        # and let the next call (or the next run) overwrite.
        if n > 0:
            self._session._last_input_tokens = n
        return n

    async def compact_if_needed(self, ctx_tokens: int, max_ctx: int) -> msg.SessionCleared | None:
        """Compact session if approaching context limit."""
        compacted = await self._session.compact_if_needed(ctx_tokens, max_ctx)
        if compacted:
            summary = ""
            with contextlib.suppress(Exception):
                agno_session = await self._session.main_team.aget_session(
                    session_id=self._session.session_id,
                    user_id=self._session.user_id,
                )
                if agno_session and agno_session.summary and agno_session.summary.summary:
                    summary = agno_session.summary.summary
            return msg.SessionCleared(
                new_session_id=self._session.session_id,
                summary=summary,
            )
        return None

    # ── Learning ──────────────────────────────────────────────────

    async def extract_learnings(self, user_msg: str, assistant_msg: str) -> None:
        """Run learning extraction as a background task on the main event loop.

        Uses the main loop (not a separate thread) so the httpx client's
        connection pool works correctly.
        """
        learning = self._session._learning
        if learning is None:
            return

        from agno.models.message import Message as AgnoMessage

        messages = [AgnoMessage(role="user", content=user_msg)]
        if assistant_msg:
            messages.append(AgnoMessage(role="assistant", content=assistant_msg))

        async def _run() -> None:
            try:
                await learning.aprocess(
                    messages=messages,
                    user_id=self._session.user_id,
                    session_id=self._session.session_id,
                )
            except Exception as exc:
                logger.warning("Learning extraction failed: %s", exc)

        asyncio.create_task(_run())

    # ── Cleanup ───────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Graceful shutdown — disconnect MCP, fire hooks, kill bg processes."""
        from ember_code.core.hooks.events import HookEvent
        from ember_code.core.tools.shell import EmberShellTools

        with contextlib.suppress(Exception):
            await self._session.hook_executor.execute(
                event=HookEvent.SESSION_END.value,
                payload={"session_id": self._session.session_id},
            )
        with contextlib.suppress(Exception):
            if self._session.settings.orchestration.auto_cleanup:
                self._session.pool.cleanup_ephemeral()
        with contextlib.suppress(Exception):
            await self._session.mcp_manager.disconnect_all()
        with contextlib.suppress(Exception):
            killed = EmberShellTools.cleanup()
            if killed:
                logger.info("Shutdown: killed %d background process(es)", killed)

    # ── Accessors for FE (read-only state) ──────────────────────

    def wire_queue_hook(self, queue: list) -> None:
        """Wire queue hooks onto the team.

        - Tool-hook (injector) drains the queue after each tool call so the
          model sees queued text on its next iteration.
        - Post-hook (persister) records those drained items as proper
          user-role history entries before the session is saved.
        """
        from ember_code.core.queue_hook import create_queue_hook

        injector, persister = create_queue_hook(queue=queue)
        team = self._session.main_team
        existing_tool_hooks = team.tool_hooks or []
        team.tool_hooks = [*existing_tool_hooks, injector]
        existing_post_hooks = team.post_hooks or []
        team.post_hooks = [*existing_post_hooks, persister]

    def wire_orchestrate_progress(self, callback) -> None:
        """Set a progress callback on the orchestrate tool."""
        from ember_code.core.tools.orchestrate import OrchestrateTools

        for tool in self._session.main_team.tools or []:
            if isinstance(tool, OrchestrateTools):
                tool._on_progress = callback
                break

    @staticmethod
    async def _close_model_http_client(team: Any) -> None:
        """Close the httpx client on the model to release open HTTP streams.

        When an Agno run finishes or is cancelled mid-stream, the underlying
        httpx connection to the API may stay open indefinitely. Closing the
        client ensures the TCP connection is torn down promptly so the server
        can release concurrency slots. A fresh client is assigned so the model
        remains usable for subsequent runs.
        """
        import httpx as _httpx

        try:
            model = getattr(team, "model", None)
            client = getattr(model, "http_client", None) if model else None
            if isinstance(client, _httpx.AsyncClient):
                await asyncio.wait_for(client.aclose(), timeout=3)
        except Exception as exc:
            logger.debug("Failed to close model HTTP client: %s", exc)

        # Always ensure a fresh client, even if close failed.
        # The old client's connections will eventually timeout.
        if model is not None:
            model.http_client = _httpx.AsyncClient(
                limits=_httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=30,
                ),
            )

    def cancel_agent_run(self, run_id: str) -> dict:
        """Cancel a specific sub-agent run by its Agno ``run_id``.

        Used by the team-progress UI when the user wants to stop one
        specialist mid-broadcast without killing the whole team. Agno
        flags the run for cooperative cancellation — the sub-agent
        bails at its next ``await`` boundary, siblings keep going.

        Returns ``{ok: bool}`` so the FE can show a quick toast on
        failure (mostly: unknown run_id).
        """
        if not run_id:
            return {"ok": False, "error": "missing run_id"}
        try:
            from agno.agent import Agent

            Agent.cancel_run(run_id)
            logger.info("Cancelled sub-agent run %s", run_id)
            return {"ok": True}
        except Exception as exc:
            logger.warning("cancel_agent_run failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def cancel_run(self) -> None:
        """Cancel the currently running agent and kill any foreground process.

        Three mechanisms fire in order:
          1. Kill the active foreground shell subprocess (so a blocking
             ``run_shell_command`` returns immediately).
          2. Flag the run for cooperative cancel via Agno
             (``Agent.cancel_run``) — propagates to the team's main loop
             and any sub-agents that check the flag.
          3. Hard-cancel the asyncio task iterating ``run_message`` —
             unblocks any ``await`` that Agno's cooperative cancel
             can't reach (notably tool calls deep inside specialist
             sub-agents during a ``broadcast`` team).
        """
        from ember_code.core.tools.shell import cancel_foreground

        if cancel_foreground():
            logger.info("Killed foreground process on cancel")

        try:
            from agno.agent import Agent

            team = self._session.main_team
            run_id = getattr(team, "run_id", None)
            if run_id:
                Agent.cancel_run(run_id)
        except Exception as exc:
            logger.debug("Failed to cancel run: %s", exc)

        task = self._current_run_task
        if task and not task.done():
            logger.info("Cancelling run task %s", task.get_name())
            task.cancel()

    @property
    def processing(self) -> bool:
        return self._processing

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def run_timeout(self) -> int:
        return self._settings.models.max_run_timeout

    @property
    def skill_names(self) -> list[str]:
        """Skill names for input autocomplete — FE needs these for the input handler."""
        return [s.name for s in self._session.skill_pool.list_skills()]

    def get_skill_pool(self):
        """Return the skill pool for input autocomplete."""
        return self._session.skill_pool

    async def get_mcp_server_details(self) -> list[dict]:
        """Full MCP server info for the panel UI."""
        mgr = self._session.mcp_manager
        servers = []
        for name in mgr.list_servers():
            config = mgr.configs.get(name)
            connected = name in mgr.list_connected()
            servers.append(
                {
                    "name": name,
                    "connected": connected,
                    "transport": config.type if config else "unknown",
                    "tool_names": mgr.get_tools(name),
                    "tool_descriptions": mgr.get_tool_descriptions(name),
                    "tools_disabled": mgr.get_disabled_tools(name),
                    "resources": await mgr.get_resources(name) if connected else [],
                    "prompts": await mgr.get_prompts(name) if connected else [],
                    "error": mgr.get_error(name),
                    "policy_blocked": mgr._policy.is_denied(name),
                }
            )
        return servers

    async def get_pending_messages(self, session_id: str) -> list[dict]:
        """Pending user messages that never completed a run.

        Surfaced by the FE on ``--continue`` to render the user's
        interrupted question(s) in the conversation pane — Agno's
        own ``get_chat_history`` doesn't return them because Agno
        only persists at end-of-run, so a crash mid-stream leaves
        the message visible only in our pre-persistence table.
        Returns ``[{role: "user", content: "<text>", received_at: <ts>}, ...]``
        in oldest-first order.
        """
        try:
            rows = await self._pending_store.alist_pending(session_id)
        except Exception as exc:
            logger.debug("get_pending_messages failed: %s", exc)
            return []
        # A fresh pending row almost always means "Agno is still
        # finishing its post-stream tail" (it can take 15-30s). The
        # surfaced "1 message(s) were interrupted" banner is meant
        # for actual crashes across BE restarts — filter to rows
        # older than 60s so a reload during the tail stays quiet.
        import time as _time

        cutoff = int(_time.time()) - 60
        rows = [r for r in rows if r.received_at <= cutoff]
        return [
            {
                "role": "user",
                "content": r.text,
                "received_at": r.received_at,
                "message_id": r.message_id,
            }
            for r in rows
        ]

    def upload_attachment(self, filename: str, content_base64: str) -> dict:
        """Persist a FE-uploaded file (OS picker / drag / paste) to a
        per-session attachments dir so the agent can read it on
        demand via its Read tool.

        Returns ``{path, size}``. Content is base64 so the FE can
        ship arbitrary bytes (PDFs, images) over the JSON wire.
        """
        import base64
        import re

        # Strip path separators / nasty chars so the FE can't write
        # outside the attachments dir.
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "file"
        dest_dir = self._session.project_dir / ".ember" / "attachments" / self._session.session_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe
        # If a same-name file already exists, suffix to avoid overwrite.
        if dest.exists():
            stem, dot, ext = safe.rpartition(".")
            base = stem if dot else safe
            suffix = ext if dot else ""
            n = 2
            while dest.exists():
                dest = dest_dir / (f"{base}-{n}{('.' + suffix) if suffix else ''}")
                n += 1
        try:
            data = base64.b64decode(content_base64)
        except Exception as exc:
            return {"path": "", "size": 0, "error": f"invalid base64: {exc}"}
        dest.write_bytes(data)
        return {"path": str(dest), "size": len(data)}

    async def get_chat_history(self, session_id: str) -> list[dict]:
        """Get chat history for a session. Returns a list of turn dicts.

        Turn shapes:
          * ``{role: "user", content, run_id, created_at}``
          * ``{role: "assistant", content, run_id, created_at}``
          * ``{role: "thinking", content, run_id, created_at}`` —
            synthesized from the preceding assistant message's
            ``reasoning_content`` so a closed session re-opens with
            the same thinking sections that were live the first time.
          * ``{role: "tool", tool_name, friendly_name, tool_args,
            content (= result), is_error, run_id, created_at}`` —
            rebuilds the tool cards on restore (live path emits
            ``tool_started`` + ``tool_completed`` events; history
            squashes the two into one turn).
          * ``{role: "plan", plan, tasks, state, run_id, created_at}``
            — synthesized in place of an ``exit_plan_mode`` tool
            result so the PlanCard renders at the chronological
            position where the agent submitted the plan, not in a
            separate "all plans at the end" pile.
          * ``{role: "stats", run_id, input_tokens, output_tokens,
            reasoning_tokens, duration}`` — per-run token badge.

        ``run_id`` lets the FE map a user message back to its owning
        run for edit/delete truncation. Skips sub-agent runs
        (parent_run_id set) and ``system`` messages.
        """
        from ember_code.protocol.agno_events import TOOL_NAMES

        agent = self._session.main_team
        agno_session = await agent.aget_session(
            session_id=session_id,
            user_id=self._session.user_id,
        )
        if agno_session is None:
            return []
        runs = getattr(agno_session, "runs", None) or []
        out: list[dict] = []
        # Running char count across the FULL prompt the model sees on
        # each turn: system prompt + tool defs + conversation history
        # (user / assistant / tool results) + this turn's user
        # message. chars/4 is the same coarse estimator the FE uses
        # for ``visibleOutTokens``. Per-turn input is monotonic — it
        # grows as the chat grows, matching the user's intuition.
        history_chars = 0  # accumulated user/assistant/tool content so far
        system_chars = 0  # the constant system + tool-defs overhead, captured once
        for run in runs:
            if getattr(run, "parent_run_id", None):
                continue
            run_id = str(getattr(run, "run_id", "") or "")
            messages = getattr(run, "messages", None) or []
            # Snapshot BEFORE walking this run's messages — that's the
            # context the model saw on its way into this turn (not yet
            # including this turn's user message).
            input_chars = history_chars
            assistant_chars = 0
            # Track exit_plan_mode tool calls within this run so we
            # can render a PlanCard in place of the regular tool turn
            # when the tool result lands. ``tool_call_id`` (set on
            # both the assistant's ``tool_calls`` entry and the
            # subsequent tool message) is the correlation key.
            plan_calls_in_run: dict[str, dict] = {}
            for m in messages:
                if getattr(m, "from_history", False):
                    continue
                role = getattr(m, "role", "")
                content = m.content if isinstance(m.content, str) else str(m.content or "")
                created_at = int(getattr(m, "created_at", 0) or 0)
                # System messages are the system-prompt + tool-defs
                # overhead the model receives on every API call.
                # Same content on every run — capture once and add as
                # a constant to every input estimate.
                if role == "system":
                    if not system_chars:
                        system_chars = len(content)
                    continue
                # Tool result messages — rebuild the live tool card,
                # UNLESS this is the result of an exit_plan_mode call:
                # then emit a PlanCard turn instead so the card
                # appears at the point in the chat where the agent
                # actually submitted the plan, not bolted onto the end.
                if role == "tool":
                    tool_name = str(getattr(m, "tool_name", "") or "")
                    tool_call_id = str(getattr(m, "tool_call_id", "") or "")
                    if tool_call_id and tool_call_id in plan_calls_in_run:
                        plan_args = plan_calls_in_run.pop(tool_call_id)
                        plan_text = str(plan_args.get("plan", "")).strip()
                        if plan_text:
                            out.append(
                                {
                                    "role": "plan",
                                    "plan": plan_text,
                                    "tasks": plan_args.get("tasks") or [],
                                    # State filled in post-walk: only
                                    # the LATEST plan turn gets the
                                    # inferred state; older plans are
                                    # always "approved" (historical).
                                    "state": "approved",
                                    "run_id": run_id,
                                    "created_at": created_at,
                                }
                            )
                            history_chars += len(content)
                            continue
                    tool_args_raw = getattr(m, "tool_args", None)
                    if isinstance(tool_args_raw, (dict, list)):
                        args_summary = _format_tool_args_for_restore(tool_args_raw)
                    elif tool_args_raw is None:
                        args_summary = ""
                    else:
                        args_summary = str(tool_args_raw)
                    out.append(
                        {
                            "role": "tool",
                            "tool_name": tool_name,
                            "friendly_name": TOOL_NAMES.get(tool_name, tool_name),
                            "args": args_summary,
                            "content": content,
                            "is_error": bool(getattr(m, "tool_call_error", False)),
                            "run_id": run_id,
                            "created_at": created_at,
                        }
                    )
                    history_chars += len(content)
                    continue
                # Assistant message: handle two thinking sources and
                # interleave with the visible reply so the restored
                # chat reads in the same order the live stream
                # produced (thinking → reply → maybe more thinking).
                #
                # Source 1: Agno's ``reasoning_content`` field — set
                # by providers that expose reasoning as a sidecar
                # stream (Anthropic-style). One thinking block,
                # logically BEFORE the visible reply.
                #
                # Source 2: inline ``<think>...</think>`` tags inside
                # the content itself (MiniMax-style). Split the
                # content and interleave assistant + thinking turns
                # in occurrence order.
                #
                # Also stash any ``exit_plan_mode`` tool calls keyed
                # by call_id so the later tool result can be rewritten
                # as a PlanCard turn.
                if role == "assistant":
                    reasoning = getattr(m, "reasoning_content", None)
                    if isinstance(reasoning, str) and reasoning.strip():
                        out.append(
                            {
                                "role": "thinking",
                                "content": reasoning,
                                "run_id": run_id,
                                "created_at": created_at,
                            }
                        )
                    for tc in getattr(m, "tool_calls", None) or []:
                        if not isinstance(tc, dict):
                            continue
                        fn = tc.get("function") or {}
                        if fn.get("name") != "exit_plan_mode":
                            continue
                        args_raw = fn.get("arguments")
                        if isinstance(args_raw, str):
                            try:
                                parsed = json.loads(args_raw)
                            except Exception:
                                continue
                        elif isinstance(args_raw, dict):
                            parsed = args_raw
                        else:
                            continue
                        call_id = str(tc.get("id") or "")
                        if call_id:
                            plan_calls_in_run[call_id] = parsed
                    # Source 2: split inline <think> tags out of the
                    # content. Each segment becomes its own turn.
                    for part_role, part_text in _split_assistant_content_for_restore(content):
                        out.append(
                            {
                                "role": part_role,
                                "content": part_text,
                                "run_id": run_id,
                                "created_at": created_at,
                            }
                        )
                        if part_role == "assistant":
                            assistant_chars += len(part_text)
                    # Count the full original content toward history
                    # — that's what the model actually saw on the
                    # next turn (including the think tags).
                    history_chars += len(content)
                    continue
                # User turn — display AND count. Carry the
                # message's ``created_at`` (Agno-issued epoch seconds)
                # so the FE can stamp each turn with a real time.
                out.append(
                    {
                        "role": role,
                        "content": content,
                        "run_id": run_id,
                        "created_at": created_at,
                    }
                )
                history_chars += len(content)
                if role == "user":
                    # The user message of this run lands in the model's
                    # input but not in the pre-run snapshot.
                    input_chars += len(content)
            metrics = getattr(run, "metrics", None)
            # Input / output are ALWAYS chars/4 estimates of the model's
            # actual prompt — NOT Agno's billed numbers. Reason: Agno's
            # ``run.metrics.input_tokens`` sums across model iterations
            # within a turn (agent reasoning loops, tool re-prompts),
            # so the same conversation reads as non-monotonic. The live
            # path corrects this via ``count_context_tokens`` after each
            # run, but historical runs have no corrected number to
            # restore. Estimate = system + history + this-turn user
            # message, all chars/4.
            full_input_chars = system_chars + input_chars
            input_tokens = max(1, full_input_chars // 4) if full_input_chars else 0
            output_tokens = max(1, assistant_chars // 4) if assistant_chars else 0
            # After estimation, an all-zero stats line means the run
            # had no visible content at all (degenerate / empty run);
            # nothing to display.
            if input_tokens or output_tokens:
                out.append(
                    {
                        "role": "stats",
                        "run_id": run_id,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "reasoning_tokens": int(getattr(metrics, "reasoning_tokens", 0) or 0)
                        if metrics
                        else 0,
                        "duration": float(getattr(metrics, "duration", 0) or 0) if metrics else 0.0,
                    }
                )
        # The LATEST plan turn gets its state inferred from the
        # current permission mode. Older plan turns stay "approved"
        # (historical — the user is past them). Without this, every
        # restored plan card would show as approved including the
        # one the user is still deciding on.
        for turn in reversed(out):
            if turn.get("role") == "plan":
                turn["state"] = self._infer_plan_state(turn.get("plan", ""))
                break
        return out

    async def search_chat(self, session_id: str, query: str, limit: int = 50) -> list[dict]:
        """Case-insensitive substring search across the persisted
        history of ``session_id``. Walks runs from the Agno SQLite
        session and emits matches with a ``history_index`` that lines
        up with ``get_chat_history``'s emission order — the FE keeps a
        parallel ``historyIndex -> itemIndex`` map built at session
        load so the result can be mapped straight to a chat item.

        Returns at most ``limit`` matches in chronological order:
          ``{history_index, role, run_id, snippet, match_start,
             match_end}``
        ``match_start``/``match_end`` are offsets within ``snippet``
        (NOT the full content) so the FE can highlight without
        bookkeeping the original string.
        """
        needle = (query or "").strip()
        if not needle:
            return []
        history = await self.get_chat_history(session_id)
        return _search_history(history, needle, limit)

    async def truncate_history(self, session_id: str, run_id: str) -> dict:
        """Drop the run with ``run_id`` and every later run from the
        session. Used by the FE when the user edits or deletes one of
        their past messages — both operations require that everything
        downstream of the touched turn gets wiped before continuing.
        Returns ``{removed: N}``; ``N=0`` if ``run_id`` wasn't found.
        """
        agent = self._session.main_team
        agno_session = await agent.aget_session(
            session_id=session_id,
            user_id=self._session.user_id,
        )
        if agno_session is None:
            return {"removed": 0, "error": "session not found"}
        runs = list(getattr(agno_session, "runs", None) or [])
        cut_idx: int | None = None
        for i, r in enumerate(runs):
            if getattr(r, "parent_run_id", None):
                continue
            if str(getattr(r, "run_id", "") or "") == run_id:
                cut_idx = i
                break
        if cut_idx is None:
            return {"removed": 0, "error": f"run_id {run_id!r} not in session"}
        removed = len(runs) - cut_idx
        agno_session.runs = runs[:cut_idx]
        try:
            await agent.asave_session(agno_session)
        except Exception as exc:
            logger.exception("truncate_history: save failed")
            return {"removed": 0, "error": str(exc)}
        # The latched context-token count was computed against the
        # pre-truncate session; invalidate it so the next status read
        # recomputes from the new (shorter) history.
        self._session._last_input_tokens = 0
        return {"removed": removed}

    def get_mcp_servers(self) -> list[dict]:
        """MCP server info for the panel."""
        mgr = self._session.mcp_manager
        servers = []
        for name in mgr.list_servers():
            connected = name in mgr.list_connected()
            servers.append({"name": name, "connected": connected})
        return servers

    async def mcp_connect(self, server_name: str) -> msg.Info:
        """Connect a single MCP server."""
        await self._session.mcp_manager.connect(server_name)
        self._session.rebuild_mcp()
        return msg.Info(text=f"Connected MCP: {server_name}")

    async def mcp_disconnect(self, server_name: str) -> msg.Info:
        """Disconnect a single MCP server."""
        await self._session.mcp_manager.disconnect_one(server_name)
        self._session.rebuild_mcp()
        return msg.Info(text=f"Disconnected MCP: {server_name}")

    # ── Agents ─────────────────────────────────────────────────────

    def get_agent_details(self) -> list[AgentInfo]:
        """Snapshot of every loaded agent for the panel UI.

        Combines :meth:`AgentPool.list_agents` with the ephemeral
        directory check so the panel can render the "ephemeral" badge
        + show the promote/discard actions without making a second
        RPC call. Includes the full ``system_prompt`` since the panel
        expands it inline on Enter.
        """
        from ember_code.core.pool import AgentInfo

        pool = self._session.pool
        ephemeral_dir = getattr(pool, "_ephemeral_dir", None)
        results: list[AgentInfo] = []
        for defn in pool.list_agents():
            is_ephemeral = bool(
                ephemeral_dir and defn.source_path and ephemeral_dir in defn.source_path.parents
            )
            results.append(
                AgentInfo(
                    name=defn.name,
                    description=defn.description,
                    tools=list(defn.tools),
                    model=defn.model or "",
                    color=defn.color or "",
                    can_orchestrate=defn.can_orchestrate,
                    mcp_servers=list(defn.mcp_servers),
                    tags=list(defn.tags),
                    system_prompt=defn.system_prompt,
                    source_path=str(defn.source_path) if defn.source_path else "",
                    is_ephemeral=is_ephemeral,
                )
            )
        return results

    def promote_ephemeral_agent(self, name: str) -> msg.Info:
        """Save an ephemeral agent permanently (called from the panel)."""
        try:
            dest = self._session.pool.promote_ephemeral(name, self._session.project_dir)
        except (KeyError, ValueError, RuntimeError) as e:
            return msg.Info(text=str(e))
        return msg.Info(text=f"Promoted '{name}' to {dest}")

    def discard_ephemeral_agent(self, name: str) -> msg.Info:
        """Delete an ephemeral agent (called from the panel)."""
        try:
            self._session.pool.discard_ephemeral(name)
        except (KeyError, ValueError, RuntimeError) as e:
            return msg.Info(text=str(e))
        return msg.Info(text=f"Discarded ephemeral agent '{name}'.")

    # ── Skills ─────────────────────────────────────────────────────

    # ── Knowledge ──────────────────────────────────────────────────

    async def get_knowledge_status(self) -> dict:
        """Status snapshot for the knowledge panel header.

        Returns a plain dict (vs a typed model) because
        ``KnowledgeStatus`` already serializes cleanly and this is a
        read-only display payload — no behavior depends on the type."""
        status = await self._session.knowledge_mgr.status()
        return {
            "enabled": status.enabled,
            "collection_name": status.collection_name,
            "document_count": status.document_count,
            "embedder": status.embedder,
        }

    async def knowledge_search(self, query: str) -> list[dict]:
        """Search the knowledge base. Returns one dict per hit with
        ``name``, ``content``, ``score``, ``metadata`` — same shape
        the panel reconstructs into ``KnowledgeSearchHit``."""
        response = await self._session.knowledge_mgr.search(query)
        return [
            {
                "name": r.name,
                "content": r.content,
                "score": r.score,
                "metadata": dict(r.metadata),
            }
            for r in response.results
        ]

    async def knowledge_add(self, source: str) -> msg.Info:
        """Add content to the knowledge base from the panel. Dispatch
        rules mirror ``/knowledge add <source>``: HTTP URLs → URL
        ingest, path-shaped strings → file/dir ingest, anything else
        → inline text."""
        mgr = self._session.knowledge_mgr
        if source.startswith(("http://", "https://")):
            result = await mgr.add_url(source)
        elif "/" in source or source.startswith("."):
            result = await mgr.add_path(source)
        else:
            result = await mgr.add(text=source)
        if not result.success:
            return msg.Info(text=result.error or "Add failed.")
        return msg.Info(text=result.message)

    async def knowledge_list(self) -> list[dict]:
        """Every document in the KB — used by the panel's Browse tab.

        Returns one dict per entry shaped for the panel:
        ``{id, name, source, size, preview, added_at, metadata}``.
        ``name`` is the source basename or the first non-empty line
        of content when no source path is available (e.g. inline
        text), so the Browse list always has a meaningful label.
        """
        from pathlib import PurePosixPath

        knowledge = self._session.knowledge_mgr.knowledge
        if knowledge is None:
            return []
        try:
            entries = await knowledge.list_entries()
        except Exception as exc:
            logger.debug("knowledge_list failed: %s", exc)
            return []

        def _name_for(entry: dict) -> str:
            source = (entry.get("source") or "").strip()
            if source:
                if source.startswith(("http://", "https://")):
                    return source
                return PurePosixPath(source).name or source
            content = (entry.get("content") or "").strip()
            for line in content.splitlines():
                line = line.strip().lstrip("# ").strip()
                if line:
                    return line[:80]
            return "(untitled)"

        out: list[dict] = []
        for e in entries:
            content = e.get("content") or ""
            meta = e.get("metadata") or {}
            out.append(
                {
                    "id": e.get("id", ""),
                    "name": _name_for(e),
                    "source": e.get("source", ""),
                    "size": len(content),
                    "preview": content[:240],
                    "added_at": str(meta.get("added_at", "")),
                    "kind": str(meta.get("kind", "")),
                    "metadata": {k: str(v) for k, v in meta.items() if v is not None},
                }
            )
        # Newest first when ``added_at`` is comparable; otherwise
        # stable order.
        out.sort(key=lambda d: d.get("added_at", ""), reverse=True)
        return out

    async def knowledge_get(self, entry_id: str) -> dict:
        """Full content for one document — used by the detail page."""
        knowledge = self._session.knowledge_mgr.knowledge
        if knowledge is None:
            return {"error": "Knowledge base is disabled."}
        try:
            entries = await knowledge.list_entries()
        except Exception as exc:
            return {"error": f"knowledge_get failed: {exc}"}
        match = next((e for e in entries if e.get("id") == entry_id), None)
        if not match:
            return {"error": f"Document {entry_id} not found."}
        meta = match.get("metadata") or {}
        return {
            "id": entry_id,
            "name": (match.get("source") or "").strip() or entry_id,
            "source": match.get("source", ""),
            "content": match.get("content", ""),
            "metadata": {k: str(v) for k, v in meta.items() if v is not None},
        }

    def read_file(self, path: str) -> dict:
        """Read a small text file for FE preview.

        Sandboxed: the resolved path must live under the current
        project dir OR under ``~/.ember`` (covers global hooks,
        settings, plugin sources). Anywhere else returns an error
        rather than reading — this is for read-only UI previews, not
        a general file API.
        """
        from os.path import expanduser

        try:
            requested = Path(path).expanduser()
            if not requested.is_absolute():
                requested = (self._session.project_dir / requested).resolve()
            else:
                requested = requested.resolve()
        except Exception as exc:
            return {"path": path, "contents": "", "size": 0, "error": f"bad path: {exc}"}

        project_root = Path(self._session.project_dir).resolve()
        ember_root = Path(expanduser("~/.ember")).resolve()
        if not (_is_within(requested, project_root) or _is_within(requested, ember_root)):
            return {
                "path": str(requested),
                "contents": "",
                "size": 0,
                "error": (
                    "Refused: path is outside the project and ~/.ember. "
                    "Open it in your editor instead."
                ),
            }
        if not requested.exists():
            return {
                "path": str(requested),
                "contents": "",
                "size": 0,
                "error": "File not found.",
            }
        if requested.is_dir():
            return {
                "path": str(requested),
                "contents": "",
                "size": 0,
                "error": "Path is a directory.",
            }

        # Cap at 256 KB. ``read_file`` is only invoked by the plain-
        # browser fallback preview — Tauri / VSCode / JetBrains hosts
        # always go through their native open bridge and never hit
        # this path. So this isn't a policy on what's openable, it's
        # a guard for the in-app preview which isn't meant to be an
        # editor for large files.
        MAX = 256 * 1024
        size = requested.stat().st_size
        if size > MAX:
            return {
                "path": str(requested),
                "contents": "",
                "size": size,
                "error": f"File too large to preview ({size} bytes; cap {MAX}).",
            }
        try:
            contents = requested.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as exc:
            return {
                "path": str(requested),
                "contents": "",
                "size": size,
                "error": f"read failed: {exc}",
            }
        return {
            "path": str(requested),
            "contents": contents,
            "size": size,
            "language": _guess_language(requested.suffix),
        }

    def search_code(self, snippet: str, max_results: int = 20) -> dict:
        """Find exact-substring occurrences of ``snippet`` across the
        project. Used by the composer's paste handler — when the user
        pastes code, the FE asks "where does this live?" so it can
        decorate the message with refs.

        Strategy:
          - Use ``rg`` if available (parallel, gitignore-aware, fast).
          - Otherwise walk the project with Python, skipping noisy dirs.

        Match mode is **exact substring** — no normalisation, no
        fuzzy. Multi-line snippets become a single literal pattern.

        Returns ``{matches: [{path, line, end_line, preview}], truncated: bool}``.
        ``path`` is project-relative. ``line`` is the start line of
        the match; ``end_line`` is computed from the snippet itself
        (start + newline count) so the pill can label a 5-line paste
        as ``71-75`` instead of just ``71`` — rg's ``--multiline``
        only reports the start line and the FE can't derive the end
        without knowing the snippet here on the BE.

        Repeated pastes of the same snippet (re-pasting after an edit,
        the model echoing code back) hit a small in-process cache so
        only the first lookup pays for the rg spawn.
        """
        import hashlib
        import shutil
        import subprocess

        snippet = (snippet or "").strip()
        if len(snippet) < 5:
            return {"matches": [], "truncated": False}

        # ── Result cache ──
        # Bounded to a few dozen entries; rotates by reinsertion order
        # (Python dicts preserve insertion order). The key includes the
        # project root so switching directories doesn't serve stale
        # results.
        project_root = Path(self._session.project_dir).resolve()
        cache_key = hashlib.sha1(
            f"{project_root}\0{max_results}\0{snippet}".encode("utf-8", "ignore")
        ).hexdigest()
        cache: dict[str, dict] = getattr(self, "_search_code_cache", None) or {}
        if not hasattr(self, "_search_code_cache"):
            self._search_code_cache = cache
        cached = cache.get(cache_key)
        if cached is not None:
            # Move to MRU position.
            cache.pop(cache_key, None)
            cache[cache_key] = cached
            return cached

        # Used below for the end_line calculation. ``rg --multiline``
        # emits one row per match (start line only), so we derive the
        # end from the snippet structure itself.
        snippet_lines = snippet.count("\n") + 1

        results: list[dict] = []
        truncated = False

        rg = shutil.which("rg")
        if rg:
            # --fixed-strings: literal pattern (no regex)
            # --line-number: include line numbers
            # --no-heading: machine-friendly output
            # --multiline: required for snippets with newlines
            # --max-count: per-file cap
            # Newlines inside the snippet require --multiline+--multiline-dotall.
            cmd = [
                rg,
                "--fixed-strings",
                "--line-number",
                "--no-heading",
                "--color=never",
                "--max-count=5",
                "--max-filesize=2M",
                "--multiline",
                "--multiline-dotall",
                snippet,
                str(project_root),
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except subprocess.TimeoutExpired:
                return {"matches": [], "truncated": True, "error": "search timed out"}
            for raw_line in (proc.stdout or "").splitlines():
                # Format: "/abs/path:LINE:preview"
                parts = raw_line.split(":", 2)
                if len(parts) < 3:
                    continue
                abs_path, line_str, preview = parts
                try:
                    line = int(line_str)
                except ValueError:
                    continue
                try:
                    rel = str(Path(abs_path).resolve().relative_to(project_root))
                except ValueError:
                    rel = abs_path
                results.append(
                    {
                        "path": rel,
                        "line": line,
                        "end_line": line + snippet_lines - 1,
                        "preview": preview.strip(),
                    }
                )
                if len(results) >= max_results:
                    truncated = True
                    break
            payload = {"matches": results, "truncated": truncated}
            _search_code_cache_put(cache, cache_key, payload)
            return payload

        # ── Python fallback (no rg) ──
        # Walk text-ish files, scan line-by-line for the first line of
        # the snippet, then verify the full snippet at that offset.
        SKIP_DIRS = {
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            "dist",
            "build",
            ".next",
            "target",
            ".idea",
            ".vscode",
        }
        first_line = snippet.splitlines()[0]
        for dirpath, dirnames, filenames in os.walk(project_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    if p.stat().st_size > 2 * 1024 * 1024:
                        continue
                    text = p.read_text(encoding="utf-8", errors="replace")
                except (OSError, UnicodeDecodeError):
                    continue
                idx = text.find(snippet)
                if idx < 0:
                    continue
                line_no = text.count("\n", 0, idx) + 1
                try:
                    rel = str(p.resolve().relative_to(project_root))
                except ValueError:
                    rel = str(p)
                results.append(
                    {
                        "path": rel,
                        "line": line_no,
                        "end_line": line_no + snippet_lines - 1,
                        "preview": first_line.strip(),
                    }
                )
                if len(results) >= max_results:
                    truncated = True
                    payload = {"matches": results, "truncated": truncated}
                    _search_code_cache_put(cache, cache_key, payload)
                    return payload
        payload = {"matches": results, "truncated": truncated}
        _search_code_cache_put(cache, cache_key, payload)
        return payload

    async def knowledge_remove(self, entry_id: str) -> dict:
        """Delete one document by id. Returns ``{removed: bool}`` so
        the FE can confirm and refresh the list optimistically."""
        knowledge = self._session.knowledge_mgr.knowledge
        if knowledge is None:
            return {"removed": False, "error": "Knowledge base is disabled."}
        try:
            removed = await knowledge.delete_entry(entry_id)
            return {"removed": removed}
        except Exception as exc:
            return {"removed": False, "error": str(exc)}

    # ── Hooks ──────────────────────────────────────────────────────

    def get_hooks_details(self) -> list[dict]:
        """Snapshot of every active hook for the hooks panel.

        Walks ``session.hooks_map`` (which is ``{event: [hook, ...]}``
        after the four-root merge + plugin prepend) and flattens
        into one dict per (event, hook) pair. The panel groups
        client-side by ``event`` for display.

        Plain dicts vs. typed wire model because the panel-side
        :class:`HookInfo` lives in the widget — keeping the BE
        side dict-flat avoids a cross-side schema import and the
        fields here are display-only (no behavior depends on the
        type).
        """
        out: list[dict] = []
        for event, hooks in self._session.hooks_map.items():
            for hook in hooks:
                out.append(
                    {
                        "event": str(event),
                        "type": getattr(hook, "type", ""),
                        "command": getattr(hook, "command", "") or "",
                        "url": getattr(hook, "url", "") or "",
                        "matcher": getattr(hook, "matcher", "") or "",
                        "timeout_ms": int(getattr(hook, "timeout", 0) or 0),
                        "background": bool(getattr(hook, "background", False)),
                        "headers": dict(getattr(hook, "headers", {}) or {}),
                    }
                )
        return out

    def reload_hooks_rpc(self) -> msg.Info:
        """Reload hooks from disk. Returns count for the panel toast.

        Distinct name from ``Session.reload_hooks`` so the RPC
        dispatch lambda can reference a stable method here without
        colliding with the session-level helper (which returns an
        int, not the FE-facing ``msg.Info``).
        """
        count = self._session.reload_hooks()
        return msg.Info(text=f"Reloaded hooks — {count} active hook(s) across all events.")

    # ── CodeIndex ──────────────────────────────────────────────────

    async def codeindex_status(self) -> dict:
        """Status snapshot for the CodeIndex panel header.

        Focuses on the *current commit*: whether HEAD is indexed
        locally, whether the server is still indexing it (with the
        latest preflight progress %), and the install state.

        Designed to be cheap and read-only so the panel can poll it
        every couple of seconds without firing extra ``sync_now``
        round-trips — ``sync_progress_pct`` / ``sync_step`` come
        from ``_last_sync_result``, which the watcher (or a manual
        sync) populates on its own cadence.

        Async because ``current_sha`` shells out to ``git`` — running
        it inline on the event loop blocks every other session's RPC
        for the duration of the subprocess (worst case 5 s timeout).
        """
        sync = self._session.code_index_sync
        index = self._session.code_index
        state = index.manifest.load()
        local_sha = (await asyncio.to_thread(sync.current_sha)) or ""
        head_indexed = bool(local_sha) and local_sha in state.commits

        last = sync._last_sync_result
        # ``sync_in_progress`` is True for either a server-side
        # IN_PROGRESS preflight *or* a local apply-delta currently
        # running. Both stretch the panel's "syncing…" state; the
        # apply-side progress is what saved ``/codeindex resync``
        # from looking frozen during the embedding-heavy snapshot
        # apply.
        sync_in_progress = (
            bool(sync._in_progress_sha and sync._in_progress_sha == local_sha) or sync._applying
        )
        # Only surface pct/step when the cached result is *for the
        # current HEAD* and still in-progress. A stale in-progress
        # result from a previous sha would otherwise paint the wrong
        # commit's progress.
        sync_progress_pct: int | None = None
        sync_step = ""
        sync_reason = ""
        sync_error = ""
        if last is not None and last.commit_sha == local_sha:
            sync_reason = last.reason or ""
            sync_error = last.error or ""
            if last.in_progress:
                sync_progress_pct = last.progress_percentage
                sync_step = last.current_step or ""
        # Local apply takes precedence: it's actually running *now*,
        # while ``last.in_progress`` is the most recent preflight
        # report which may be stale.
        if sync._applying and sync._apply_total > 0:
            sync_progress_pct = int(sync._apply_done * 100 / sync._apply_total)
            sync_step = sync._apply_step or "indexing"

        resolved = sync.resolver.cached if sync.resolver else None
        # Lazy resolver kick — when HEAD was already indexed locally
        # ``sync_now`` short-circuits before calling ``resolve()``, so
        # ``cached`` would otherwise stay None and the panel would
        # render "GitHub App: unknown" forever. Fire-and-forget so
        # this call stays cheap; the next poll picks up the result.
        if resolved is None and sync.resolver is not None:
            # No loop = rare; panel will retry shortly.
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(sync.resolver.resolve())
        if resolved is None:
            install_state = "unknown"
            repository_id = ""
            install_url = ""
        elif resolved.needs_install:
            install_state = "needs_install"
            repository_id = ""
            install_url = resolved.install_url or ""
        else:
            install_state = "installed"
            repository_id = resolved.repository_id or ""
            install_url = ""
        # Index stats — cheap walk of the per-commit chroma dirs to
        # compute total size on disk. Doing it inline avoids a
        # background scheduler for what is, in practice, a quick walk
        # (each commit dir is a small chroma snapshot).
        from ember_code.core.code_index.paths import commit_chroma_path

        def _dir_size(p: Path) -> int:
            try:
                return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            except OSError:
                return 0

        index_size_bytes = 0
        branches_indexed: list[dict] = []
        for sha, info in state.commits.items():
            chroma_dir = commit_chroma_path(index.project, sha, data_dir=index.data_dir)
            size = _dir_size(chroma_dir)
            index_size_bytes += size
            branches_indexed.append(
                {
                    "sha": sha,
                    "is_head": sha == state.head,
                    "size_bytes": size,
                    "last_used_at": info.last_used_at,
                    "branch_refs": list(info.branch_refs),
                }
            )
        # Newest-first so the panel can show the most recently used
        # commit at the top of the "branches indexed" list.
        branches_indexed.sort(key=lambda c: c["last_used_at"], reverse=True)

        last_sync_at = ""
        last_sync_stats: dict = {}
        recent = sync.recent_activity()
        if recent:
            top = recent[0]
            last_sync_at = top.ts
            last_sync_stats = {
                "items_upserted": top.items_upserted,
                "items_deleted": top.items_deleted,
            }
        elif last and last.stats:
            last_sync_stats = {
                "items_upserted": last.stats.items_upserted,
                "items_deleted": last.stats.items_deleted,
            }

        return {
            "local_sha": local_sha,
            "remote_url": (sync.resolver.remote_url() if sync.resolver else None) or "",
            "last_synced_sha": sync.last_synced_sha or "",
            "index_head": state.head or "",
            "head_indexed": head_indexed,
            "sync_in_progress": sync_in_progress,
            "sync_progress_pct": sync_progress_pct,
            "sync_step": sync_step,
            "sync_reason": sync_reason,
            "sync_error": sync_error,
            "apply_done": sync._apply_done if sync._applying else 0,
            "apply_total": sync._apply_total if sync._applying else 0,
            "apply_step": sync._apply_step if sync._applying else "",
            "install_state": install_state,
            "repository_id": repository_id,
            "install_url": install_url,
            # New volume/freshness fields
            "commits_indexed": len(state.commits),
            "index_size_bytes": index_size_bytes,
            "branches_indexed": branches_indexed,
            "last_sync_at": last_sync_at,
            "last_sync_stats": last_sync_stats,
        }

    async def codeindex_sync(self, sha: str | None) -> dict:
        """Pull and apply a changeset. ``sha=None`` defaults to HEAD.

        The result is flattened to a dict so the panel can render a
        single message without needing the ``SyncResult`` dataclass
        on the FE side. ``link_start_url`` surfaces the install URL
        when the server returned ``LINK_REQUIRED`` — the panel opens
        it in a browser and prompts the user to retry.
        """
        result = await self._session.code_index_sync.sync_now(sha=sha)
        # If this sync flipped the codeindex from absent → present
        # (or vice versa), rebuild the agent pool + main team so the
        # system prompt matches reality (``main_agent.codeindex.md``
        # vs ``main_agent.md``). Without this, an agent built at
        # session start with an empty chroma keeps saying
        # "CodeIndex isn't active" even after a successful sync.
        try:
            self._session.refresh_codeindex_availability()
        except Exception as exc:
            logger.debug("refresh_codeindex_availability after sync failed (%s)", exc)
        stats = result.stats
        return {
            "skipped": result.skipped,
            "reason": result.reason or "",
            "commit_sha": result.commit_sha or "",
            "error": result.error or "",
            "link_start_url": result.link_start_url or "",
            "items_upserted": stats.items_upserted if stats else 0,
            "items_deleted": stats.items_deleted if stats else 0,
            "references_upserted": stats.references_upserted if stats else 0,
        }

    async def codeindex_resync(self, sha: str | None) -> dict:
        """Wipe the local chroma for ``sha`` (defaults to HEAD) and pull
        a fresh snapshot. Mirrors ``/codeindex resync`` for panel use —
        the underlying recovery path is identical: ``forget_commit`` +
        ``sync_now(force_snapshot=True)``.
        """
        target_sha = sha or (await asyncio.to_thread(self._session.code_index_sync.current_sha))
        forgot = False
        if target_sha:
            forgot = await self._session.code_index.forget_commit(target_sha)
        result = await self._session.code_index_sync.sync_now(sha=target_sha, force_snapshot=True)
        # Same rebuild as ``codeindex_sync``: ``forget_commit`` cleared
        # the chroma and the snapshot just refilled it. The avail flag
        # was likely False during forget, True after the snapshot — so
        # the agent definitely needs the codeindex prompt variant now.
        try:
            self._session.refresh_codeindex_availability()
        except Exception as exc:
            logger.debug("refresh_codeindex_availability after resync failed (%s)", exc)
        stats = result.stats
        return {
            "forgot": forgot,
            "skipped": result.skipped,
            "reason": result.reason or "",
            "commit_sha": result.commit_sha or "",
            "error": result.error or "",
            "link_start_url": result.link_start_url or "",
            "items_upserted": stats.items_upserted if stats else 0,
            "items_deleted": stats.items_deleted if stats else 0,
            "references_upserted": stats.references_upserted if stats else 0,
        }

    async def codeindex_clean(self) -> dict:
        """Drop commits past the retention rules (selective: keeps
        HEAD and every branch tip). Returns the SHAs that were
        dropped so the panel header can refresh."""
        dropped = await self._session.code_index.clean()
        return {"dropped": list(dropped)}

    async def codeindex_head_breakdown(self) -> dict:
        """Repo-at-HEAD signal for the panel: tracked file count
        broken down by language/extension, the last few commits
        with their indexed-or-not flag, AND per-extension indexed
        counts (for the donut's coverage overlay). Slightly heavier
        than ``codeindex_status`` (one chroma scan + git calls), so
        the panel fetches it on open and after each sync — not on
        every 2-second poll.
        """
        import subprocess
        from collections import Counter

        project = self._session.project_dir
        # ``git`` calls run in a thread so a slow git invocation (or its
        # 5s timeout) doesn't block the event loop — under multi-session
        # load this RPC used to stall every other session's dispatch.
        try:
            files = await asyncio.to_thread(
                subprocess.run,
                ["git", "ls-files"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return {
                "file_count": 0,
                "languages": [],
                "recent_commits": [],
                "files_indexed": 0,
                "languages_indexed": {},
                "error": "git not available",
            }
        if files.returncode != 0:
            return {
                "file_count": 0,
                "languages": [],
                "recent_commits": [],
                "files_indexed": 0,
                "languages_indexed": {},
                "error": files.stderr.strip() or "git ls-files failed",
            }

        tracked = [p for p in files.stdout.splitlines() if p]
        ext_counts: Counter[str] = Counter()
        for path in tracked:
            i = path.rfind(".")
            ext = path[i + 1 :].lower() if i > 0 and i < len(path) - 1 else ""
            ext_counts[(ext or "(other)")] += 1
        top_langs = [{"ext": ext, "count": n} for ext, n in ext_counts.most_common(10)]

        # Last 5 commits + indexed flag.
        state = self._session.code_index.manifest.load()
        indexed_shas = set(state.commits.keys())
        try:
            log = await asyncio.to_thread(
                subprocess.run,
                ["git", "log", "-5", "--pretty=format:%H%x09%h%x09%s%x09%cr"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            log = None
        recent_commits: list[dict] = []
        if log and log.returncode == 0:
            for line in log.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                full, short, subj, when = parts[:4]
                recent_commits.append(
                    {
                        "sha": short,
                        "full_sha": full,
                        "subject": subj,
                        "when": when,
                        "indexed": full in indexed_shas,
                    }
                )

        # Per-language indexed counts (HEAD only).
        head_sha = state.head or ""
        files_indexed = 0
        languages_indexed: dict[str, int] = {}
        if head_sha:
            try:
                head = await self._session.code_index.head_stats(head_sha)
                files_indexed = int(head.get("files_indexed", 0))
                languages_indexed = dict(head.get("languages_indexed", {}) or {})
            except Exception as exc:
                logger.debug("head_stats failed: %s", exc)

        return {
            "file_count": len(tracked),
            "languages": top_langs,
            "recent_commits": recent_commits,
            "files_indexed": files_indexed,
            "languages_indexed": languages_indexed,
        }

    def codeindex_activity(self) -> list[dict]:
        """Recent sync events for the panel's activity log."""
        from dataclasses import asdict

        return [asdict(e) for e in self._session.code_index_sync.recent_activity()]

    def codeindex_install(self) -> dict:
        """Return the URL of the Ember portal's repositories page.

        The portal lists the user's connected repos and has an
        ``Add repository`` button that drives the actual GitHub-App
        install flow. The panel opens this URL in a browser; we
        don't try to short-circuit by computing a per-repo install
        URL via ``resolver.resolve`` because:

        * It requires a live API round-trip (and a valid cloud
          token), which fails with a confusing "Could not reach
          Ember Cloud" error when the user simply isn't logged in.
        * The portal page is the same target regardless of repo
          state — if already installed, the user sees their repo
          in the list; if not, they click ``Add repository``.

        Derives the portal host from ``api_url`` by stripping the
        ``api`` token from the leftmost host segment:

        * ``api.ignite-ember.sh`` → ``ignite-ember.sh``
        * ``dev-api.ignite-ember.sh`` → ``dev.ignite-ember.sh``
        * ``staging-api.example.com`` → ``staging.example.com``

        Hosts without an ``api`` token in the leftmost segment are
        passed through unchanged.
        """
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(self._session.settings.api_url)
        host = parsed.netloc
        first, sep, rest = host.partition(".")
        if first == "api":
            # ``api.example.com`` → ``example.com``
            new_host = rest or host
        elif first.endswith("-api"):
            # ``dev-api.example.com`` → ``dev.example.com``
            new_host = f"{first[: -len('-api')]}{sep}{rest}"
        elif first.startswith("api-"):
            # ``api-dev.example.com`` → ``dev.example.com`` (symmetric).
            new_host = f"{first[len('api-') :]}{sep}{rest}"
        else:
            new_host = host
        portal_url = urlunparse((parsed.scheme or "https", new_host, "", "", "", ""))
        return {"install_url": f"{portal_url.rstrip('/')}/repositories"}

    # ── Skills ─────────────────────────────────────────────────────

    def get_skill_details(self) -> list[SkillInfo]:
        """Snapshot of every loaded skill for the panel UI.

        Sends the full ``body`` (which the panel head-clips for the
        expanded view) so toggling expansion doesn't need an extra
        RPC round trip per row.
        """
        from ember_code.core.skills.parser import SkillInfo

        return [
            SkillInfo(
                name=skill.name,
                description=skill.description,
                version=skill.version,
                category=skill.category,
                argument_hint=skill.argument_hint,
                context=skill.context,
                agent=skill.agent or "",
                user_invocable=skill.user_invocable,
                body=skill.body,
                source_dir=str(skill.source_dir) if skill.source_dir else "",
            )
            for skill in self._session.skill_pool.list_skills()
        ]

    # One-liner descriptions for the built-in commands. Used only
    # by ``get_slash_commands`` to give SDK consumers a hint for
    # completion UIs — the source of truth for the actual help
    # text remains ``CommandHandler._HELP_TOPICS``.
    _BUILTIN_DESCRIPTIONS: dict[str, str] = {
        "help": "Show help and available commands",
        "quit": "Exit the session",
        "exit": "Exit the session",
        "clear": "Clear the current conversation",
        "compact": "Compact the conversation context",
        "plan": "Toggle plan mode (read-only sandbox + plan-then-execute workflow)",
        "accept": "Toggle acceptEdits mode (auto-approve file edits)",
        "bypass": "Toggle bypassPermissions mode (auto-approve every tool — no HITL prompts)",
        "output-style": "List or switch the active output style (tone / verbosity)",
        "sessions": "List past sessions",
        "rename": "Rename the current session",
        "fork": "Fork the current session under a new id",
        "model": "Switch the active model",
        "config": "Show current settings",
        "memory": "Inspect or optimise learned memories",
        "knowledge": "Search or add to the knowledge base",
        "codeindex": "Manage the semantic code index",
        "agents": "List and manage agents",
        "skills": "List installed skills",
        "hooks": "List installed hooks",
        "plugins": "Open the plugins panel",
        "plugin": "Install / update / remove a plugin",
        "mcp": "Open the MCP servers panel",
        "login": "Sign in to Ember Cloud",
        "logout": "Sign out of Ember Cloud",
        "whoami": "Show the signed-in user",
        "ctx": "Show context window usage",
        "schedule": "Schedule one-shot or recurring tasks",
        "loop": "Run a prompt in a loop",
        "evals": "Run evaluation suites",
        "bug": "Open a bug report",
        "sync-knowledge": "Sync the knowledge base with git",
    }

    def get_output_styles(self) -> dict:
        """Snapshot of discovered output styles + the active
        one (row 52). FE renders a picker / chip from this.
        ``styles`` is a list of ``{name, description}`` dicts;
        ``active`` is the currently-applied style name (empty
        when none configured)."""
        styles = getattr(self._session, "output_styles", {}) or {}
        active = getattr(self._session, "_active_output_style", "") or ""
        return {
            "active": active,
            "styles": [
                {"name": s.name, "description": s.description}
                for s in sorted(styles.values(), key=lambda s: s.name)
            ],
        }

    def get_latest_plan(self) -> dict:
        """Snapshot of the session's plan store — the agent's
        ``exit_plan_mode`` submissions (row 50).

        Returns ``{latest: str, history: list[str], tasks: list[dict],
        state: "pending"|"approved"|""}``. ``tasks`` is the current
        todo-store snapshot, included so a restored session can
        rebuild the PlanCard's task checklist (the live path seeds
        it from the ``plan_submitted`` push; on restart there's no
        push to listen to, so we read state). ``state`` is inferred
        from the current permission mode: if a plan exists AND the
        session is still in plan mode, the user hasn't acted yet
        (pending); otherwise the user exited plan mode (treat as
        approved). ``dismissed`` (Refine) isn't restorable — we'd
        need to persist the user's reject click, and that decision
        is currently FE-only state.
        Empty strings / list when no plan has been submitted yet.
        """
        store = getattr(self._session, "plan_store", None)
        snap: dict = {"latest": "", "history": []}
        if store is not None:
            snap = store.snapshot()
        todo = getattr(self._session, "todo_store", None)
        snap["tasks"] = todo.snapshot() if todo is not None else []
        snap["state"] = self._infer_plan_state(snap.get("latest", ""))
        return snap

    def _infer_plan_state(self, latest_plan: str) -> str:
        """Best-effort state for a restored PlanCard.

        ``""`` when no plan exists. ``"pending"`` when a plan exists
        and the session is currently in plan mode (the user hasn't
        approved or rejected yet). ``"approved"`` otherwise — the
        only way to leave plan mode after a submission is for the
        user to act on the card (or type ``/plan off``, which has
        the same effect from the user's POV).
        """
        if not latest_plan:
            return ""
        evaluator = getattr(self._session, "permission_evaluator", None)
        if evaluator is None:
            return "approved"
        mode_value = getattr(getattr(evaluator, "mode", None), "value", "") or ""
        return "pending" if mode_value == "plan" else "approved"

    def get_todos(self) -> list[dict]:
        """Snapshot of the session's todo list as written by the
        agent's ``todo_write`` tool. Each entry: ``{content,
        status, activeForm}``. Returns ``[]`` when the agent
        hasn't called the tool yet (no list exists).

        Read-only — clients can't mutate the list directly. The
        agent is the sole writer (CC parity: ``TodoWrite`` is the
        only mutation path)."""
        store = getattr(self._session, "todo_store", None)
        if store is None:
            return []
        return store.snapshot()

    def get_slash_commands(self) -> list[dict]:
        """Snapshot of every available slash command for SDK
        consumers (IDE plugins, completion UIs, the Claude Code
        compatibility surface).

        Three sources, in stable order:

        1. ``builtin`` — shipped commands from
           ``CommandHandler._COMMANDS`` (always available).
        2. ``markdown`` — files discovered under the four
           ``commands/`` roots (user-tier + project-tier ×
           ember + claude namespaces, gated by
           ``cross_tool_support``).
        3. ``skill`` — user-invocable skills from
           ``session.skill_pool``.

        Each entry: ``{name, description, source, argument_hint}``.
        ``name`` is the bare command (no leading slash) — callers
        prepend the slash when displaying. Mirrors Claude Code's
        SDK ``slash_commands`` field so a CC-compatible client can
        consume both backends uniformly.
        """
        from ember_code.backend.command_handler import CommandHandler
        from ember_code.core.utils.markdown_commands import discover_markdown_commands

        out: list[dict] = []

        # Built-ins.
        for cmd_name in CommandHandler._COMMANDS:
            bare = cmd_name.lstrip("/")
            out.append(
                {
                    "name": bare,
                    "description": self._BUILTIN_DESCRIPTIONS.get(bare, ""),
                    "source": "builtin",
                    "argument_hint": "",
                }
            )

        # Markdown-authored commands.
        try:
            read_claude = self._session.settings.rules.cross_tool_support
            md_commands = discover_markdown_commands(
                self._session.project_dir,
                read_claude=read_claude,
            )
        except Exception as exc:
            logger.debug("get_slash_commands: markdown discovery failed: %s", exc)
            md_commands = {}
        for md in md_commands.values():
            out.append(
                {
                    "name": md.name,
                    "description": md.description,
                    "source": "markdown",
                    "argument_hint": md.argument_hint,
                }
            )

        # User-invocable skills.
        try:
            skills = self._session.skill_pool.list_skills()
        except Exception as exc:
            logger.debug("get_slash_commands: skill enumeration failed: %s", exc)
            skills = []
        for skill in skills:
            if not getattr(skill, "user_invocable", True):
                continue
            out.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": "skill",
                    "argument_hint": getattr(skill, "argument_hint", ""),
                }
            )

        return out

    # ── Plugins ────────────────────────────────────────────────────

    def get_plugin_contents(self, name: str) -> dict:
        """Detailed inventory of one installed plugin — what skills,
        agents, hooks, MCP servers, and custom tools it bundles, plus
        a short README excerpt if present. Powers the expandable
        plugin card in the panel.
        """
        loader = self._session.plugin_loader
        plugin = next(
            (p for p in loader.list_plugins() if p.name == name),
            None,
        )
        if plugin is None:
            return {"error": f"Plugin '{name}' not found"}
        return _scan_plugin_dir(plugin.root_path, name=name)

    async def preview_plugin(
        self,
        source: str,
        branch: str | None = None,
        subdir: str | None = None,
    ) -> dict:
        """Same inventory as :meth:`get_plugin_contents`, but for a
        plugin that ISN'T installed yet — performs a shallow clone of
        *source* to a temp dir, scans it, and deletes the clone. Cached
        per (source, branch, subdir) for the lifetime of this backend
        so re-opening the card is instant.
        """
        import re
        import shutil
        import tempfile

        from ember_code.core.plugins.git import GitClient, GitError

        # The marketplace panel sends ``source`` as the formatted
        # display string from ``get_marketplaces`` — bare URL or the
        # subdir form ``"<url> [<subdir>]"``. Split it back so we
        # clone the right URL and descend into the right path.
        m = re.match(r"^(.+?)\s+\[(.+?)\]\s*$", source.strip())
        if m and not subdir:
            clone_url = m.group(1).strip()
            subdir = m.group(2).strip()
        else:
            clone_url = source.strip()

        key = (clone_url, branch or "", subdir or "")
        # Lazy-initialize the preview cache on first access; pin the
        # variable's type to a concrete dict so the indexing operations
        # below typecheck cleanly (``getattr(..., None)`` would otherwise
        # widen ``cache`` to ``Any | None``).
        preview_cache: dict[tuple[str, str, str], dict] = (
            getattr(self, "_preview_cache", None) or {}
        )
        if not hasattr(self, "_preview_cache"):
            self._preview_cache = preview_cache
        if key in preview_cache:
            return preview_cache[key]

        git = GitClient()
        if not git.is_available():
            return {"error": "git is not installed on this machine."}

        tmp = Path(tempfile.mkdtemp(prefix="ember-preview-"))
        try:
            await asyncio.to_thread(git.clone, clone_url, tmp, ref=branch or None, shallow=True)
            scan_root = tmp / subdir if subdir else tmp
            if not scan_root.is_dir():
                return {
                    "error": (
                        f"Cloned repo has no '{subdir}' subdirectory — "
                        "the marketplace entry may be stale."
                    )
                }
            result = _scan_plugin_dir(scan_root, name=source)
            # Don't leak the throwaway temp path — surface the source
            # the user knows about instead. Echo the subdir form so
            # the FE display matches the catalog entry.
            result["root_path"] = f"{clone_url} [{subdir}]" if subdir else clone_url
            preview_cache[key] = result
            return result
        except GitError as exc:
            return {"error": f"git clone failed: {exc}"}
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def get_plugin_details(self) -> list[PluginInfo]:
        """Snapshot of every discovered plugin for the panel UI.

        Combines :class:`PluginLoader` discovery state with the
        persisted enable/disable list and pinned-SHA map so the panel
        can render counts, version, source root, and toggle status
        without any further RPC chatter. Returns typed
        :class:`PluginInfo` models — the wire format is defined in
        :mod:`core.plugins.models` so backend and frontend share the
        same shape.
        """
        from ember_code.core.plugins.models import PluginInfo

        loader = self._session.plugin_loader
        state = self._session.plugin_state
        disabled = set(state.disabled)
        return [
            PluginInfo(
                name=p.name,
                version=p.manifest.version or "",
                description=p.manifest.description or "",
                source_root=p.source.root,
                path=str(p.root_path),
                # Managed plugins ignore the persisted disable
                # list (see ``Session._disabled_plugins``); reflect
                # that here so the panel shows them enabled and
                # locks the toggle.
                enabled=p.is_managed or p.name not in disabled,
                has_skills=p.has_skills,
                has_agents=p.has_agents,
                has_hooks=p.has_hooks,
                has_mcp=p.has_mcp,
                has_tools=p.has_tools,
                has_lsp=p.has_lsp,
                has_monitors=p.has_monitors,
                managed=p.is_managed,
                pin=state.pins.get(p.name, ""),
            )
            for p in loader.list_plugins()
        ]

    def set_plugin_enabled(self, name: str, enabled: bool) -> msg.Info:
        """Toggle a plugin's enabled flag, persist, and hot-reload.

        Re-applying the plugin set after the flip means an
        ``enable`` activates the plugin's skills/agents/hooks
        immediately, and a ``disable`` makes them disappear from
        the live session — no restart needed.
        """
        from ember_code.core.plugins.state import save_state

        loader = self._session.plugin_loader
        state = self._session.plugin_state
        plugin = loader.get(name)
        if plugin is None:
            return msg.Info(text=f"No plugin named '{name}'.")
        if plugin.is_managed and not enabled:
            # Managed plugins are sysadmin-enforced; refuse the
            # disable attempt explicitly so the user knows why
            # rather than seeing a silent no-op.
            return msg.Info(
                text=(
                    f"Plugin '{name}' is managed (sysadmin-enforced) and cannot be "
                    "disabled. Remove it from the managed plugins directory to "
                    "uninstall."
                )
            )

        disabled_set = set(state.disabled)
        if enabled:
            disabled_set.discard(name)
        else:
            disabled_set.add(name)
        state.disabled = sorted(disabled_set)
        save_state(state, data_dir=self._session.settings.storage.data_dir)

        # Hot-reload — ``reload_plugins`` re-reads the disabled set
        # from disk and rebuilds skills/agents/hooks/MCP-configs
        # accordingly. The main team rebuilds at the end so tools
        # in the disabled plugin disappear from the agent surface,
        # and any bundled MCP servers are disconnected in the
        # background (auto-symmetric with the enable path).
        self._session.reload_plugins()
        if enabled:
            tail = (
                "Its skills/agents/hooks/tools are active; any bundled "
                "MCP servers are starting in the background."
            )
        else:
            tail = (
                "Its skills/agents/hooks/tools are no longer active; "
                "any bundled MCP servers are being disconnected."
            )
        verb = "enabled" if enabled else "disabled"
        return msg.Info(text=f"Plugin '{name}' {verb}. {tail}")

    def install_plugin(self, ref: str, install_ref: str | None = None) -> msg.Info:
        """Install a plugin by git URL or ``@<marketplace>/<plugin>`` ref.

        ``install_ref`` (the ``--ref`` flag in the slash command — a
        branch / tag / SHA) is forwarded to the installer. Marketplace
        refs may carry a default ``branch`` in the catalog; honored
        only when ``install_ref`` is omitted so explicit user choice
        wins.
        """
        from ember_code.core.plugins.git import GitError
        from ember_code.core.plugins.installer import (
            PluginError,
            PluginInstaller,
        )
        from ember_code.core.plugins.marketplaces import resolve_install_ref

        data_dir = self._session.settings.storage.data_dir
        installer = PluginInstaller(data_dir=data_dir)
        if not installer.is_git_available():
            return msg.Info(text="git not found on PATH. Install git and retry.")

        url = ref
        subdir: str | None = None
        if ref.startswith("@"):
            resolved = resolve_install_ref(ref, data_dir=data_dir)
            if resolved is None:
                return msg.Info(
                    text=f"Could not resolve '{ref}'. Run "
                    "/plugin marketplace list to see registered "
                    "marketplaces, or use a git URL."
                )
            resolved_source, _mkt_entry = resolved
            url = resolved_source.url
            subdir = resolved_source.subdir
            if install_ref is None:
                install_ref = resolved_source.ref

        try:
            manifest = installer.install(url, ref=install_ref, subdir=subdir)
        except GitError as e:
            return msg.Info(text=f"git error: {e}")
        except PluginError as e:
            return msg.Info(text=str(e))

        version = f" v{manifest.version}" if manifest.version else ""
        # Hot-reload — pull the new plugin's skills / agents / hooks /
        # MCP configs / custom tools into the running session so the
        # user can use them immediately. ``reload_plugins`` rebuilds
        # the main team at the end, and auto-connects any new MCP
        # servers in the background (the existing approval prompt
        # gates first-use, so consent is still required).
        counts = self._session.reload_plugins()
        return msg.Info(
            text=(
                f"Installed plugin '{manifest.name}'{version}. "
                f"Active now — {counts['skills']} skill(s), "
                f"{counts['agents']} agent(s), {counts['hooks']} hook(s). "
                f"Any bundled MCP servers are starting in the background."
            )
        )

    def update_plugin(self, name: str, install_ref: str | None = None) -> msg.Info:
        """Fetch + reset to ``install_ref`` (default: origin's HEAD)."""
        from ember_code.core.plugins.git import GitError
        from ember_code.core.plugins.installer import (
            PluginError,
            PluginInstaller,
        )

        installer = PluginInstaller(
            data_dir=self._session.settings.storage.data_dir,
        )
        if not installer.is_git_available():
            return msg.Info(text="git not found on PATH.")
        try:
            new_sha = installer.update(name, ref=install_ref)
        except GitError as e:
            return msg.Info(text=f"git error: {e}")
        except PluginError as e:
            return msg.Info(text=str(e))
        # Hot-reload so the updated plugin's contents replace the
        # old ones in the live session.
        self._session.reload_plugins()
        return msg.Info(text=f"Updated '{name}' to {new_sha[:12]}. Active now.")

    def remove_plugin(self, name: str) -> msg.Info:
        """Delete the plugin directory and clear its pin."""
        from ember_code.core.plugins.installer import (
            PluginError,
            PluginInstaller,
        )

        installer = PluginInstaller(
            data_dir=self._session.settings.storage.data_dir,
        )
        try:
            installer.remove(name)
        except PluginError as e:
            return msg.Info(text=str(e))
        # Hot-reload so the removed plugin's skills/agents/hooks
        # disappear from the live session immediately. Any
        # bundled MCP servers are also disconnected in the
        # background — symmetric with the enable/install path.
        self._session.reload_plugins()
        return msg.Info(
            text=(
                f"Removed '{name}'. Skills/agents/hooks/tools no "
                "longer active; bundled MCP servers are being "
                "disconnected."
            )
        )

    def get_marketplaces(self) -> list[MarketplaceInfo]:
        """Snapshot of every registered marketplace for the panel.

        Returns typed :class:`MarketplaceInfo` models (nesting
        :class:`MarketplacePluginInfo` per catalog entry). Same wire
        contract as ``get_plugin_details`` — source-of-truth shape
        lives in :mod:`core.plugins.models`.

        The catalog's raw ``source`` field can be a string OR a dict
        (see :class:`ResolvedSource` for the three official shapes).
        We collapse it to a single human-readable string here so the
        panel's :class:`MarketplacePluginInfo` (which types ``source``
        as ``str`` for display simplicity) doesn't blow up on the
        dict-shaped entries Anthropic's marketplace ships for
        ~75% of its plugins.
        """
        from ember_code.core.plugins.marketplaces import load_registry
        from ember_code.core.plugins.models import (
            MarketplaceInfo,
            MarketplacePluginInfo,
        )

        registry = load_registry(
            data_dir=self._session.settings.storage.data_dir,
        )
        out: list[MarketplaceInfo] = []
        for m in registry.marketplaces:
            plugins: list[MarketplacePluginInfo] = []
            for p in m.cached.plugins if m.cached else []:
                resolved = p.resolved_source(m.url)
                # Display string: ``url`` for bare-URL installs,
                # ``url [subdir/path]`` for subdir/relative shapes,
                # or repr(raw) as a last-resort fallback when the
                # entry is so malformed we can't resolve it at all.
                if resolved is None:
                    source_display = str(p.source) if p.source else ""
                elif resolved.subdir:
                    source_display = f"{resolved.url} [{resolved.subdir}]"
                else:
                    source_display = resolved.url
                plugins.append(
                    MarketplacePluginInfo(
                        name=p.name,
                        source=source_display,
                        description=p.description or "",
                        version=p.version or "",
                        branch=p.branch or "",
                    )
                )
            out.append(
                MarketplaceInfo(
                    name=m.name,
                    url=m.url,
                    last_fetched=m.last_fetched or "",
                    plugins=plugins,
                )
            )
        return out

    def add_marketplace(self, url: str) -> msg.Info:
        from ember_code.core.plugins.git import GitError
        from ember_code.core.plugins.marketplaces import (
            add_marketplace as _add,
        )

        try:
            entry = _add(url, data_dir=self._session.settings.storage.data_dir)
        except GitError as e:
            return msg.Info(text=f"git error: {e}")
        except Exception as e:  # noqa: BLE001 — surface verbatim
            return msg.Info(text=f"Failed to add marketplace: {e}")
        count = len(entry.cached.plugins) if entry.cached else 0
        return msg.Info(text=f"Added '{entry.name}' ({count} plugin(s) catalogued).")

    def remove_marketplace(self, name: str) -> msg.Info:
        from ember_code.core.plugins.marketplaces import (
            remove_marketplace as _remove,
        )

        if not _remove(name, data_dir=self._session.settings.storage.data_dir):
            return msg.Info(text=f"No marketplace named '{name}'.")
        return msg.Info(text=f"Unregistered '{name}'. Installed plugins from it remain.")

    def refresh_marketplaces(self, name: str | None = None) -> msg.Info:
        """Re-fetch one marketplace or all. Errors per-marketplace are
        collected and reported together so a single bad URL doesn't
        abort the whole refresh."""
        from ember_code.core.plugins.marketplaces import (
            load_registry,
        )
        from ember_code.core.plugins.marketplaces import (
            refresh_marketplace as _refresh,
        )

        data_dir = self._session.settings.storage.data_dir
        if name:
            try:
                entry = _refresh(name, data_dir=data_dir)
            except Exception as e:  # noqa: BLE001
                return msg.Info(text=f"Refresh failed for '{name}': {e}")
            if entry is None:
                return msg.Info(text=f"No marketplace named '{name}'.")
            count = len(entry.cached.plugins) if entry.cached else 0
            return msg.Info(text=f"Refreshed '{entry.name}' ({count} plugins).")

        registry = load_registry(data_dir=data_dir)
        ok: list[str] = []
        failed: list[str] = []
        for m in registry.marketplaces:
            try:
                _refresh(m.name, data_dir=data_dir)
                ok.append(m.name)
            except Exception as e:  # noqa: BLE001
                failed.append(f"{m.name} ({e})")
        if not ok and not failed:
            return msg.Info(text="No marketplaces to refresh.")
        line = f"Refreshed {len(ok)} ok"
        if failed:
            line += f"; {len(failed)} failed: {', '.join(failed)}"
        return msg.Info(text=line)

    async def fire_session_start_hook(self) -> None:
        """Fire the SessionStart hook."""
        from ember_code.core.hooks.events import HookEvent

        with contextlib.suppress(Exception):
            await self._session.hook_executor.execute(
                event=HookEvent.SESSION_START.value,
                payload={"session_id": self._session.session_id},
            )

    async def auto_sync_knowledge(self) -> str | None:
        """Auto-sync knowledge file on startup. Returns status message or None."""
        if self._session.knowledge is None:
            return None
        try:
            result = await self._session.knowledge_mgr.sync_from_file()
            if result:
                return f"Knowledge synced: {result}"
        except Exception as exc:
            logger.debug("knowledge sync_from_file failed (%s)", exc)
        return None

    async def execute_scheduled_task(self, description: str) -> str:
        """Execute a scheduled task via the agent. Returns result text."""
        from ember_code.core.utils.response import extract_response_text

        team = self._session.main_team
        response = await team.arun(description, stream=False)
        return extract_response_text(response)

    async def cancel_scheduled_task(self, task_id: str) -> msg.Info:
        """Cancel a scheduled task."""
        from ember_code.core.scheduler.models import TaskStatus
        from ember_code.core.scheduler.store import TaskStore

        store = TaskStore(project_dir=self._session.project_dir)
        await store.update_status(task_id, TaskStatus.cancelled)
        return msg.Info(text=f"Cancelled task {task_id}")

    async def get_scheduled_tasks(self, include_done: bool = True) -> list:
        """Get all scheduled tasks."""
        from ember_code.core.scheduler.store import TaskStore

        store = TaskStore(project_dir=self._session.project_dir)
        return await store.get_all(include_done=include_done)

    def start_scheduler(
        self,
        on_task_started=None,
        on_task_completed=None,
    ) -> Any:
        """Start the background scheduler. Idempotent: caches the
        runner on the pool so reconnects (and the Web/TUI both calling
        this) don't spawn duplicate pollers competing for the same
        task store. Returns the runner for stop()."""
        from ember_code.core.hooks.events import HookEvent
        from ember_code.core.scheduler.runner import SchedulerRunner
        from ember_code.core.scheduler.store import TaskStore

        existing = getattr(self._session.pool, "_scheduler_runner", None)
        if existing is not None and getattr(existing, "is_running", False):
            return existing

        sched_cfg = self._settings.scheduler
        store = TaskStore(project_dir=self._session.project_dir)

        # Compose the caller's task callbacks with hook-event firings
        # so plugins observe scheduler lifecycle without each call
        # site re-implementing it. TaskCreated fires when the
        # scheduler spawns a task (the moment the runtime first
        # touches it); TaskCompleted fires regardless of outcome
        # with the success/failure flag in ``status``.
        hook_executor = self._session.hook_executor
        session_id = self._session.session_id

        def _on_started(task_id: str, description: str) -> None:
            if on_task_started:
                on_task_started(task_id, description)
            asyncio.create_task(
                hook_executor.execute(
                    event=HookEvent.TASK_CREATED.value,
                    payload={
                        "session_id": session_id,
                        "task_id": task_id,
                        "description": description,
                    },
                )
            )

        def _on_completed(task_id: str, description: str, success: bool) -> None:
            if on_task_completed:
                on_task_completed(task_id, description, success)
            asyncio.create_task(
                hook_executor.execute(
                    event=HookEvent.TASK_COMPLETED.value,
                    payload={
                        "session_id": session_id,
                        "task_id": task_id,
                        "description": description,
                        "status": "completed" if success else "error",
                    },
                )
            )

        runner = SchedulerRunner(
            store=store,
            execute_fn=self.execute_scheduled_task,
            on_task_started=_on_started,
            on_task_completed=_on_completed,
            poll_interval=sched_cfg.poll_interval,
            task_timeout=sched_cfg.task_timeout,
            max_concurrent=sched_cfg.max_concurrent,
        )
        runner.start()
        self._session.pool._scheduler_runner = runner
        return runner

    def toggle_verbose(self) -> bool:
        """Toggle verbose mode. Returns new state."""
        self._settings.display.show_routing = not self._settings.display.show_routing
        return self._settings.display.show_routing
