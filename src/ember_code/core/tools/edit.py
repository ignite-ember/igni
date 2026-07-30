"""EmberEditTools — targeted string-replacement editing."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agno.tools import Toolkit
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class FileEditNotifier:
    """Owns the file-edit notification channel.

    The callback lives on an instance rather than a module global,
    so multiple notifiers can coexist (e.g. a test constructs its
    own without leaking into the shared default).

    Wiring:

    - The backend constructs a :class:`PushNotificationBridge` whose
      :meth:`PushNotificationBridge.bind_to_file_edit_listener` calls
      :meth:`set_listener` on the notifier shared with the toolkit.
    - :class:`EmberEditTools` calls :meth:`notify` after every
      successful write.

    Downstream clients react to ``file_edited`` PushNotifications:

    - JetBrains plugin  -> ``LocalFileSystem.refreshAndFindFileByPath``
      (Local History captures the change, open editor tabs reload,
      the "modified externally" prompt stops firing).
    - VSCode extension  -> ``workspace.openTextDocument`` reveal +
      ``editor.action.revert`` reload.
    - Tauri / web       -> no-op (the FE doesn't own an editor).
    """

    def __init__(self, listener: Callable[[str], None] | None = None) -> None:
        self._listener: Callable[[str], None] | None = listener

    def set_listener(self, listener: Callable[[str], None] | None) -> None:
        """Register (or clear) the callback fired after each successful
        edit. ``listener`` receives the absolute path of the file that
        was written.
        """
        self._listener = listener

    def notify(self, path: Path) -> None:
        """Fire the listener with ``str(path)``.

        Exceptions raised by the listener are swallowed and logged at
        DEBUG so a flaky observer can never break an edit — the tool
        contract is "the file was written", not "every observer
        succeeded".
        """
        listener = self._listener
        if listener is None:
            return
        try:
            listener(str(path))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("file-edit listener raised: %s", exc)


# Module-level default notifier — the shared instance the backend
# wires to and every toolkit-constructed-without-an-explicit-notifier
# falls back to. Exposing a single default keeps the injection point
# one line at each wiring site while still moving the mutable state
# off the module and onto an instance.
default_file_edit_notifier = FileEditNotifier()


class EditResult(BaseModel):
    """Typed result of one :class:`EmberEditTools` operation.

    Mirrors :class:`ember_code.core.monitors.models.MonitorControlResult`
    — callers still receive a Toolkit-facing string via ``__str__``
    (Agno tool functions must return ``str``), but structured fields
    are available for downstream introspection.
    """

    ok: bool
    path: str
    reason: str
    count: int | None = None

    def __str__(self) -> str:
        return self.reason


class EmberEditTools(Toolkit):
    """Targeted string-replacement editing tools.

    Instead of rewriting entire files, these tools replace specific text spans,
    producing minimal, reviewable diffs. Inspired by Claude Code's Edit tool.
    """

    def __init__(
        self,
        base_dir: str | None = None,
        *,
        notifier: FileEditNotifier | None = None,
        requires_confirmation_tools: list[str] | None = None,
        **toolkit_kwargs: Any,
    ):
        """Construct an edit toolkit.

        Args:
            base_dir: Working directory for relative-path edits.
                Defaults to the current working directory.
            notifier: :class:`FileEditNotifier` fired after each
                successful write. Defaults to the module-level
                :data:`default_file_edit_notifier` so backend-wired
                listeners see edits without per-toolkit plumbing.
            requires_confirmation_tools: Tool names that should gate
                on human-in-the-loop confirmation. Threaded through
                to Agno's ``requires_confirmation`` flag on both the
                sync and async function registries.
            **toolkit_kwargs: Forwarded verbatim to
                :class:`agno.tools.Toolkit` (``name``, ``tool_hooks``,
                etc.). Explicit passthrough — no silent kwarg drop.
        """
        super().__init__(name="ember_edit", **toolkit_kwargs)
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        # Share the module-level notifier by default so a single
        # backend-set listener sees edits from every toolkit instance.
        self._notifier = notifier or default_file_edit_notifier
        self.register(self.edit_file)
        self.register(self.edit_file_replace_all)
        self.register(self.create_file)
        if requires_confirmation_tools:
            self.requires_confirmation_tools = requires_confirmation_tools
            # Agno routes async callables into ``async_functions``;
            # sync ones into ``functions``. Both must be gated —
            # skipping either silently disables HITL for that half.
            for registry in (self.functions, self.async_functions):
                for name, func in registry.items():
                    if name in requires_confirmation_tools:
                        func.requires_confirmation = True

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to base_dir."""
        p = Path(path)
        if not p.is_absolute():
            p = self.base_dir / p
        return p

    def edit_file(self, file_path: str, old_string: str, new_string: str) -> str:
        """Replace a specific string in a file. The old_string must appear exactly once.

        Args:
            file_path: Path to the file to edit.
            old_string: The exact text to find and replace. Must be unique in the file.
            new_string: The replacement text.

        Returns:
            Success or error message.
        """
        path = self._resolve_path(file_path)

        if not path.exists():
            return str(
                EditResult(ok=False, path=str(path), reason=f"Error: File not found: {path}")
            )

        content = path.read_text(encoding="utf-8")
        count = content.count(old_string)

        if count == 0:
            return str(
                EditResult(
                    ok=False,
                    path=str(path),
                    reason=(
                        f"Error: old_string not found in {path}. Make sure the string "
                        "matches exactly (including whitespace and indentation)."
                    ),
                )
            )

        if count > 1:
            return str(
                EditResult(
                    ok=False,
                    path=str(path),
                    count=count,
                    reason=(
                        f"Error: old_string appears {count} times in {path}. Provide "
                        "more surrounding context to make it unique, or use "
                        "edit_file_replace_all."
                    ),
                )
            )

        new_content = content.replace(old_string, new_string, 1)
        path.write_text(new_content, encoding="utf-8")
        self._notifier.notify(path)

        return str(EditResult(ok=True, path=str(path), reason=f"Successfully edited {path}"))

    def edit_file_replace_all(self, file_path: str, old_string: str, new_string: str) -> str:
        """Replace ALL occurrences of a string in a file.

        Args:
            file_path: Path to the file to edit.
            old_string: The text to find.
            new_string: The replacement text.

        Returns:
            Success message with count of replacements.
        """
        path = self._resolve_path(file_path)

        if not path.exists():
            return str(
                EditResult(ok=False, path=str(path), reason=f"Error: File not found: {path}")
            )

        content = path.read_text(encoding="utf-8")
        count = content.count(old_string)

        if count == 0:
            return str(
                EditResult(
                    ok=False,
                    path=str(path),
                    reason=f"Error: old_string not found in {path}.",
                )
            )

        new_content = content.replace(old_string, new_string)
        path.write_text(new_content, encoding="utf-8")
        self._notifier.notify(path)

        return str(
            EditResult(
                ok=True,
                path=str(path),
                count=count,
                reason=f"Successfully replaced {count} occurrence(s) in {path}",
            )
        )

    def create_file(self, file_path: str, content: str) -> str:
        """Create a new file. Fails if the file already exists.

        Args:
            file_path: Path for the new file.
            content: File content.

        Returns:
            Success or error message.
        """
        path = self._resolve_path(file_path)

        if path.exists():
            return str(
                EditResult(
                    ok=False,
                    path=str(path),
                    reason=(f"Error: File already exists: {path}. Use edit_file to modify it."),
                )
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._notifier.notify(path)

        return str(EditResult(ok=True, path=str(path), reason=f"Successfully created {path}"))
