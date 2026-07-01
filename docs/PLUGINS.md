# Plugins

igni supports **Claude-Code-compatible plugins**. A plugin is a directory bundling skills, agents, hooks, MCP servers, and custom tools — installed once, namespaced automatically, toggleable per session. Plugins built for Claude Code work in igni unchanged.

## Quick start

```text
/plugin install https://github.com/foo/some-plugin.git
/plugins                         # open the panel
/plugins disable some-plugin     # turn it off without uninstalling
```

The first command clones the repo into `~/.ember/plugins/some-plugin/` and pins the SHA. On next session start, the plugin's bundled contents activate automatically.

## How plugins are discovered

igni scans four roots in priority order (later wins same-name collisions):

| Priority | Path | Use case |
|---:|---|---|
| 1 | `~/.claude/plugins/` | Pick up whatever Claude Code installed |
| 2 | `~/.ember/plugins/` | Ember-managed user-global plugins (where `/plugin install` lands) |
| 3 | `<project>/.claude/plugins/` | Project-local Claude plugins |
| 4 | `<project>/.ember/plugins/` | Project-local Ember plugins (committed alongside the repo) |

A "plugin" = any directory under one of these roots that contains `.claude-plugin/plugin.json`. Anything else is ignored silently — leaves room for `README.md`, gitkeeps, scratch notes, etc.

## Plugin format

```text
my-plugin/
├── .claude-plugin/
│   └── plugin.json        # required — manifest (name, version, description, author)
├── skills/
│   └── <name>/SKILL.md    # optional — auto-namespaced as <plugin>:<name>
├── agents/
│   └── <name>.md          # optional — auto-namespaced as <plugin>:<name>
├── hooks/
│   └── hooks.json         # optional — same shape as settings.json's "hooks" block
├── .mcp.json              # optional — same shape as project .mcp.json; servers prefixed <plugin>:<server>
└── tools/
    └── <name>.py          # optional — Agno @tool functions; toolkit named custom_<plugin>_<name>
```

`plugin.json` is the only required file. Everything else is opt-in — a plugin that ships nothing but a manifest is still a valid (empty) plugin.

### Manifest

Only `name` is required.

```json
{
  "name": "my-plugin",
  "version": "1.2.0",
  "description": "Adds X, Y, and Z to your agents.",
  "author": "Ada Lovelace"
}
```

Unknown fields are preserved (`extra="allow"`) — Claude Code's manifest evolution won't break loading.

### Namespacing

Skills and agents from a plugin land in their respective pools under `<plugin>:<original-name>`. So a plugin named `git-extras` shipping `skills/rebase-clean/SKILL.md` is invoked as `/git-extras:rebase-clean`. This guarantees:

- Two plugins can ship the same skill name without colliding.
- Your `~/.ember/skills/` or `~/.ember/agents/` always wins over a plugin's same-named entry.

MCP servers get the same treatment (`<plugin>:<server>`). Custom-tool toolkits are named `custom_<plugin>_<file>`.

### Hook ordering

Plugin hooks are **prepended** to each event's hook list — project hooks (from `settings.json`) still run last so a project veto can always override plugin behavior.

## Installing plugins

### Direct URL

```text
/plugin install https://github.com/foo/some-plugin.git
/plugin install https://github.com/foo/some-plugin.git --ref v1.4.0     # pin a release
/plugin install https://github.com/foo/some-plugin.git --ref a1b2c3d    # pin a SHA
```

The installer clones to a temp directory, validates the manifest, then atomically moves to `~/.ember/plugins/<name>/`. A failed install leaves no trace.

### Via marketplace

A marketplace is a git repo with `.claude-plugin/marketplace.json` listing plugins by name + git URL. Register one first, then install by friendly ref:

```text
/plugin marketplace add https://github.com/some-org/some-marketplace
/plugin marketplace list
/plugin install @some-marketplace/some-plugin
```

igni reads Claude Code's `marketplace.json` schema, so any catalog built for Claude works here.

### Updating + removing

```text
/plugin update some-plugin                # fetch + reset to origin's HEAD
/plugin update some-plugin --ref v2.0.0   # switch the pin
/plugin remove some-plugin                # rm -rf + clear pin
```

`/plugin update` records the new SHA in `~/.ember/plugins.json`. `/plugin remove` deletes the plugin directory and drops the pin + disabled-list entry.

## The plugins panel

`/plugins` opens a Textual panel with two tabs:

```text
┌─ Plugins ────────────────────────────────────────────────────┐
│ ▸ ● git-extras           v1.2.0  · user-ember · S A          │
│   ○ slack-tools          v0.9.1  · user-claude · S           │
│                                                              │
│ ↑/↓ navigate · Space toggle · u update · r remove ·          │
│ Tab marketplace · Esc close                                  │
└──────────────────────────────────────────────────────────────┘
```

`Tab` switches to the **Marketplace** tab to browse catalogs and install with `i`. `R` refreshes all catalogs.

### Slash + panel modes

| Form | Surface |
|---|---|
| `/plugins` | Opens the TUI panel |
| `/plugins enable <name>` | Text-mode toggle (no panel) |
| `/plugins disable <name>` | Text-mode toggle (no panel) |
| `/plugin install ...` etc. | Text-mode (the panel can also drive these) |

## Enable / disable

Toggling a plugin **doesn't uninstall** it — it just skips the plugin's contents at session start. Use this when you want to A/B a plugin's effect on the agent without losing the install pin.

State lives at `~/.ember/plugins.json`:

```json
{
  "disabled": ["some-plugin"],
  "pins": {
    "some-plugin": "a1b2c3d…"
  }
}
```

`disabled` is the toggle list. `pins` is the install-time SHA per plugin, surfaced in the panel for drift detection.

Changes take effect **on next session start**. Hot-reload across all five extension surfaces (skills, agents, hooks, MCP, tools) is intentionally not implemented in v1 — too many cross-cutting cache invalidations.

## Marketplaces

```text
/plugin marketplace add <git-url>             # register
/plugin marketplace list                      # show registered + plugin counts + last_fetched
/plugin marketplace remove <name>             # unregister (installed plugins stay)
/plugin marketplace refresh [<name>]          # re-fetch one or all catalogs
```

Marketplaces are stored at `~/.ember/marketplaces.json` with their cached catalogs. On every session start, all registered catalogs are refreshed in the background (10s per marketplace timeout, log-and-swallow failures) so `@<marketplace>/<plugin>` install refs are always against current data without slowing startup.

No marketplaces are bootstrapped by default — register the ones you want via `/plugin marketplace add`.

## Writing your own plugin

The minimum viable plugin:

```text
my-plugin/
└── .claude-plugin/
    └── plugin.json
```

with:

```json
{ "name": "my-plugin", "version": "0.1.0" }
```

Add `skills/`, `agents/`, `hooks/hooks.json`, `.mcp.json`, `tools/<file>.py` as you build out the bundle. To test locally without publishing:

```text
mkdir -p ~/.ember/plugins/my-plugin/.claude-plugin
# … drop your files in …
# restart your ember session
/plugins  # should list `my-plugin`
```

Or commit the directory under `<project>/.ember/plugins/my-plugin/` to ship it alongside the repo.

### Cross-tool compatibility

Plugins built for Claude Code work in igni with no changes — same manifest, same `skills/` / `agents/` / `hooks/` / `.mcp.json` shapes. Plugins built for igni with `tools/<file>.py` Python tools are an Ember-specific extension and won't be picked up by Claude Code (which has no equivalent loader), but the rest of the plugin still works there.

## Limitations (v1)

- **LSP servers**, **background monitors**, plugin `bin/` executables on PATH, and plugin-bundled `settings.json` defaults are recognized by Claude Code but **not loaded** by igni yet. The rest of the plugin still works.
- **Hot reload** isn't supported. Enable/disable, install, update, remove all require a session restart to apply.
- **Per-project disable** isn't supported — `~/.ember/plugins.json` is user-global.
- **Private marketplace auth** relies on your ambient git credentials (SSH key, gh CLI, etc.). No token storage in v1.

## Reference

- Plugin loader: [`core/plugins/loader.py`](../src/ember_code/core/plugins/loader.py)
- Installer (git operations): [`core/plugins/installer.py`](../src/ember_code/core/plugins/installer.py)
- Marketplace registry: [`core/plugins/marketplaces.py`](../src/ember_code/core/plugins/marketplaces.py)
- State files: `~/.ember/plugins.json`, `~/.ember/marketplaces.json`
- TUI panel: [`frontend/tui/widgets/_plugins_panel.py`](../src/ember_code/frontend/tui/widgets/_plugins_panel.py)
