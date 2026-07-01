"""Reproduce the 'explorer narrates without acting' pattern in isolation.

Hypothesis under test: the model emits text-only (no tool_calls) when given
a long task with shell tools available, and Agno's loop exits because
tool_calls=[] looks like "done".

We instrument the model so we see EXACTLY what's coming back:
  - response.content (text)
  - response.tools  (tool calls Agno saw)
  - response.messages — the raw assistant message turn-by-turn
  - finish_reason if exposed by the OpenAI-compat API
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

# Load .env so EMBER_TEST_LLM_* are available (same as the smoke runner)
env_path = Path(".env")
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def build_model():
    from agno.models.openai.like import OpenAILike

    return OpenAILike(
        id=os.environ["EMBER_TEST_LLM_MODEL"],
        api_key=os.environ["EMBER_TEST_LLM_API_KEY"],
        base_url=os.environ["EMBER_TEST_LLM_BASE_URL"],
    )


def build_explorer_agent(work_dir: Path):
    """Build explorer the same way the smoke runner does — bundled prompt + Bash."""
    from agno.agent import Agent

    from ember_code.core.config.tool_permissions import ToolPermissions
    from ember_code.core.tools.registry import ToolRegistry

    md = Path("agents/explorer.md").read_text()
    fm_match = re.match(r"^---\n.*?\n---\n(.*)", md, re.DOTALL)
    system_prompt = fm_match.group(1) if fm_match else md

    registry = ToolRegistry(
        base_dir=str(work_dir),
        permissions=ToolPermissions(project_dir=work_dir),
    )
    # Match explorer's actual frontmatter toolkit: WebFetch + WebSearch + Bash
    tools = registry.resolve(["WebFetch", "WebSearch", "Bash"])

    return Agent(
        name="explorer",
        model=build_model(),
        instructions=system_prompt,
        tools=tools,
        markdown=True,
    )


# A task description like the one in the user's TUI screenshot — 8 numbered
# steps + 7 sub-bullets. Enough length / structure to trigger the pattern.
LONG_TASK = """\
Explore the igni project architecture comprehensively. Read these
files and directories:

1. Read src/ember_code/backend/ directory listing and key files
   (server.py, command_handler.py)
2. Read src/ember_code/core/ directory listing - list all
   subdirectories and files
3. Read src/ember_code/frontend/tui/ structure
4. Read pyproject.toml
5. Read README.md
6. Read ember.md
7. Read agents/architect.md to understand the architect agent
8. List docs/ directory

Return a detailed architectural summary covering:
- Overall project purpose and what it does
- High-level architecture (client/server, agents, frontend, backend)
- Key modules and their responsibilities
- Agent system design
- Transport/protocol layer
- Frontend structure
- Tech stack (Python, any frameworks)"""


async def main():
    work_dir = Path(tempfile.mkdtemp(prefix="ember-repro-"))
    agent = build_explorer_agent(work_dir)

    print(f"=== Running explorer with the long task in {work_dir} ===\n")
    response = await agent.arun(LONG_TASK, stream=False)

    print("─" * 70)
    print(f"response.content (len={len(response.content or '')}):\n")
    print(response.content or "<empty>")

    print("\n" + "─" * 70)
    print(f"response.tools (count={len(getattr(response, 'tools', None) or [])}):\n")
    for i, t in enumerate(getattr(response, "tools", None) or []):
        name = getattr(t, "tool_name", None)
        args = getattr(t, "tool_args", None)
        result_preview = (str(getattr(t, "result", "") or ""))[:200].replace("\n", " ")
        print(f"  [{i}] {name}({json.dumps(args)})  →  {result_preview!r}")

    print("\n" + "─" * 70)
    msgs = getattr(response, "messages", None) or []
    print(f"response.messages — turn-by-turn (count={len(msgs)}):\n")
    for i, m in enumerate(msgs):
        role = getattr(m, "role", "?")
        content = (getattr(m, "content", None) or "")
        content_short = (content[:200] if isinstance(content, str) else str(content)[:200]).replace("\n", " ")
        tool_calls = getattr(m, "tool_calls", None)
        tc_str = ""
        if tool_calls:
            names = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    names.append(tc.get("function", {}).get("name") or tc.get("name") or "?")
                else:
                    names.append(getattr(tc, "name", "?"))
            tc_str = f"  tool_calls={names}"
        print(f"  [{i}] role={role}{tc_str}")
        if content_short:
            print(f"        content: {content_short}")

    # Look for finish_reason or stop_reason in any message metadata.
    print("\n" + "─" * 70)
    print("metadata sweep for finish_reason / stop_reason:\n")
    for i, m in enumerate(msgs):
        for attr in ("finish_reason", "stop_reason"):
            val = getattr(m, attr, None)
            if val is not None:
                print(f"  msg[{i}].{attr} = {val!r}")
        # Some responses stash this in metadata dict
        meta = getattr(m, "metrics", None) or getattr(m, "metadata", None)
        if meta and not isinstance(meta, str):
            try:
                d = meta if isinstance(meta, dict) else meta.__dict__
                for k, v in d.items():
                    if "finish" in k.lower() or "stop" in k.lower():
                        print(f"  msg[{i}].metrics.{k} = {v!r}")
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
