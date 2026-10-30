"""A headless run (``-m``, ``--pipe``) starts the extensions installed here.

An extension's backend logic runs in every mode, so its hooks apply to a
run with no window too. One that does not come up holds its fail-closed
hooks shut: a ``PreToolUse`` it would have checked is refused, with why.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ember_code.backend.headless_extensions import HeadlessExtensions
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.extension_gate import ExtensionHookGate, PreToolUseValue
from ember_code.extensions.registry import ExtensionState
from ember_code.extensions.store import ExtensionStore


class Mcp:
    """An MCP manager whose servers never start."""

    def __init__(self) -> None:
        self.configs: dict[str, Any] = {}
        self.disconnected = False

    async def connect(self, name: str) -> Any:
        return SimpleNamespace(ok=False, reason="its command was not found")

    async def disconnect_one(self, name: str) -> bool:
        return False

    async def disconnect_all(self) -> None:
        self.disconnected = True

    def get_client(self, name: str) -> Any:
        return None


class Session:
    def __init__(self, tmp: Path) -> None:
        self.settings = SimpleNamespace(
            storage=SimpleNamespace(data_dir=str(tmp / "data")),
            auth=SimpleNamespace(credentials_file=str(tmp / "credentials.json")),
            api_url=None,
        )
        self.project_dir = tmp / "project"
        self.project_dir.mkdir()
        self.session_id = "s-1"
        self.mcp_manager = Mcp()
        self.rebuilt = 0

    def reload_group_agents(self) -> bool:
        self.rebuilt += 1
        return True


def _install_guard(tmp: Path) -> None:
    bundle = tmp / "guard"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "id": "acme.guard",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "name": "Guard",
                "trust": "unsigned",
                "dev": {"command": ["no-such-command"]},
                "permissions": {"hooks": ["PreToolUse"]},
            }
        )
    )
    ExtensionStore().add(bundle)


async def test_nothing_installed_starts_nothing(tmp_path: Path) -> None:
    session = Session(tmp_path)
    extensions = HeadlessExtensions(session)

    await extensions.start()
    await extensions.stop()

    assert session.rebuilt == 0 and not session.mcp_manager.disconnected
    assert not ExtensionHookGate.hooked(HookEvent.PRE_TOOL_USE)


async def test_an_extension_that_did_not_start_refuses_what_it_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    _install_guard(tmp_path)
    session = Session(tmp_path)
    extensions = HeadlessExtensions(session)

    await extensions.start()
    try:
        entry = extensions.registry.get("acme.guard")
        assert entry is not None and entry.state is ExtensionState.FAILED
        assert session.rebuilt == 1

        verdict = await ExtensionHookGate.ask(
            PreToolUseValue(session_id="s-1", tool_name="run_shell_command"),
            HookEvent.PRE_TOOL_USE,
        )
    finally:
        await extensions.stop()

    assert not verdict.allowed
    assert "acme.guard" in verdict.reason and "not running" in verdict.reason
    assert session.mcp_manager.disconnected
    assert not ExtensionHookGate.hooked(HookEvent.PRE_TOOL_USE)
