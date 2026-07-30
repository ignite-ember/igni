"""OS-native folder picker dialogs.

Extracted from :mod:`ember_code.backend.rpc_router` so the RPC router
stays focused on dispatch and this module owns everything about
"pop a native folder-selection dialog on the current OS". Each
platform is its own subclass — the ``if sys.platform`` chain that
used to live in ``_picker_for_platform`` becomes a classmethod on
the base class that returns the right subclass instance.
"""

from __future__ import annotations

import asyncio
import sys
from abc import ABC, abstractmethod

from ember_code.backend.schemas_rpc import PickDirResult


class NativeFolderPicker(ABC):
    """Base class for OS-native folder-picker dialogs.

    Each subclass drives one platform's dialog. The base class owns
    ``_run_cmd`` — the subprocess-communicate boilerplate every
    subclass needs — so subclasses only implement :meth:`pick`.
    """

    def __init__(self, start_dir: str) -> None:
        self._start_dir = start_dir

    @classmethod
    def for_platform(cls, start_dir: str) -> NativeFolderPicker:
        """Return the concrete picker for the running platform.

        Owns the ``sys.platform`` switch that used to be a module-
        level ``_picker_for_platform`` free function — callers just
        ask for the picker without knowing the platform matrix.
        """
        if sys.platform == "darwin":
            return _DarwinPicker(start_dir)
        if sys.platform.startswith("linux"):
            return _LinuxPicker(start_dir)
        if sys.platform == "win32":
            return _WindowsPicker(start_dir)
        return _UnsupportedPicker(start_dir)

    @abstractmethod
    async def pick(self) -> PickDirResult:
        """Show the dialog. Return the chosen path or a cancel /
        error result — the wire shape is the same on every branch."""

    async def _run_cmd(self, cmd: list[str]) -> tuple[int | None, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return proc.returncode, out.decode(errors="replace").strip()


class _DarwinPicker(NativeFolderPicker):
    async def pick(self) -> PickDirResult:
        script = 'choose folder with prompt "Lock session to a project folder"'
        if self._start_dir:
            escaped = self._start_dir.replace("\\", "\\\\").replace('"', '\\"')
            script += f' default location POSIX file "{escaped}"'
        rc, out = await self._run_cmd(["osascript", "-e", f"POSIX path of ({script})"])
        if rc == 0 and out:
            return PickDirResult(path=out.rstrip("/") or "/", cancelled=False, error="")
        # osascript exits non-zero on user cancel.
        return PickDirResult(path="", cancelled=True, error="")


class _LinuxPicker(NativeFolderPicker):
    async def pick(self) -> PickDirResult:
        zenity_cmd = ["zenity", "--file-selection", "--directory"]
        if self._start_dir:
            zenity_cmd.append(f"--filename={self._start_dir}/")
        kdialog_cmd = ["kdialog", "--getexistingdirectory"]
        if self._start_dir:
            kdialog_cmd.append(self._start_dir)
        for cmd in (zenity_cmd, kdialog_cmd):
            try:
                rc, out = await self._run_cmd(cmd)
            except FileNotFoundError:
                continue
            if rc == 0 and out:
                return PickDirResult(path=out, cancelled=False, error="")
            return PickDirResult(path="", cancelled=True, error="")
        return PickDirResult(path="", cancelled=False, error="no native dialog available")


class _WindowsPicker(NativeFolderPicker):
    async def pick(self) -> PickDirResult:
        selected = (
            f"$d.SelectedPath = '{self._start_dir}'; "
            if self._start_dir and "'" not in self._start_dir
            else ""
        )
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"{selected}"
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
        )
        rc, out = await self._run_cmd(["powershell", "-NoProfile", "-Command", ps])
        if rc == 0 and out:
            return PickDirResult(path=out, cancelled=False, error="")
        return PickDirResult(path="", cancelled=True, error="")


class _UnsupportedPicker(NativeFolderPicker):
    async def pick(self) -> PickDirResult:
        return PickDirResult(
            path="", cancelled=False, error=f"unsupported platform: {sys.platform}"
        )
