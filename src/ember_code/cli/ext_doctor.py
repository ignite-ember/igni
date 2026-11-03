"""What ``igni ext doctor`` checks beyond the manifest's shape.

Two things the manifest cannot tell you by being valid:

* **Names and grants.** One tool goes by four names — the server's own
  (``lookup_ticket``), the manifest's (``acme.tickets.lookup_ticket``),
  the page's (``ext.acme.tickets.lookup_ticket``) and the one it is
  registered with the agent under. A grant in the wrong form is legal
  and does nothing, so it is warned about rather than refused.
* **The process.** Started the way igni starts it — a ``dev`` command
  from the bundle, a ``python`` package installed into a throwaway
  environment from its wheelhouse or its source — then asked, over MCP,
  for its tools, with a timeout. A process that dies at import or
  offers tools its manifest does not declare used to pass ``doctor`` and
  fail at install, where the reason was harder to find.

Everything it makes is in a temporary directory and deleted after.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ember_code.core.config.tool_names import wire_name
from ember_code.core.mcp.naming import tool_name_prefix
from ember_code.extensions.manifest import ExtensionManifest

#: How many lines of a process's stderr a failure quotes.
STDERR_LINES = 12


def server_name(extension_id: str) -> str:
    """The MCP server name igni registers an extension under."""
    from ember_code.extensions.installer import server_name as installer_server_name

    return installer_server_name(extension_id)


@dataclass
class Probe:
    """What starting the process found."""

    tools: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Findings:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class NameCheck:
    """The manifest's tool and rpc names, against the extension's namespace."""

    def __init__(self, manifest: ExtensionManifest) -> None:
        self._manifest = manifest
        self._id = manifest.id

    def check(self, findings: Findings) -> None:
        prefix = f"{self._id}."
        for tool in self._manifest.permissions.tools:
            if not tool.name.startswith(prefix):
                findings.warnings.append(
                    f"permissions.tools names {tool.name!r} without the extension's id. "
                    f"igni matches it to the server's tool either way, but the name an "
                    f"administrator reads and approves is '<id>.<tool>': write "
                    f"{(prefix + tool.name)!r}."
                )
        page = f"ext.{self._id}."
        for grant in self._manifest.permissions.rpc:
            if grant == "*":
                continue  # a problem of its own, reported by doctor
            if not grant.startswith(page):
                findings.warnings.append(
                    f"permissions.rpc grants {grant!r}, outside this extension's "
                    f"namespace. A page calls its own tools as '{page}<tool>' and "
                    f"nothing else is routed to it, so this grants nothing: write "
                    f"'{page}<tool>' or '{page}*'."
                )

    def bare(self, declared: str) -> str:
        prefix = f"{self._id}."
        return declared[len(prefix) :] if declared.startswith(prefix) else declared

    def compare(self, offered: list[str], findings: Findings) -> None:
        declared = {self.bare(t.name): t.name for t in self._manifest.permissions.tools}
        for tool in offered:
            if tool in declared:
                continue
            if self._manifest.grants_rpc(f"ext.{self._id}.{tool}"):
                findings.notes.append(
                    f"{tool!r} is for the page only: permissions.rpc grants it, and the "
                    "agent is not given it because permissions.tools does not declare it."
                )
            else:
                findings.warnings.append(
                    f"it offers {tool!r}, which neither permissions.tools (the agent) nor "
                    "permissions.rpc (the page) grants, so nothing can call it. Declare "
                    f"it as {self._id}.{tool} with its four hints, or stop offering it."
                )
        for bare, name in declared.items():
            if bare not in offered:
                findings.problems.append(
                    f"permissions.tools declares {name!r} and the process does not offer "
                    f"a tool called {bare!r}"
                )

    def table(self, offered: list[str]) -> list[str]:
        """One row per offered tool: every name it goes by."""
        registered_prefix = tool_name_prefix(server_name(self._id))
        declared = {self.bare(t.name) for t in self._manifest.permissions.tools}
        rows = []
        for tool in offered:
            page = f"ext.{self._id}.{tool}"
            rows.append(
                f"    {tool:<22} manifest {self._id}.{tool}"
                + ("" if tool in declared else " (not declared)")
                + f"\n    {'':<22} page     {page}"
                + ("" if self._manifest.grants_rpc(page) else " (not granted in permissions.rpc)")
                + f"\n    {'':<22} registry {registered_prefix}_{tool}"
                + f"\n    {'':<22} model    {wire_name(f'{registered_prefix}_{tool}')}"
            )
        return rows


class ProcessProbe:
    """Starts the extension's process and asks it for its tools."""

    def __init__(self, manifest: ExtensionManifest, bundle: Path, *, timeout: float) -> None:
        self._manifest = manifest
        self._bundle = bundle
        self._timeout = timeout

    async def run(self) -> Probe:
        with tempfile.TemporaryDirectory(prefix="igni-doctor-") as scratch:
            work = Path(scratch)
            try:
                command, args, cwd = await self._command(work)
            except Exception as exc:  # noqa: BLE001 — said, not raised
                return Probe(error=str(exc))
            return await self._ask(command, args, cwd, work)

    async def _command(self, work: Path) -> tuple[str, list[str], Path]:
        manifest = self._manifest
        if manifest.dev is not None:
            cwd = self._bundle if manifest.dev.cwd is None else self._bundle / manifest.dev.cwd
            return manifest.dev.command[0], list(manifest.dev.command[1:]), cwd
        assert manifest.python is not None
        from ember_code.extensions.python_env import (
            PythonEnvironmentError,
            PythonEnvironments,
        )

        environments = PythonEnvironments(work / "environments")
        try:
            entry = await environments.ensure(manifest, self._bundle, development=True)
        except PythonEnvironmentError as exc:
            message = str(exc)
            if "igni-extension" in message or "igni_extension" in message:
                message += (
                    "\n    Until igni-extension is on PyPI, build its wheel from an igni "
                    "checkout and point pip at it: "
                    "PIP_FIND_LINKS=<directory with the wheel> igni ext doctor …"
                )
            raise RuntimeError(f"installing its package failed: {message}") from exc
        return str(entry), [], self._bundle

    async def _ask(self, command: str, args: list[str], cwd: Path, work: Path) -> Probe:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(self._manifest.dev.env) if self._manifest.dev is not None else {}
        env.setdefault("IGNI_EXTENSION_DATA", str(work / "data"))
        (work / "data").mkdir(exist_ok=True)
        params = StdioServerParameters(command=command, args=args, cwd=str(cwd), env=env)
        stderr_path = work / "stderr.log"

        async def round_trip() -> list[str]:
            with stderr_path.open("w") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        return [tool.name for tool in listed.tools]

        try:
            tools = await asyncio.wait_for(round_trip(), self._timeout)
        except TimeoutError:
            return Probe(
                error=f"it did not answer MCP's initialize and tools/list within "
                f"{self._timeout:g}s{self._stderr(stderr_path)}"
            )
        except FileNotFoundError:
            return Probe(error=f"its command {command!r} was not found{self._on_path(command)}")
        except BaseException as exc:  # noqa: BLE001 — anyio wraps a dead process in a group
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            return Probe(
                error=f"it exited before answering MCP ({self._reason(exc)})"
                f"{self._stderr(stderr_path)}"
            )
        return Probe(tools=tools)

    @staticmethod
    def _reason(exc: BaseException) -> str:
        # anyio reports a dead process as a group of one.
        while getattr(exc, "exceptions", None):
            exc = exc.exceptions[0]  # type: ignore[attr-defined]
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

    @staticmethod
    def _on_path(command: str) -> str:
        if os.sep in command:
            return ""
        return " on PATH; give its full path in dev.command"

    @staticmethod
    def _stderr(path: Path) -> str:
        try:
            lines = path.read_text(errors="replace").strip().splitlines()
        except OSError:
            return ""
        if not lines:
            return "; it wrote nothing to stderr"
        tail = "\n".join(f"      {line}" for line in lines[-STDERR_LINES:])
        return f"; the last of its stderr:\n{tail}"


__all__ = ["Findings", "NameCheck", "Probe", "ProcessProbe"]
