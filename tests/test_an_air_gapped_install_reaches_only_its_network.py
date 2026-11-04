"""Under ``air_gapped: true``, every connection igni starts stays inside the network.

``test_an_air_gapped_install_makes_no_outbound_call.py`` covers the flows
the switch turns off outright. This file covers the three that still have
to work inside the customer's network and must not work outside it:
``igni ext install`` from a repository, an MCP server reached by URL, and
an ``http`` hook. All three ask one policy
(:class:`~ember_code.core.network_policy.NetworkPolicy`) before they
connect, so each test watches the seam the connection would go through —
the ``git`` subprocess, the MCP transport, the HTTP client — and asserts
nothing reached it, rather than trusting a return value that a failed
attempt would also produce.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from ember_code.cli.ext import ext
from ember_code.core.config.schemas import NetworkConfig, PluginsConfig
from ember_code.core.config.settings import Settings, load_settings
from ember_code.core.hooks.handlers.http import HttpHookHandler
from ember_code.core.hooks.schemas import HookDefinition, HookPayload
from ember_code.core.mcp.client import MCPClientManager
from ember_code.core.mcp.config import MCPPolicy, MCPServerConfig, MCPTransport
from ember_code.core.network_policy import NetworkPolicy

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"
PUBLIC = "https://github.com/acme/ext.git"
GAPPED = NetworkPolicy(air_gapped=True, internal_hosts=["git.acme.internal", "*.corp.example"])


def _write_user_config(text: str) -> None:
    (Path.home() / ".igni").mkdir(parents=True, exist_ok=True)
    (Path.home() / ".igni" / "config.yaml").write_text(text)


# ── The policy ──────────────────────────────────────────────────────


class TestThePolicy:
    def test_without_the_switch_everything_is_allowed(self):
        assert NetworkPolicy().allows(PUBLIC)
        assert NetworkPolicy().allows("git@github.com:acme/ext.git")

    @pytest.mark.parametrize(
        "url", ["file:///srv/mirror/ext.git", "/srv/mirror/ext", "./ext", r"C:\mirror\ext"]
    )
    def test_local_sources_are_inside(self, url):
        assert NetworkPolicy(air_gapped=True).allows(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8080/sse",
            "http://127.0.0.1/hook",
            "http://127.4.5.6:9000/sse",
            "http://[::1]:7000/sse",
        ],
    )
    def test_loopback_is_inside(self, url):
        assert NetworkPolicy(air_gapped=True).allows(url)

    def test_a_named_host_is_inside(self):
        assert GAPPED.allows("https://git.acme.internal/tools/ext.git")
        assert GAPPED.allows("git@git.acme.internal:tools/ext.git")
        assert GAPPED.allows("https://GIT.ACME.INTERNAL./tools/ext.git")

    def test_a_suffix_pattern_covers_hosts_under_it(self):
        assert GAPPED.allows("https://mcp.corp.example/sse")
        assert GAPPED.allows("https://a.b.corp.example/sse")

    def test_a_suffix_pattern_does_not_cover_the_apex_or_a_lookalike(self):
        assert not GAPPED.allows("https://corp.example/sse")
        assert not GAPPED.allows("https://evilcorp.example/sse")
        assert not GAPPED.allows("https://corp.example.attacker.com/sse")

    @pytest.mark.parametrize("url", [PUBLIC, "git@github.com:acme/ext.git", "http://10.0.0.5/hook"])
    def test_anything_else_is_outside(self, url):
        assert not GAPPED.allows(url)

    def test_the_hosts_igni_is_configured_to_use_are_inside(self):
        settings = Settings(
            air_gapped=True,
            api_url="https://api.acme.example:7777",
            portal_url="https://portal.acme.example",
            update_endpoint="https://mirror.acme.example/igni/{{target}}/{{current_version}}",
            plugins=PluginsConfig(marketplaces=["git@plugins.acme.example:m.git"]),
            network=NetworkConfig(internal_hosts=["*.svc.acme"]),
        )
        policy = NetworkPolicy.from_settings(settings)
        for host in ("api", "portal", "mirror", "plugins"):
            assert policy.allows(f"https://{host}.acme.example/x"), host
        assert policy.allows("https://vllm.svc.acme/v1")
        assert not policy.allows("https://acme.example/x")

    def test_a_stand_in_settings_object_is_not_air_gapped(self):
        assert NetworkPolicy.from_settings(object()).air_gapped is False


class TestWhereTheHostsComeFrom:
    def test_the_user_config_names_them(self, tmp_path):
        _write_user_config("network:\n  internal_hosts: [git.acme.internal]\n")
        settings = load_settings(project_dir=tmp_path)
        assert settings.network.internal_hosts == ["git.acme.internal"]

    def test_a_project_config_cannot_widen_them(self, tmp_path):
        _write_user_config("air_gapped: true\nnetwork:\n  internal_hosts: [git.acme.internal]\n")
        project = tmp_path / ".igni"
        project.mkdir()
        (project / "config.yaml").write_text("network:\n  internal_hosts: [github.com]\n")
        (project / "config.local.yaml").write_text("network:\n  internal_hosts: ['*.com']\n")

        settings = load_settings(project_dir=tmp_path)

        assert settings.network.internal_hosts == ["git.acme.internal"]
        assert not NetworkPolicy.from_settings(settings).allows(PUBLIC)

    def test_a_project_config_cannot_switch_air_gapped_mode_off(self, tmp_path):
        _write_user_config("air_gapped: true\n")
        project = tmp_path / ".igni"
        project.mkdir()
        (project / "config.yaml").write_text("air_gapped: false\n")

        assert load_settings(project_dir=tmp_path).air_gapped is True

    def test_a_project_config_cannot_add_a_host_through_another_setting(self, tmp_path):
        # api_url, portal_url, update_endpoint and plugins.marketplaces all
        # name hosts the policy treats as inside, so a repository may set none.
        _write_user_config("air_gapped: true\n")
        project = tmp_path / ".igni"
        project.mkdir()
        (project / "config.yaml").write_text(
            "api_url: https://evil.example\n"
            "portal_url: https://evil.example\n"
            "update_endpoint: https://evil.example/latest.json\n"
            "plugins:\n  marketplaces: [https://evil.example/m.git]\n"
        )

        settings = load_settings(project_dir=tmp_path)

        assert not NetworkPolicy.from_settings(settings).allows("https://evil.example/x")

    def test_plugins_reach_a_host_named_in_internal_hosts(self):
        from ember_code.core.plugins.source_policy import PluginSourcePolicy

        policy = PluginSourcePolicy(air_gapped=True, internal_hosts=["git.acme.internal"])

        assert policy.allows("https://git.acme.internal/plugins.git")
        assert not policy.allows("https://github.com/x/plugins.git")


# ── igni ext install ────────────────────────────────────────────────


@pytest.fixture
def dev_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")


@pytest.fixture
def mirror(tmp_path: Path) -> Path:
    """An extension in a repository on disk, the way a customer mirrors one."""
    root = tmp_path / "mirror" / "acme-ext"
    shutil.copytree(FIXTURE, root)
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "v1"], check=True)
    return root


class TestExtInstall:
    def test_a_repository_outside_the_network_is_refused_before_git_runs(self, dev_flag, tmp_path):
        _write_user_config("air_gapped: true\n")
        with patch("ember_code.core.plugins.git.subprocess.run") as git:
            done = CliRunner().invoke(ext, ["install", PUBLIC, "--root", str(tmp_path / "s")])
        git.assert_not_called()
        assert done.exit_code != 0
        assert "air-gapped" in done.output
        assert "network.internal_hosts" in done.output, "says how to allow it"
        assert "directory" in done.output, "and how to do without it"

    def test_a_file_url_still_installs(self, dev_flag, tmp_path, mirror):
        _write_user_config("air_gapped: true\n")
        done = CliRunner().invoke(
            ext, ["install", f"file://{mirror}", "--root", str(tmp_path / "s")]
        )
        assert done.exit_code == 0, done.output
        assert "installed" in done.output

    def test_without_the_switch_the_clone_is_attempted(self, dev_flag, tmp_path):
        failed = subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="no route")
        with patch("ember_code.core.plugins.git.subprocess.run", return_value=failed) as git:
            done = CliRunner().invoke(ext, ["install", PUBLIC, "--root", str(tmp_path / "s")])
        assert any(call.args[0][:2] == ["git", "clone"] for call in git.call_args_list)
        assert "air-gapped" not in done.output


# ── MCP servers reached by URL ──────────────────────────────────────


def _manager(configs: dict[str, MCPServerConfig], network: NetworkPolicy) -> MCPClientManager:
    with (
        patch("ember_code.core.mcp.client.MCPConfigLoader") as loader,
        patch("ember_code.core.mcp.client.MCPApprovalManager") as approval,
    ):
        loader.return_value.load.return_value = configs
        approval.return_value.check_approval.return_value = True
        return MCPClientManager(project_dir="/tmp/test", policy=MCPPolicy(), network_policy=network)


def _sse(url: str) -> MCPServerConfig:
    return MCPServerConfig(name="remote", type=MCPTransport.sse, url=url)


def _live_tools() -> MagicMock:
    tools = MagicMock()
    tools.functions = {"t": MagicMock()}
    tools.__aenter__ = AsyncMock(return_value=tools)
    return tools


class TestMCPServers:
    async def test_a_url_outside_the_network_is_refused_before_connecting(self):
        mgr = _manager({"remote": _sse("https://mcp.vendor.com/sse")}, GAPPED)
        with patch("ember_code.core.mcp.client._MCPTools") as transport:
            result = await mgr.connect("remote")
        transport.assert_not_called()
        mgr._approval.check_approval.assert_not_called()
        assert result.ok is False
        assert "air-gapped" in result.reason
        assert "network.internal_hosts" in result.reason

    async def test_a_url_on_an_internal_host_connects(self):
        mgr = _manager({"remote": _sse("https://mcp.corp.example/sse")}, GAPPED)
        with patch("ember_code.core.mcp.client._MCPTools", return_value=_live_tools()) as t:
            result = await mgr.connect("remote")
        assert result.ok is True
        assert t.call_args.kwargs["url"] == "https://mcp.corp.example/sse"

    async def test_a_stdio_server_is_left_to_the_network(self):
        stdio = MCPServerConfig(name="local", command="node", args=["s.js"])
        mgr = _manager({"local": stdio}, GAPPED)
        with patch.object(
            mgr._stdio_binding, "open", new_callable=AsyncMock, return_value=_live_tools()
        ) as opened:
            result = await mgr.connect("local")
        opened.assert_awaited_once()
        assert result.ok is True

    async def test_without_the_switch_any_url_connects(self):
        mgr = _manager({"remote": _sse("https://mcp.vendor.com/sse")}, NetworkPolicy())
        with patch("ember_code.core.mcp.client._MCPTools", return_value=_live_tools()):
            result = await mgr.connect("remote")
        assert result.ok is True


# ── http hooks ──────────────────────────────────────────────────────


def _hook(url: str) -> HookDefinition:
    return HookDefinition(type="http", url=url)


def _payload() -> HookPayload:
    return HookPayload(event="PreToolUse", tool_name="Bash")


def _ok_response() -> MagicMock:
    response = MagicMock(status_code=200)
    response.json.return_value = {"decision": "approve"}
    return response


def _http_client(response: MagicMock) -> MagicMock:
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestHttpHooks:
    async def test_a_url_outside_the_network_is_not_sent(self):
        handler = HttpHookHandler(network_policy=GAPPED)
        with patch("ember_code.core.hooks.handlers.http.httpx.AsyncClient") as client:
            result = await handler.run(
                _hook("https://hooks.vendor.com/x"), "PreToolUse", _payload()
            )
        client.assert_not_called()
        # Exactly what an unreachable webhook returns: non-blocking, no message.
        assert result == HttpHookHandler._non_blocking()

    async def test_a_url_on_an_internal_host_is_sent(self):
        handler = HttpHookHandler(network_policy=GAPPED)
        client = _http_client(_ok_response())
        with patch("ember_code.core.hooks.handlers.http.httpx.AsyncClient", return_value=client):
            await handler.run(_hook("https://audit.corp.example/hook"), "PreToolUse", _payload())
        assert client.post.await_args.args[0] == "https://audit.corp.example/hook"

    async def test_loopback_is_sent(self):
        handler = HttpHookHandler(network_policy=NetworkPolicy(air_gapped=True))
        client = _http_client(_ok_response())
        with patch("ember_code.core.hooks.handlers.http.httpx.AsyncClient", return_value=client):
            await handler.run(_hook("http://127.0.0.1:9000/hook"), "PreToolUse", _payload())
        client.post.assert_awaited_once()

    async def test_without_the_switch_any_url_is_sent(self):
        handler = HttpHookHandler()
        client = _http_client(_ok_response())
        with patch("ember_code.core.hooks.handlers.http.httpx.AsyncClient", return_value=client):
            await handler.run(_hook("https://hooks.vendor.com/x"), "PreToolUse", _payload())
        client.post.assert_awaited_once()
