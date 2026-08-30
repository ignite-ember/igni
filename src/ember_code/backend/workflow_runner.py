"""Workflow runner — execute ``.claude/workflows/*.mjs`` in a Node subprocess.

Two surfaces:

- :class:`WorkflowDiscovery` — scans ``.claude/workflows/`` and
  reads each file's ``meta`` export via the runtime's
  ``--discovery`` short-circuit. Cached by mtime.
- :class:`WorkflowRunner` — spawns the runtime subprocess, drains
  the structured event stream, bridges ``agent()`` calls back to
  the BE's :func:`session.main_team.arun`, persists every event
  to the session's append-only event log, and pushes it on the
  ``workflow_event`` push channel for the FE to render.

Architecture (3 layers):

```
.claude/workflows/<name>.mjs   (existing, unchanged)
        │  imported via
        ▼
workflow_runtime.mjs           (the Node bridge — emits events on
                               stdout, reads agent responses on
                               stdin)
        │
        ▼
WorkflowRunner                (this module — orchestrates the
                               subprocess, persists + pushes
                               events, bridges agent() calls)
```

Why a subprocess? ``vm`` in Python can't safely run a workflow
that needs top-level ``await``, the CC-style ``return <value>`` at
the top of the file, or an async-iife wrapper without bringing in
the full Node module system. The Node bridge is small, has no
runtime deps, and ``vm.createContext`` already gives us
fresh-per-run isolation inside the bridge itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import ValidationError

from ember_code.backend.schemas_workflows import (
    WorkflowEvent,
    WorkflowMeta,
    WorkflowMetaEnvelope,
)

if TYPE_CHECKING:
    from ember_code.backend.push_bridge import PushNotificationBridge
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)

# Path to the Node bridge runtime (ships next to this module).
RUNTIME_PATH = Path(__file__).parent / "workflow_runtime.mjs"

# Where we look for workflow files. Two layers:
#
# 1. **Team layer** (committed to the repo): ``<project>/.claude/workflows``
#    — the CC convention. Workflows that ship with the project and
#    are version-controlled alongside the code.
#
# 2. **Per-user layer** (uncommitted): ``<project>/.ember/workflows`` —
#    personal overrides or additions that don't belong in the repo.
#    Same convention as ``.ember/skills/`` / ``.ember/agents/`` /
#    ``.ember/hooks/`` already in use.
#
# On name collisions, the per-user layer wins (typical override
# semantics: your local copy is the source of truth for you).
DEFAULT_WORKFLOW_DIR_TEAM = Path(".claude") / "workflows"
DEFAULT_WORKFLOW_DIR_USER = Path(".ember") / "workflows"

# Per-agent timeout. 10 minutes matches ClaudeCode's default for
# one-shot agent calls. Workflow callers can override per call via
# the ``timeoutSeconds`` option (forwarded by the Node bridge).
DEFAULT_AGENT_TIMEOUT_SECONDS = 600

# Subprocess grace period for cancel() before SIGKILL.
CANCEL_GRACE_SECONDS = 5.0


# ── Discovery ──────────────────────────────────────────────────────


class WorkflowDiscovery:
    """Scan two workflow directories and read each file's ``meta`` export.

    Workflows live in two layers:

    1. **Team layer** — ``<project>/.claude/workflows``. The CC
       convention; workflows that ship with the project and are
       version-controlled.
    2. **Per-user layer** — ``<project>/.ember/workflows``. The
       Ember override directory; personal additions or
       replacements that don't belong in the repo.

    On name collisions the per-user layer wins (you can
    override a team workflow by placing a file with the same
    stem in ``.ember/workflows/``). Discovery spawns the
    runtime once per file with ``--discovery`` (evaluates the
    file in a fresh :class:`vm.Script` context and emits a
    single ``workflow_meta`` event). Cached by mtime per file.
    """

    def __init__(self, *, project_dir: Path, group_dir: Path | None = None):
        self._project_dir = Path(project_dir)
        self._team_dir = self._project_dir / DEFAULT_WORKFLOW_DIR_TEAM
        self._user_dir = self._project_dir / DEFAULT_WORKFLOW_DIR_USER
        # The org's, from the group policy cache. Listed last so a
        # workflow the group ships wins the name.
        self._group_dir = Path(group_dir) if group_dir else None
        self._cache: dict[Path, tuple[float, WorkflowMetaEnvelope]] = {}

    @property
    def workflows_dir(self) -> Path:
        """The team-layer directory (``.claude/workflows``).

        Exists for backwards compat with any caller that read the
        single ``workflows_dir`` attribute — most should now
        use :meth:`list_workflows` which walks both layers.
        """
        return self._team_dir

    def _iter_paths(self) -> list[Path]:
        """All ``*.mjs`` files across both layers, with the
        per-user layer listed LAST so it wins name collisions in
        the shadow pass (later entries overwrite earlier ones).
        """
        out: list[Path] = []
        for directory in (self._team_dir, self._user_dir, self._group_dir):
            if directory is not None and directory.is_dir():
                out.extend(sorted(directory.glob("*.mjs")))
        return out

    def _shadow(self, paths: list[Path]) -> dict[str, Path]:
        """Reduce a list of paths to ``{name: path}``; later
        entries shadow earlier ones.

        ``name`` is the file stem (``refactor-to-standards`` for
        ``refactor-to-standards.mjs``). Callers pass paths in
        team-first / user-last order so the user layer wins.
        """
        out: dict[str, Path] = {}
        for p in paths:
            out[p.stem] = p
        return out

    async def list_workflows(self) -> list[WorkflowMetaEnvelope]:
        """Return every workflow's meta, user-layer shadows team-layer.

        Sorted by workflow name (the user-visible label) for a
        stable ``?demo=workflow`` rendering and a predictable
        CLI completion order.
        """
        shadowed = self._shadow(self._iter_paths())
        results: list[WorkflowMetaEnvelope] = []
        for name in sorted(shadowed):
            env = await self._meta_for(shadowed[name])
            if env is not None:
                results.append(env)
        return results

    async def resolve(self, name: str) -> WorkflowMetaEnvelope | None:
        """Return the meta for a single named workflow.

        User-layer wins on conflict — same shadow semantics as
        :meth:`list_workflows`. The ``_iter_paths`` ordering has
        team first and user second, so a plain ``for`` over it
        would return the team version. We reverse-iterate to
        find the user-layer match first, falling back to team
        if the user layer doesn't define this name.
        """
        for path in reversed(self._iter_paths()):
            if path.stem == name:
                return await self._meta_for(path)
        return None

    async def _meta_for(self, path: Path) -> WorkflowMetaEnvelope | None:
        mtime = path.stat().st_mtime
        cached = self._cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        env = await self._discover_one(path)
        if env is not None:
            self._cache[path] = (mtime, env)
        return env

    async def _discover_one(self, path: Path) -> WorkflowMetaEnvelope | None:
        if not RUNTIME_PATH.exists():
            logger.warning("workflow runtime missing at %s", RUNTIME_PATH)
            return None
        proc = await asyncio.create_subprocess_exec(
            "node",
            str(RUNTIME_PATH),
            str(path),
            "--discovery",
            "--workflow-run-id",
            "discovery",
            cwd=str(self._project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        if proc.returncode != 0:
            logger.debug(
                "discovery failed for %s (exit %s): %s",
                path,
                proc.returncode,
                stderr_b.decode("utf-8", errors="replace")[:400],
            )
            return None
        for line in stdout_b.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = WorkflowEvent.model_validate_json(line)
            except ValidationError:
                continue
            if event.type == "workflow_meta":
                try:
                    return WorkflowMetaEnvelope(
                        meta=WorkflowMeta.model_validate(event.payload["meta"]),
                        path=str(path.relative_to(self._project_dir)),
                    )
                except ValidationError as exc:
                    logger.debug("meta validation failed for %s: %s", path, exc)
                    return None
        return None


# ── Runner ─────────────────────────────────────────────────────────


@dataclass
class _RunState:
    """Per-run mutable state held while a subprocess is alive."""

    proc: asyncio.subprocess.Process
    workflow_run_id: str
    name: str
    started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    final_status: str | None = None
    final_payload: dict[str, Any] | None = None
    stdin_closed: bool = False
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)


class WorkflowRunner:
    """Execute a workflow, broadcast progress, bridge agent() calls.

    The runner is constructed once per ``BackendApp`` and held on
    ``backend.workflow_runner`` so the RPC handlers can reach it.
    A single runner multiplexes concurrent workflow runs (one
    subprocess + one ``_RunState`` per run) — typical usage is a
    handful of concurrent workflows at most.
    """

    def __init__(
        self,
        *,
        project_dir: Path,
        push: PushNotificationBridge,
        group_dir: Path | None = None,
    ):
        self._project_dir = Path(project_dir)
        self._push = push
        self._discovery = WorkflowDiscovery(project_dir=self._project_dir, group_dir=group_dir)
        self._runs: dict[str, _RunState] = {}

    @property
    def discovery(self) -> WorkflowDiscovery:
        return self._discovery

    async def list_workflows(self) -> list[WorkflowMetaEnvelope]:
        return await self._discovery.list_workflows()

    async def run(
        self,
        *,
        name: str,
        args: dict[str, Any],
        session: Session,
    ) -> str:
        """Start a workflow run. Returns the ``workflow_run_id`` immediately.

        The run is async — the actual subprocess is spawned, event
        drain + agent bridge are kicked off as background tasks,
        and we return the id. The FE optimistically creates a
        ``ChatItem`` keyed by this id and folds subsequent
        ``workflow_event`` pushes into it.
        """
        meta = await self._discovery.resolve(name)
        if meta is None:
            raise FileNotFoundError(
                f"workflow '{name}' not found in {self._discovery.workflows_dir}"
            )
        if not RUNTIME_PATH.exists():
            raise RuntimeError(
                f"workflow runtime missing at {RUNTIME_PATH}; "
                "the .mjs file is part of the repo, this is a build issue"
            )

        workflow_run_id = f"wf_{uuid4().hex[:12]}"
        workflow_path = self._project_dir / meta.path

        proc = await asyncio.create_subprocess_exec(
            "node",
            str(RUNTIME_PATH),
            str(workflow_path),
            "--workflow-run-id",
            workflow_run_id,
            "--session-id",
            session.id,
            "--name",
            meta.meta.name,
            "--args",
            json.dumps(args),
            cwd=str(self._project_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        state = _RunState(proc=proc, workflow_run_id=workflow_run_id, name=name)
        self._runs[workflow_run_id] = state

        # Kick off the three concurrent tasks. We don't await them
        # here — they run until the subprocess exits (drain) or
        # forever (agent bridge). The runner owns the tasks so
        # cancel() can stop them.
        state.tasks = [
            asyncio.create_task(
                self._drain_stdout(state, session),
                name=f"workflow-drain-{workflow_run_id}",
            ),
            asyncio.create_task(
                self._bridge_agents(state, session),
                name=f"workflow-bridge-{workflow_run_id}",
            ),
            asyncio.create_task(
                self._await_completion(state, session),
                name=f"workflow-await-{workflow_run_id}",
            ),
        ]
        return workflow_run_id

    async def cancel(self, workflow_run_id: str) -> bool:
        """Best-effort cancel: notify the subprocess, SIGTERM after grace."""
        state = self._runs.get(workflow_run_id)
        if state is None or state.proc.returncode is not None:
            return False
        # Notify the runtime so in-flight agent() calls reject promptly.
        try:
            if state.proc.stdin is not None and not state.proc.stdin.is_closing():
                state.proc.stdin.write(b'{"type":"cancel"}\n')
                await state.proc.stdin.drain()
        except Exception as exc:
            logger.debug("cancel: stdin notify failed for %s (%s)", workflow_run_id, exc)
        # Wait briefly for graceful exit; escalate to SIGTERM.
        try:
            await asyncio.wait_for(state.proc.wait(), timeout=CANCEL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            state.proc.terminate()
            try:
                await asyncio.wait_for(state.proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                state.proc.kill()
                await state.proc.wait()
        return True

    async def resume(self, workflow_run_id: str) -> str | None:
        """Resume a previously-cancelled workflow from where it stopped.

        The CC workflow contract is one-shot — scripts re-run from
        the top on a fresh call. A true resume requires the
        workflow script to support resumability (typically via
        ``args.startFromPhase`` or a saved checkpoint), which the
        refactor-to-standards.mjs does not. This method exists as
        the seam for that future work: it returns ``None`` for now
        so the FE can show a "Resume not supported" hint instead
        of crashing on a missing RPC.

        For now, callers should fall back to :meth:`run` (which
        always re-runs from the top — the user re-runs the whole
        workflow, not just the failed phase).
        """
        # TODO(workflow): wire a ``--start-phase`` CLI flag
        # through ``workflow_runtime.mjs`` + the script's own
        # ``args`` parsing, then update this method to spawn a
        # subprocess with the right start arg.
        return None

    # ── Internal tasks ────────────────────────────────────────────

    async def _drain_stdout(self, state: _RunState, session: Session) -> None:
        """Read stdout line-by-line; persist + push each event.

        When the line is an ``agent_request``, fan out a coroutine
        that calls ``session.main_team.arun`` and writes the
        response back to the subprocess's stdin. The drain keeps
        reading other events in parallel — the subprocess is
        blocked on the pending agent call's response, so no new
        events arrive until we reply.
        """
        if state.proc.stdout is None:
            return
        try:
            while True:
                line = await state.proc.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    event = WorkflowEvent.model_validate_json(raw)
                except ValidationError as exc:
                    logger.debug("workflow event validation failed: %s — %s", exc, raw[:200])
                    continue
                if event.run_id != state.workflow_run_id:
                    event = event.model_copy(update={"run_id": state.workflow_run_id})

                # agent_request is special — the runtime is blocked
                # awaiting our reply. Resolve it concurrently so the
                # drain can keep reading.
                if event.type == "agent_request":
                    asyncio.create_task(
                        self._handle_agent_request(state, session, event),
                        name=f"workflow-agent-{state.workflow_run_id}-{event.payload.get('id', '?')}",
                    )
                    continue

                payload = {
                    "workflow_run_id": state.workflow_run_id,
                    "name": state.name,
                    "ts": event.ts,
                    "seq": event.seq,
                    "type": event.type,
                    "payload": event.payload,
                }
                try:
                    await session.append_event("workflow_event", payload)
                except Exception:
                    logger.exception("workflow event persist failed for %s", state.workflow_run_id)
                self._push._schedule_push("workflow_event", payload)
                if event.type == "workflow_completed":
                    state.final_status = "completed"
                    state.final_payload = event.payload
                elif event.type == "workflow_failed":
                    state.final_status = "failed"
                    state.final_payload = event.payload
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("workflow stdout drain failed for %s", state.workflow_run_id)

    async def _handle_agent_request(
        self,
        state: _RunState,
        session: Session,
        event: WorkflowEvent,
    ) -> None:
        """Resolve one ``agent_request``: call ``team.arun`` and write the reply."""
        req_id = str(event.payload.get("id", ""))
        prompt = event.payload.get("prompt", "")
        timeout = int(event.payload.get("timeout_seconds", DEFAULT_AGENT_TIMEOUT_SECONDS))
        ok = False
        result: Any = None
        error: str | None = None
        try:
            team = getattr(session, "main_team", None)
            if team is None:
                error = "session.main_team not available"
            else:
                response = await asyncio.wait_for(
                    team.arun(prompt, stream=False),  # type: ignore[arg-type]
                    timeout=timeout,
                )
                # The Team response is a ``TeamRunOutput`` whose
                # ``content`` (or ``messages[-1].content``) holds the
                # text. Workflow scripts expect a JSON-parseable
                # result when they pass a ``schema``; for plain
                # prompts the text is fine.
                result = _extract_agent_result(response)
                ok = True
        except asyncio.TimeoutError:
            error = f"agent call timed out after {timeout}s"
        except Exception as exc:
            error = f"agent call failed: {exc}"
            logger.exception("agent bridge failed for %s", state.workflow_run_id)

        if state.proc.stdin is None or state.proc.stdin.is_closing():
            logger.warning(
                "agent response for %s dropped: subprocess stdin closed",
                state.workflow_run_id,
            )
            return
        try:
            line = (
                json.dumps(
                    {
                        "type": "agent_response",
                        "id": req_id,
                        "ok": ok,
                        "result": result,
                        "error": error,
                    }
                )
                + "\n"
            )
            state.proc.stdin.write(line.encode("utf-8"))
            await state.proc.stdin.drain()
        except Exception as exc:
            logger.warning("agent response write failed for %s: %s", state.workflow_run_id, exc)

    async def _bridge_agents(self, state: _RunState, session: Session) -> None:
        """Sentinel task — the actual agent bridging happens in
        :meth:`_handle_agent_request`, which the drain spawns per
        ``agent_request`` event. This coroutine just keeps the
        task slot alive until the run ends so the runner can
        cancel it cleanly on shutdown.
        """
        try:
            while state.proc.returncode is None:
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise

    async def _await_completion(self, state: _RunState, session: Session) -> None:
        """Wait for the subprocess to exit; synthesize failure event if needed."""
        try:
            rc = await state.proc.wait()
        except asyncio.CancelledError:
            raise
        # Give the drain a moment to flush any final lines.
        for task in state.tasks:
            if task.get_name().startswith("workflow-drain-"):
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2.0)
        # If the subprocess exited without a terminal event, the
        # workflow crashed (uncaught JS exception, OOM, etc.).
        # Emit a synthetic ``workflow_failed`` so the FE card
        # reaches a terminal state.
        if state.final_status is None:
            status = "failed" if rc != 0 else "completed"
            payload = {
                "workflow_run_id": state.workflow_run_id,
                "name": state.name,
                "ts": int(time.time() * 1000),
                "seq": -1,
                "type": "workflow_failed" if rc != 0 else "workflow_completed",
                "payload": {
                    "status": status,
                    "exit_code": rc,
                    "synthetic": True,
                },
            }
            try:
                await session.append_event("workflow_event", payload)
            except Exception:
                logger.exception(
                    "failed to append synthetic workflow_failed for %s",
                    state.workflow_run_id,
                )
            self._push._schedule_push("workflow_event", payload)
        # Cancel any leftover tasks and unregister.
        for task in state.tasks:
            if not task.done():
                task.cancel()
        self._runs.pop(state.workflow_run_id, None)

    async def shutdown(self) -> None:
        """Cancel every active run. Called from ``BackendApp.run`` teardown."""
        for run_id in list(self._runs.keys()):
            try:
                await self.cancel(run_id)
            except Exception:
                logger.exception("error cancelling workflow %s", run_id)


# ── Helpers ────────────────────────────────────────────────────────


def _extract_agent_result(response: Any) -> Any:
    """Pull the agent's text content out of a ``TeamRunOutput``.

    The Agent/Team response shape varies by Agno version, so we
    walk the most common fields and try a couple of fallbacks
    before giving up. Returns the raw content (string for plain
    prompts, parsed JSON for schema-validated calls) so the
    Node-side caller can use it directly.
    """
    if response is None:
        return None
    if isinstance(response, str):
        return response
    if isinstance(response, (dict, list, int, float, bool)):
        return response
    # Pydantic-like object — try common fields.
    for attr in ("content", "output", "text", "response"):
        if hasattr(response, attr):
            value = getattr(response, attr)
            if value is not None:
                return value
    # Last resort: stringify the messages list.
    messages = getattr(response, "messages", None)
    if messages:
        last = messages[-1]
        if hasattr(last, "content"):
            return last.content
    return str(response)
