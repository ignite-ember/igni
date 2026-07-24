"""GUI-panel RPC handlers (file picker, shell, folder browse, agents,
slash commands, todos, viz, background processes, output styles).

This is the biggest handler cluster by method count because it maps
to a single conceptual thing — "everything the GUI panels need to
render themselves that isn't in another well-defined subsystem". If
this class approaches the Pattern-8 ceiling in the future, split off
the watcher/panel cluster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ember_code.backend.dir_scanner import DirScanner
from ember_code.backend.folder_picker import NativeFolderPicker
from ember_code.backend.rpc_handlers.base import RpcHandler, rpc
from ember_code.backend.schemas_panels import (
    DiscardEphemeralResult,
    OutputStylesResult,
    PromoteEphemeralResult,
    SlashCommandEntry,
)
from ember_code.backend.schemas_rpc import (
    DirListResult,
    FileCompletion,
    PickDirResult,
    RunShellResult,
)
from ember_code.core.agents import AgentInfo
from ember_code.protocol.rpc import RpcMethod


class PanelRpcHandler(RpcHandler):
    """GUI panel + watcher RPC surface."""

    @rpc(RpcMethod.COMPLETE_FILES)
    async def complete_files(self, args: dict) -> FileCompletion:
        query = str(args.get("query", ""))
        limit = int(args.get("limit", 50))
        return await self._ctx.file_completion.complete(query, limit)

    @rpc(RpcMethod.RUN_SHELL)
    async def run_shell(self, args: dict) -> RunShellResult:
        """``$``-prefix shell mode. Captured (non-interactive) by
        design — parity with the TUI's inline shell for the common
        cases."""
        return await self._ctx.shell_runner.run(str(args.get("command", "")))

    @rpc(RpcMethod.LIST_DIRS)
    async def list_dirs(self, args: dict) -> DirListResult:
        """Subdirectory listing for the GUI folder browser.

        Same trust level as ``run_shell`` (local user over loopback).
        Dot-dirs are filtered unless ``show_hidden`` — the browser is
        for picking project roots, not spelunking.
        """
        raw = str(args.get("path") or Path.home())
        show_hidden = bool(args.get("show_hidden", False))
        return await DirScanner(Path(raw), show_hidden).scan_async()

    @rpc(RpcMethod.PICK_DIR_NATIVE)
    async def pick_dir_native(self, args: dict) -> PickDirResult:
        """Open the OS folder picker on this machine, return the path.

        The BE always runs on the user's machine (loopback-only
        transport), so the dialog appears on their desktop — even
        when the view is a plain browser tab that could never get a
        real path out of its own sandboxed file dialogs. No timeout:
        the user may take their time; the FE uses a long RPC timeout
        for this call.
        """
        start = Path(str(args.get("start") or self._ctx.backend.project_dir)).expanduser()
        start_dir = str(start) if start.is_dir() else ""
        picker = NativeFolderPicker.for_platform(start_dir)
        return await picker.pick()

    @rpc(RpcMethod.GET_AGENT_DETAILS)
    def get_agent_details(self, args: dict) -> list[AgentInfo]:
        return self._ctx.backend.get_agent_details()

    @rpc(RpcMethod.PROMOTE_EPHEMERAL_AGENT)
    def promote_ephemeral_agent(self, args: dict) -> PromoteEphemeralResult:
        return self._ctx.backend.promote_ephemeral_agent(args["name"])

    @rpc(RpcMethod.DISCARD_EPHEMERAL_AGENT)
    def discard_ephemeral_agent(self, args: dict) -> DiscardEphemeralResult:
        return self._ctx.backend.discard_ephemeral_agent(args["name"])

    @rpc(RpcMethod.GET_SLASH_COMMANDS)
    def get_slash_commands(self, args: dict) -> list[SlashCommandEntry]:
        return self._ctx.backend.get_slash_commands()

    @rpc(RpcMethod.GET_TODOS)
    def get_todos(self, args: dict) -> Any:
        return self._ctx.backend.get_todos()

    @rpc(RpcMethod.DISPATCH_VISUALIZATION_ACTION)
    def dispatch_visualization_action(self, args: dict) -> Any:
        return self._ctx.backend.dispatch_visualization_action(
            action=str(args.get("action", "")),
            params=args.get("params") or {},
        )

    @rpc(RpcMethod.LIST_BACKGROUND_PROCESSES)
    def list_bg_processes(self, args: dict) -> Any:
        return self._ctx.backend.list_background_processes()

    @rpc(RpcMethod.READ_PROCESS_TAIL)
    def read_process_tail(self, args: dict) -> Any:
        return self._ctx.backend.read_process_tail(
            pid=int(args.get("pid", 0)),
            tail=int(args.get("tail", 200)),
        )

    @rpc(RpcMethod.STOP_BACKGROUND_PROCESS)
    def stop_bg_process(self, args: dict) -> Any:
        return self._ctx.backend.stop_background_process(pid=int(args.get("pid", 0)))

    @rpc(RpcMethod.GET_OUTPUT_STYLES)
    def get_output_styles(self, args: dict) -> OutputStylesResult:
        return self._ctx.backend.get_output_styles()
