"""Hook loader — discovers and loads hooks from settings files."""

import json
import sys
from pathlib import Path

from ember_code.core.hooks.schemas import HookDefinition


class HookLoader:
    """Loads hook definitions from settings files."""

    def __init__(self, project_dir: Path | None = None, cross_tool_support: bool = False):
        self.project_dir = project_dir or Path.cwd()
        self.cross_tool_support = cross_tool_support

    def load(self) -> dict[str, list[HookDefinition]]:
        """Load hooks from all settings files.

        Settings locations (merged, later wins):
        1. ~/.ember/settings.json (user global defaults)
        2. ~/.ember/settings.local.json (user local overrides)
        3. .ember/settings.json (project overrides, committed)
        4. .ember/settings.local.json (project local overrides, gitignored)
        """
        hooks: dict[str, list[HookDefinition]] = {}

        home_ember = Path.home() / ".ember"
        paths = [
            home_ember / "settings.json",
            home_ember / "settings.local.json",
            self.project_dir / ".ember" / "settings.json",
            self.project_dir / ".ember" / "settings.local.json",
        ]

        if self.cross_tool_support:
            home_claude = Path.home() / ".claude"
            paths.extend(
                [
                    home_claude / "settings.json",
                    home_claude / "settings.local.json",
                    self.project_dir / ".claude" / "settings.json",
                    self.project_dir / ".claude" / "settings.local.json",
                ]
            )

        for path in paths:
            self._load_from_file(path, hooks)

        return hooks

    def _load_from_file(self, path: Path, hooks: dict[str, list[HookDefinition]]) -> None:
        """Load hooks from a single settings file."""
        if not path.exists():
            return

        try:
            with open(path) as f:
                data = json.load(f)

            hooks_data = data.get("hooks", {})
            self._merge_hooks_data(hooks_data, hooks, source=path)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Failed to load hooks from {path}: {e}", file=sys.stderr)

    def _merge_hooks_data(
        self,
        hooks_data: dict,
        target: dict[str, list[HookDefinition]],
        *,
        source: Path,
        prepend: bool = False,
    ) -> None:
        """Parse a ``{event: [hook, ...]}`` block and merge it into *target*.

        Extracted so plugin hooks (which carry the same shape but live at
        ``<plugin>/hooks/hooks.json`` instead of settings.json) can use
        the same parser without re-stating the schema. ``prepend`` puts
        new hooks at the front of each event's list — used for plugin
        hooks so plugin-supplied behavior fires *before* project hooks,
        letting the project still get the final word (e.g. a project
        PreToolUse veto runs after the plugin's PreToolUse audit log).
        """
        for event_name, hook_list in hooks_data.items():
            if not isinstance(hook_list, list):
                continue
            for hook_data in hook_list:
                hook = HookDefinition(
                    type=hook_data.get("type", "command"),
                    command=hook_data.get("command", ""),
                    url=hook_data.get("url", ""),
                    headers=hook_data.get("headers", {}),
                    text=hook_data.get("text", ""),
                    mcp_server=hook_data.get("mcp_server", ""),
                    mcp_tool=hook_data.get("mcp_tool", ""),
                    mcp_args=hook_data.get("mcp_args", {}),
                    matcher=hook_data.get("matcher", ""),
                    timeout=hook_data.get("timeout", 10000),
                    background=hook_data.get("background", False),
                    # Accept Claude Code's camelCase ``asyncRewake``
                    # AND a snake_case alias for parity with the
                    # rest of ember-code's settings.
                    async_rewake=hook_data.get("asyncRewake", hook_data.get("async_rewake", False)),
                )
                bucket = target.setdefault(event_name, [])
                if prepend:
                    bucket.insert(0, hook)
                else:
                    bucket.append(hook)

    def load_plugin_hooks(
        self,
        plugin_dir: Path,
        target: dict[str, list[HookDefinition]],
    ) -> None:
        """Merge ``<plugin_dir>/hooks/hooks.json`` into *target*.

        Same schema as settings.json's ``hooks`` block — the file IS
        the ``hooks`` block (no outer wrapping). Plugins are prepended
        so project-level hooks still run last.

        Failures (missing, malformed, OSError) are logged and swallowed
        so one broken plugin can't take down the whole hooks pipeline.
        """
        path = plugin_dir / "hooks" / "hooks.json"
        if not path.is_file():
            return
        try:
            with open(path) as f:
                hooks_data = json.load(f)
            if not isinstance(hooks_data, dict):
                print(
                    f"Warning: plugin hooks at {path} is not a JSON object — skipping",
                    file=sys.stderr,
                )
                return
            self._merge_hooks_data(hooks_data, target, source=path, prepend=True)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Failed to load plugin hooks from {path}: {e}", file=sys.stderr)
