"""Boot a real Session and ask it what the group gave it.

``check_group_config.py`` proves each subsystem *can* read the group's
directory when handed it. This proves the session actually hands it
over — the wiring between ``group_dir_for(kind)`` and eight call sites.
The distinction is not academic: it is how MCP servers were found to be
loading from a directory nothing passed to the manager, with every
lower-level test passing.

Runs against a live server: fetches the pack, materialises it into a
throwaway HOME, constructs a Session on a throwaway project, and asks
the pools what they hold.

    IGNI_API=http://localhost:7777 .venv/bin/python scripts/check_group_session.py

Point it at a group holding one entry of each kind. Exits non-zero on
any failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

API = os.environ.get("IGNI_API", "http://localhost:7777")
TOKEN = os.environ.get("IGNI_TOKEN", "local-bypass")

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(f"  {'ok  ' if ok else 'FAIL'}  {what}")


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="igni-session-"))
    home = tmp / "home"
    project = tmp / "project"
    (home / ".igni").mkdir(parents=True)
    (project / ".igni").mkdir(parents=True)

    # A throwaway HOME so this cannot read or write the real one.
    os.environ["HOME"] = str(home)
    os.environ["IGNI_NEO4J_DISABLED"] = "1"

    from ember_code.core.auth.portal_client import PortalClient
    from ember_code.core.config.group_policy import GroupPolicyCache

    print(f"\nFetching the pack from {API}")
    pack = await PortalClient(api_url=API).fetch_group_pack(TOKEN)
    if pack is None:
        print("  FAIL  no pack — is the server up and the user in a group?")
        return 1

    by_kind: dict[str, list[str]] = {}
    for e in pack.entries:
        by_kind.setdefault(e.kind, []).append(e.entry_name)
    print(f"  group {pack.group_name!r}: " + ", ".join(f"{k}×{len(v)}" for k, v in sorted(by_kind.items())))

    cache = GroupPolicyCache(cache_dir=home / ".igni" / "group-policy")
    cache.materialize(pack)

    print("\nBooting a Session")
    from ember_code.core.config.settings import load_settings
    from ember_code.core.session.core import Session

    settings = load_settings(project_dir=project)
    settings.storage.data_dir = str(home / ".igni")
    session = Session(settings, project_dir=project)
    print(f"  built, session {session.identity.session_id}")

    print("\nWhat the session sees")

    if "agents" in by_kind:
        names = set(session.pool.list_names()) if hasattr(session.pool, "list_names") else set(session.pool._entries)
        check(set(by_kind["agents"]) <= names, f"group agents in the pool ({len(by_kind['agents'])})")

    if "skills" in by_kind:
        got = {n for n in by_kind["skills"] if session.skill_pool.get(n)}
        check(got == set(by_kind["skills"]), f"group skills in the skill pool ({len(got)})")

    if "output-styles" in by_kind:
        check(set(by_kind["output-styles"]) <= set(session.output_styles), "group output styles registered")

    if "mcps" in by_kind:
        check(set(by_kind["mcps"]) <= set(session.mcp_manager.configs), "group MCP servers configured")

    if "hooks" in by_kind:
        wanted = {
            json.loads(e.content).get("command")
            for e in pack.entries
            if e.kind == "hooks"
        }
        registered = {
            h.command
            for event in ("PreToolUse", "PostToolUse", "Stop", "SubagentStop")
            for h in session._hook_registry.for_event(event)
        }
        check(wanted <= registered, f"group hooks registered ({', '.join(sorted(wanted))})")

    if "rules" in by_kind:
        check(bool(session.rules_index.consume_path(project / "app" / "main.py")), "group rule fires on a .py file")

    if "commands" in by_kind:
        from ember_code.core.utils.markdown_commands import MarkdownCommand

        found = MarkdownCommand.discover(
            session.project_dir,
            read_claude=settings.rules.cross_tool_support,
        )
        check(set(by_kind["commands"]) <= set(found), "group slash commands resolve")

    print("\nWhat the session decided about igni's own copies")
    check(session._group_ships("agents") == ("agents" in by_kind), "knows whether the group ships agents")
    check(session._group_ships("hooks") == ("hooks" in by_kind), "knows whether the group ships hooks")
    # Inverted deliberately. This used to assert the project directory
    # HELD the group's agents, because a sync copied them there. That
    # sync is gone: the group's entries are read from
    # ``~/.igni/group-policy/agents`` and nothing of the server's is
    # written into a repository. So the check worth making is the
    # opposite one — that none of the group's agents landed in the
    # project, which is what would silently reintroduce two sources of
    # truth for the same content.
    scaffolded = {p.stem for p in (project / ".igni" / "agents").glob("*.md")}
    if "agents" in by_kind:
        leaked = scaffolded & set(by_kind["agents"])
        check(not leaked, f"the group's agents stay out of the project ({sorted(leaked)} leaked)")

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for w in failed:
        print(f"  failed: {w}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
