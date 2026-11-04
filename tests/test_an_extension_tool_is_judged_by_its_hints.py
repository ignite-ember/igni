"""An extension's tools, in every permission mode, by what they say they are.

igni classifies its own tools by name. It cannot classify an
extension's that way — it does not know them — so every tool in
``permissions.tools`` states MCP's four hints, and every mode reads
them. The table in ``docs/EXTENSIBILITY.md`` (*What a tool says it is*)
is what these tests hold the modes to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ember_code.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
    ToolCategoryCatalog,
)
from ember_code.core.hooks.permission_pipeline import BypassGuardStage, ToolCallContext
from ember_code.core.mcp.hints import ToolHints, ToolHintsRegistry
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc

LOCAL_READ = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)
OUTSIDE_READ = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=True)
CONTAINED_WRITE = ToolHints(read_only=False, destructive=False, idempotent=False, open_world=False)
DESTRUCTIVE = ToolHints(read_only=False, destructive=True, idempotent=True, open_world=False)
OUTSIDE_WRITE = ToolHints(read_only=False, destructive=False, idempotent=False, open_world=True)

TOOLS = {
    "mcp_ext:acme.a_local_read": LOCAL_READ,
    "mcp_ext:acme.a_outside_read": OUTSIDE_READ,
    "mcp_ext:acme.a_contained_write": CONTAINED_WRITE,
    "mcp_ext:acme.a_destructive": DESTRUCTIVE,
    "mcp_ext:acme.a_outside_write": OUTSIDE_WRITE,
}

ALLOW, DENY, DEFER = PermissionDecision.ALLOW, PermissionDecision.DENY, PermissionDecision.DEFER

#: ``{mode: {tool: decision}}``. DEFER is "the mode leaves it to rules,
#: then to the pause": a tool that is not read-only pauses for the
#: person, a read-only one runs.
TABLE: dict[str, dict[str, PermissionDecision]] = {
    "default": {
        "local_read": DEFER,
        "outside_read": DEFER,
        "contained_write": DEFER,
        "destructive": DEFER,
        "outside_write": DEFER,
    },
    "acceptEdits": {
        "local_read": DEFER,
        "outside_read": DEFER,
        "contained_write": ALLOW,
        "destructive": DEFER,
        "outside_write": DEFER,
    },
    "plan": {
        "local_read": ALLOW,
        "outside_read": ALLOW,
        "contained_write": DENY,
        "destructive": DENY,
        "outside_write": DENY,
    },
    "bypassPermissions": {
        "local_read": ALLOW,
        "outside_read": ALLOW,
        "contained_write": ALLOW,
        "destructive": ALLOW,
        "outside_write": ALLOW,
    },
    "dontAsk": {
        "local_read": ALLOW,
        "outside_read": DENY,
        "contained_write": DENY,
        "destructive": DENY,
        "outside_write": DENY,
    },
}


@pytest.fixture
def registry() -> ToolHintsRegistry:
    registry = ToolHintsRegistry()
    registry.declare("ext:acme.a", TOOLS)
    return registry


def evaluator(mode: str, registry: ToolHintsRegistry, **rules: list[str]) -> PermissionEvaluator:
    built = PermissionEvaluator.from_strings(mode=mode, **rules)
    built.catalog = ToolCategoryCatalog(tool_hints=registry)
    return built


@pytest.mark.parametrize("mode", sorted(TABLE))
def test_each_mode_decides_by_the_hints(mode: str, registry: ToolHintsRegistry) -> None:
    decided = {
        name.removeprefix("mcp_ext:acme.a_"): evaluator(mode, registry).evaluate(name, {})
        for name in TOOLS
    }
    assert decided == TABLE[mode]


def test_plan_mode_says_why_a_change_waits(registry: ToolHintsRegistry) -> None:
    outcome = evaluator("plan", registry).evaluate_outcome("mcp_ext:acme.a_destructive", {})
    assert outcome.decision is DENY
    assert "not read-only" in outcome.reason
    assert "exit_plan_mode" in outcome.reason


def test_a_deny_rule_wins_over_what_a_tool_says(registry: ToolHintsRegistry) -> None:
    """Rules first, in every mode — a read-only tool can still be denied by name."""
    built = evaluator("plan", registry, deny=["mcp_ext:acme.a_local_read"])
    assert built.evaluate("mcp_ext:acme.a_local_read", {}) is DENY


def test_an_allow_rule_lets_a_strict_run_write(registry: ToolHintsRegistry) -> None:
    built = evaluator("dontAsk", registry, allow=["mcp_ext:acme.a_contained_write"])
    assert built.evaluate("mcp_ext:acme.a_contained_write", {}) is ALLOW


def test_igni_s_own_tools_are_still_classified_by_name(registry: ToolHintsRegistry) -> None:
    """A tool with no hints is igni's, and plan mode treats it as it did."""
    built = evaluator("plan", registry)
    assert built.evaluate("edit_file", {"file_path": "a.py"}) is DENY
    assert built.evaluate("web_search", {"query": "x"}) is ALLOW


def test_a_tool_cannot_read_and_destroy() -> None:
    with pytest.raises(ValueError):
        ToolHints(read_only=True, destructive=True, idempotent=True, open_world=False)


# ── Bypass's last check ───────────────────────────────────────────


class RecordingGuard:
    def __init__(self) -> None:
        self.checked: list[str] = []

    async def check(self, tool_name: str, args: dict[str, Any], agent_path: list[str]) -> None:
        self.checked.append(tool_name)
        return None


@pytest.mark.asyncio
async def test_bypass_waves_through_a_local_read_and_checks_the_rest(
    registry: ToolHintsRegistry,
) -> None:
    """As it does igni's own: local reads pass, everything else is looked at."""
    guard = RecordingGuard()
    stage = BypassGuardStage(guard, tool_hints=registry)
    for name in TOOLS:
        await stage.evaluate(ToolCallContext(name=name, func=None, args={}, agent=None))
    assert sorted(guard.checked) == sorted(
        name for name in TOOLS if name != "mcp_ext:acme.a_local_read"
    )


# ── What the installer does with them ─────────────────────────────


class Function:
    def __init__(self) -> None:
        self.requires_confirmation = False


class HintedMcp:
    """An MCP manager whose server offers two tools and reports their hints."""

    def __init__(self, reported: dict[str, Any]) -> None:
        self.configs: dict[str, Any] = {}
        self.functions = {
            "mcp_ext:acme.a_lookup": Function(),
            "mcp_ext:acme.a_purge": Function(),
        }
        self._reported = reported

    async def connect(self, name: str) -> Any:
        return type("Ok", (), {"ok": True, "reason": ""})()

    async def disconnect_one(self, name: str) -> None:
        self.configs.pop(name, None)

    def get_client(self, name: str) -> None:
        return None

    def get_tools(self, name: str) -> list[str]:
        return list(self.functions)

    def require_confirmation(self, server: str, tools: set[str]) -> None:
        for tool in tools:
            self.functions[tool].requires_confirmation = True

    async def tool_annotations(self, name: str) -> dict[str, Any]:
        return self._reported


def hints_json(hints: ToolHints) -> dict[str, bool]:
    return hints.as_annotations()


def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "acme.a"
    root.mkdir()
    manifest = {
        "id": "acme.a",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": ["python", "server.py"]},
        "permissions": {
            "tools": [
                {"name": "acme.a.lookup", "annotations": hints_json(LOCAL_READ)},
                {"name": "acme.a.purge", "annotations": hints_json(DESTRUCTIVE)},
            ]
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


async def install(
    tmp_path: Path, reported: dict[str, Any]
) -> tuple[Any, HintedMcp, ToolHintsRegistry]:
    registry = ExtensionRegistry()
    mcp = HintedMcp(reported)
    hints = ToolHintsRegistry()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=mcp,
        allow_development=True,
        tool_hints=hints,
    )
    entry = await installer.install(bundle(tmp_path))
    assert entry.state is ExtensionState.READY, entry.reason
    return entry, mcp, hints


@pytest.mark.asyncio
async def test_the_manifest_s_hints_reach_the_permission_modes(tmp_path: Path) -> None:
    entry, mcp, hints = await install(tmp_path, reported={})

    assert hints.get("mcp_ext:acme.a_lookup") == LOCAL_READ
    assert hints.get("mcp_ext:acme.a_purge") == DESTRUCTIVE
    # A tool that is not read-only pauses, so the mode can decide.
    assert mcp.functions["mcp_ext:acme.a_purge"].requires_confirmation is True
    assert mcp.functions["mcp_ext:acme.a_lookup"].requires_confirmation is False
    # A server that says nothing about its hints is not a problem.
    assert entry.problems == []


@pytest.mark.asyncio
async def test_a_server_that_disagrees_is_not_believed_and_is_reported(tmp_path: Path) -> None:
    """The manifest was approved; the server's own claim is shown, not used."""
    entry, _mcp, hints = await install(
        tmp_path,
        reported={
            "lookup": hints_json(LOCAL_READ),
            # The server calls its purge a harmless read.
            "purge": hints_json(LOCAL_READ),
        },
    )

    assert hints.get("mcp_ext:acme.a_purge") == DESTRUCTIVE
    assert len(entry.problems) == 1
    assert "purge" in entry.problems[0]
    assert "the manifest's hints apply" in entry.problems[0]


@pytest.mark.asyncio
async def test_uninstalling_forgets_its_hints(tmp_path: Path) -> None:
    registry = ExtensionRegistry()
    hints = ToolHintsRegistry()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=HintedMcp({}),
        allow_development=True,
        tool_hints=hints,
    )
    await installer.install(bundle(tmp_path))
    await installer.uninstall("acme.a")
    assert hints.get("mcp_ext:acme.a_lookup") is None
