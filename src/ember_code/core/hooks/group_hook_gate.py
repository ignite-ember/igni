"""Whether a hook the organisation's group policy ships may run on this machine.

A group's hooks arrive in the signed group policy pack (and in the plugins the
pack installs) and run ``bash -c`` as the developer, outside the agent's
permission system — ``--strict`` denies the agent's shell and does not touch
them. The person a developer needs protecting from here is whoever administers
the organisation, who controls the server and writes the pack, so nothing the
server does can be the control. This is the control, and it is the
developer's: ``hooks.group`` in their own ``~/.igni/config.yaml`` or on their
command line, which neither a project, the group policy nor the managed file
can set (``DeveloperOnlyTier`` in ``core/config/merge_plan.py``).

* ``run`` — the group's hooks run (the default, and the behaviour before this
  existed).
* ``ask`` — before a group hook first runs, the person sees its command and
  where it came from, and it runs only on a yes. The answer is kept per
  version of the hook (a digest of everything it does), so a hook the group
  changes is asked about again. With nobody to ask — a headless run, or an
  event that fires outside a run — an unanswered hook is refused.
  ``--strict`` means at least this.
* ``refuse`` — no group hook runs.

Enforced in :class:`~ember_code.core.hooks.handlers.registry.HookHandlerRegistry`
— the one place every hook is dispatched to its handler — so no path runs a
group hook without passing here. A refused hook is skipped like a hook that
failed (hooks fail open): it neither blocks the tool call nor runs.

The developer's own hooks (user, project, their own plugins) are not affected.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ember_code.core.config.schemas.orchestration import GROUP_HOOK_MODES
from ember_code.core.hooks.schemas import HookDefinition

logger = logging.getLogger(__name__)

#: ``(name, details, reason, agent_path) -> True / False / None`` — the
#: session's in-run approval channel (``Session._ask_about_tool_call``); ``None``
#: means nobody can answer. The same shape as the permission pipeline's
#: ``AskPerson``.
AskAboutGroupHook = Callable[[str, dict[str, Any], str, list[str]], Awaitable[bool | None]]

Decision = Literal["run", "refuse"]

#: The name the approval dialog shows for a group hook.
APPROVAL_NAME = "group_policy_hook"


class GroupHookDecisions:
    """The developer's answers about group hooks, by hook digest.

    Kept in ``~/.igni/group-hook-decisions.json`` — the developer's directory,
    which the group policy cache never writes to. Delete the file to be asked
    again about every hook.
    """

    FILENAME = "group-hook-decisions.json"

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from ember_code.core.paths import home_config_dir

            path = home_config_dir() / self.FILENAME
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Could not read %s (%s); asking again.", self.path, exc)
            return {}
        hooks = data.get("hooks") if isinstance(data, dict) else None
        return hooks if isinstance(hooks, dict) else {}

    def get(self, digest: str) -> Decision | None:
        entry = self._read().get(digest)
        decision = entry.get("decision") if isinstance(entry, dict) else None
        return decision if decision in ("run", "refuse") else None

    def set(self, digest: str, decision: Decision, hook: HookDefinition) -> None:
        hooks = self._read()
        hooks[digest] = {
            "decision": decision,
            "what": GroupHookGate.describe(hook),
            "source": hook.group_source,
            "decided_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".group-hooks-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "hooks": hooks}, f, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            # Not remembered means asked again next time — the safe side.
            logger.warning("Could not record the answer in %s: %s", self.path, exc)


class GroupHookGate:
    """Decides, per dispatch, whether a group-policy hook may run."""

    def __init__(
        self,
        mode: Callable[[], str] | str = "run",
        ask: AskAboutGroupHook | None = None,
        decisions: GroupHookDecisions | None = None,
    ) -> None:
        self._mode = mode if callable(mode) else (lambda: mode)
        self._ask = ask
        self._decisions = decisions if decisions is not None else GroupHookDecisions()
        #: In-flight questions by digest, so two dispatches of one hook at
        #: once put one question to the person.
        self._asking: dict[str, asyncio.Future[bool]] = {}
        self._logged: set[tuple[str, str]] = set()

    @classmethod
    def unwired(cls) -> GroupHookGate:
        """For a dispatcher nobody handed a gate: no group hook runs.

        Every production path passes the session's gate; a path that forgot
        to must not be the one that runs the organisation's code unchecked.
        """
        return cls(mode="refuse", decisions=_NoDecisions())

    # ── reading a hook ─────────────────────────────────────────────

    @staticmethod
    def digest(hook: HookDefinition) -> str:
        """A digest of everything the hook does — its version, for asking."""
        content = json.dumps(hook.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()

    @staticmethod
    def describe(hook: HookDefinition) -> str:
        """What the hook will do, in one line a person can judge."""
        if hook.type == "command":
            return hook.command
        if hook.type == "http":
            return f"POST {hook.url}"
        if hook.type == "mcp_tool":
            return f"MCP tool {hook.mcp_server}/{hook.mcp_tool}"
        if hook.type == "prompt":
            return f"Add to the conversation: {hook.text}"
        return f"{hook.type} hook"

    def mode(self) -> str:
        mode = self._mode()
        # Anything unrecognised is read as the cautious middle.
        return mode if mode in GROUP_HOOK_MODES else "ask"

    # ── deciding ───────────────────────────────────────────────────

    async def permits(self, hook: HookDefinition, event: str) -> bool:
        """True when ``hook`` may run for ``event``. Not a group hook → True."""
        if not hook.from_group:
            return True
        mode = self.mode()
        if mode == "run":
            return True
        if mode == "refuse":
            self._log_once(
                hook,
                "refuse",
                "Not running the group policy's %s hook from %s (hooks.group: refuse): %s",
                event,
            )
            return False

        digest = self.digest(hook)
        recorded = self._decisions.get(digest)
        if recorded is not None:
            if recorded == "refuse":
                self._log_once(
                    hook,
                    "declined",
                    "Not running the group policy's %s hook from %s (you declined it): %s",
                    event,
                )
            return recorded == "run"

        pending = self._asking.get(digest)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._asking[digest] = future
        try:
            answer = await self._put_to_person(hook, event, digest)
            future.set_result(answer)
            return answer
        except BaseException:
            if not future.done():
                future.set_result(False)
            raise
        finally:
            self._asking.pop(digest, None)

    async def _put_to_person(self, hook: HookDefinition, event: str, digest: str) -> bool:
        answer: bool | None = None
        if self._ask is not None:
            try:
                answer = await self._ask(
                    APPROVAL_NAME,
                    {
                        "hook": self.describe(hook),
                        "type": hook.type,
                        "event": event,
                        "source": hook.group_source,
                    },
                    "Your organisation's group policy ships this hook. It runs on this "
                    "machine as you, outside the agent's permissions — --strict does not "
                    "cover it. Run it? (hooks.group: ask — your answer is kept until the "
                    "hook changes.)",
                    ["group policy hook"],
                )
            except Exception as exc:  # fail closed
                logger.warning("Asking about a group hook failed (%s); not running it.", exc)
                answer = None
        if answer is None:
            self._log_once(
                hook,
                "unasked",
                "Not running the group policy's %s hook from %s: hooks.group is 'ask' and "
                "nobody is here to answer. %s",
                event,
            )
            return False
        self._decisions.set(digest, "run" if answer else "refuse", hook)
        logger.info(
            "You %s the group policy's hook from %s: %s",
            "allowed" if answer else "declined",
            hook.group_source,
            self.describe(hook),
        )
        return answer

    def _log_once(self, hook: HookDefinition, why: str, message: str, event: str) -> None:
        mark = (self.digest(hook), why)
        if mark in self._logged:
            return
        self._logged.add(mark)
        logger.warning(message, event, hook.group_source, self.describe(hook))


class _NoDecisions(GroupHookDecisions):
    """Remembers nothing — for :meth:`GroupHookGate.unwired`."""

    def __init__(self) -> None:
        self.path = Path(os.devnull)

    def get(self, digest: str) -> Decision | None:
        return None

    def set(self, digest: str, decision: Decision, hook: HookDefinition) -> None:
        return None


__all__ = [
    "APPROVAL_NAME",
    "AskAboutGroupHook",
    "GroupHookDecisions",
    "GroupHookGate",
]
