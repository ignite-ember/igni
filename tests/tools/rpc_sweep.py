"""Call every RPC the backend exposes and report what happens.

Not a test — a probe, run by hand against a live backend::

    python -m ember_code.backend --ws-port 0        # note the ws_url
    python tests/tools/rpc_sweep.py ws://127.0.0.1:PORT

Why this exists: the app's surface is 104 RPC methods, and driving
them through the UI means driving them through a language model that
decides for itself whether to call a tool. Half the effort in the
browser-level pass went into that non-determinism rather than into the
product. These methods are a wire contract, and a wire contract can be
called directly — deterministically, in seconds, with no model.

What it establishes is narrow and worth being honest about: that a
method **exists, accepts the arguments given, and returns without
raising**. It does not establish that the answer is right. A method
that returns `[]` because the feature is broken looks identical here
to one that returns `[]` because there is nothing to list. Anything
marked `verified` in the matrix on the strength of this sweep says so.

Read-only by default. Mutating methods are listed separately and only
run with ``--mutating``, because "call every RPC" against a backend
someone is using would delete their sessions.

**``--mutating`` leaves traces.** ``save_permission_rule`` writes a
real rule into ``.igni/settings.local.json`` — the sweep does not
clean up after itself, because a probe that edits files it did not
create is worse than one that says what it left. Snapshot that file
first if you care about it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_RPC_SOURCE = _ROOT / "src/ember_code/protocol/rpc.py"

#: Arguments for methods that need them. Anything absent is called
#: with ``{}`` — which is itself worth knowing, since a method that
#: raises on empty args has an undocumented requirement.
ARGS: dict[str, dict] = {
    # Argument names taken from the handlers, not guessed. Getting one
    # wrong surfaces as a bare KeyError — the reply is literally
    # `'session_id'` — so a caller cannot tell a missing argument from
    # a broken method. Noted in the matrix.
    "get_pending_messages": {"session_id": "SESSION"},
    "get_chat_history": {"session_id": "SESSION"},
    "get_interrupted_runs": {"session_id": "SESSION"},
    "search_chat": {"session_id": "SESSION", "query": "the"},
    "knowledge_get": {"id": ""},
    "preview_plugin": {"source": ""},
    "search_code": {"snippet": "def"},
    # Three required keys, discovered one bare KeyError at a time:
    # `tool_args`, then `tool_name`, then `func_name`. See the note
    # above about what the error messages do not say.
    "check_permission": {
        "tool_name": "Bash",
        "func_name": "run_shell_command",
        "tool_args": {"command": "echo hi"},
    },
    "get_agent_details": {"name": "explorer"},
    "get_skill_details": {"name": ""},
    "get_plugin_details": {"name": ""},
    "get_plugin_contents": {"name": ""},
    "get_mcp_server_details": {"name": ""},
    "read_file": {"path": "igni.md"},
    "complete_files": {"prefix": ""},
    "knowledge_search": {"query": "test"},
    "count_context_tokens": {"text": "hello"},
    "read_process_tail": {"pid": 0},
    "list_dirs": {"path": "."},
    # Mutating, with arguments taken from the handlers. Values chosen
    # to be answerable but inert: a model that exists, names that do
    # not, a zero-length attachment. What is being established is that
    # the method accepts its arguments and returns — not that the
    # side effect is right, which needs a person.
    "switch_model": {"model_name": "DeepSeek-V4-Pro"},
    "truncate_history": {"session_id": "SESSION", "run_id": ""},
    "compact_if_needed": {
        "ctx_tokens": 100,
        "max_ctx": 100000,
        "user_msg": "hello",
        "assistant_msg": "hi",
    },
    "discard_ephemeral_agent": {"name": "no-such-agent"},
    "promote_ephemeral_agent": {"name": "no-such-agent"},
    "discard_interrupted_run": {"session_id": "SESSION", "message_id": ""},
    "set_plugin_enabled": {"name": "no-such-plugin", "enabled": False, "ref": ""},
    "upload_attachment": {
        "filename": "sweep.txt",
        "content_base64": "",
        "session_id": "SESSION",
    },
    "extract_learnings": {"user_msg": "hello", "assistant_msg": "hi"},
    "knowledge_add": {"source": "sweep-probe"},
    "knowledge_remove": {"id": "no-such-doc"},
    "mcp_connect": {"server_name": "no-such-server"},
    "mcp_disconnect": {"server_name": "no-such-server"},
    "remove_plugin": {"name": "no-such-plugin"},
    "install_plugin": {"ref": ""},
    "update_plugin": {"name": "no-such-plugin"},
    "add_marketplace": {"url": ""},
    "remove_marketplace": {"name": "no-such-marketplace"},
    "cancel_scheduled_task": {"task_id": "no-such-task"},
    "execute_scheduled_task": {"description": "sweep probe"},
    "cancel_workflow": {"workflow_run_id": "no-such-run"},
    "run_workflow": {"name": "no-such-workflow"},
    "save_permission_rule": {"rule": "Bash(sweep-probe)", "level": "ask"},
    "set_mcp_tool_enabled": {
        "server": "no-such-server",
        "tool": "no-such-tool",
        "enabled": False,
    },
}

#: Never called, whatever the flags say.
#:
#: Not "risky" — *irreversible for the person running this*.
#: ``logout`` and ``clear_cloud_credentials`` discard the operator's
#: stored token; ``login`` and ``pick_dir_native`` open a browser and
#: a native dialog and then block forever; ``shutdown`` kills the
#: backend the rest of the sweep is talking to. A probe that empties
#: somebody's credentials while enumerating an API is not a probe
#: worth having.
NEVER = {
    "login",
    "logout",
    "clear_cloud_credentials",
    "reload_cloud_credentials",
    "pick_dir_native",
    "shutdown",
}

#: Methods that change state. Not called unless asked for.
MUTATING = re.compile(
    r"^(set_|delete_|remove_|install_|update_|add_|clear_|cancel_|stop_|discard_|"
    r"truncate_|switch_|attach_|shutdown|login|logout|approve_|dismiss_|save_|"
    r"promote_|upload_|execute_|start_|run_|reload_|refresh_|compact_|extract_|"
    r"codeindex_(install|sync|resync|clean)|mcp_|ensure_|knowledge_(add|remove)|"
    r"auto_sync|loop_|pop_|fire_|toggle_|pick_|dispatch_|check_for_update)"
)


def methods() -> list[str]:
    """Every method name in the ``RpcMethod`` enum."""
    return sorted(
        set(re.findall(r'^\s+[A-Z_0-9]+\s*=\s*"([a-z_0-9]+)"', _RPC_SOURCE.read_text(), re.M))
    )


async def sweep(url: str, include_mutating: bool) -> dict[str, tuple[str, str]]:
    import websockets

    results: dict[str, tuple[str, str]] = {}
    async with websockets.connect(url, max_size=None) as ws:
        # The backend greets first; drain whatever arrives before we
        # start so replies are not confused with pushes.
        await asyncio.sleep(1.0)

        async def call(method: str, args: dict, req_id: str, timeout: float = 15.0):
            """Send one request and wait for *its* reply.

            The socket also carries unrelated pushes, so replies are
            matched on id rather than taken in order.
            """
            await ws.send(
                json.dumps(
                    {"type": "rpc_request", "id": req_id, "method": method, "args": args}
                )
            )
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
                except (TimeoutError, asyncio.TimeoutError):
                    return None
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                # Matched on **id alone**, not on `type == "rpc_response"`.
                # The dispatcher special-cases handlers that return a
                # `Message`: it stamps the request id onto that message
                # and sends it as-is, so `get_status` and
                # `list_sessions` reply with their own types. Matching
                # on the type reported both as 15s timeouts — two core
                # methods declared broken by a prober that was not
                # listening for their answer.
                if msg.get("id") == req_id:
                    return msg
            return None

        # A real session id, so the history/search methods get
        # something that exists instead of a placeholder that makes
        # every one of them look broken.
        reply = await call("get_session_id", {}, "sweep-session")
        session_id = ""
        if reply and not reply.get("error"):
            result = reply.get("result")
            session_id = result if isinstance(result, str) else (result or {}).get("session_id", "")
        args_for = {
            k: {kk: (session_id if vv == "SESSION" else vv) for kk, vv in v.items()}
            for k, v in ARGS.items()
        }

        for i, method in enumerate(methods()):
            if method in NEVER:
                results[method] = ("excluded", "irreversible or blocking — never called")
                continue
            if MUTATING.match(method) and not include_mutating:
                results[method] = ("skipped", "mutating; pass --mutating to include")
                continue

            msg = await call(method, args_for.get(method, {}), f"sweep-{i}")
            if msg is None:
                outcome = ("timeout", "no reply in 15s")
            elif msg.get("error"):
                outcome = ("error", str(msg["error"])[:160])
            else:
                shape = type(msg.get("result")).__name__
                size = ""
                if isinstance(msg.get("result"), (list, dict)):
                    size = f" len={len(msg['result'])}"
                outcome = ("ok", f"{shape}{size}")
            results[method] = outcome
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="ws://127.0.0.1:PORT from the backend's ready line")
    parser.add_argument(
        "--mutating",
        action="store_true",
        help="also call methods that change state — never against a backend in use",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="write the per-method outcome here, so a document can cite it "
        "instead of somebody retyping the result from memory",
    )
    args = parser.parse_args()

    results = asyncio.run(sweep(args.url, args.mutating))

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {m: {"outcome": o, "detail": d} for m, (o, d) in sorted(results.items())},
                indent=1,
            )
            + "\n"
        )

    by_outcome: dict[str, list[str]] = {}
    for method, (outcome, detail) in sorted(results.items()):
        by_outcome.setdefault(outcome, []).append(f"{method}: {detail}")

    for outcome in ("error", "timeout", "ok", "skipped", "excluded"):
        rows = by_outcome.get(outcome, [])
        if not rows:
            continue
        print(f"\n=== {outcome} ({len(rows)}) ===")
        for row in rows:
            print(f"  {row}")

    print(
        f"\n{len(results)} methods · "
        + " · ".join(f"{k} {len(v)}" for k, v in sorted(by_outcome.items()))
    )
    return 1 if by_outcome.get("error") or by_outcome.get("timeout") else 0


if __name__ == "__main__":
    sys.exit(main())
