"""An extension's process runs inside its grants, not with the person's access.

Its ``permissions.egress`` and ``permissions.workspace`` were enforced on
its pages and on what igni told it; the process itself could open any
socket and read any file the person can. Now it starts in an OS sandbox
(:mod:`ember_code.extensions.sandbox`): Seatbelt on macOS, bubblewrap on
Linux, with an egress proxy igni runs as the only way out.

The macOS tests run the real sandbox and skip elsewhere; the Linux ones
run real bubblewrap and skip where ``bwrap`` is missing or cannot make a
namespace. What does not depend on the OS — the proxy, the plan, the
settings — runs everywhere.
"""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ember_code.core.mcp.config import MCPServerConfig, MCPTransport
from ember_code.core.network_policy import NetworkPolicy
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.sandbox import (
    NO_PROXY,
    REQUIRE_SANDBOX_ENV,
    EgressProxy,
    ExtensionSandbox,
    HostAllowList,
    SandboxPlan,
    SandboxRefused,
    SandboxSettings,
)
from ember_code.extensions.sandbox.bubblewrap import PROXY_PORT, Bubblewrap
from ember_code.extensions.sandbox.seatbelt import Seatbelt

pytestmark = pytest.mark.extension_sandbox

ON_MACOS = sys.platform == "darwin" and Seatbelt.found()
ON_LINUX = sys.platform.startswith("linux")


def manifest(**permissions) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": "acme.probe",
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": "Probe",
            "trust": "unsigned",
            "dev": {"command": ["python", "probe.py"]},
            "permissions": permissions,
        }
    )


# ── A local upstream, standing in for a host on the internet ──────────


class Upstream:
    """An HTTP server on loopback that answers ``hello from <host>``.

    The proxy's connector maps every name to it, so "reaching
    ``allowed.test``" needs no DNS and no internet, and what the proxy
    decided is the only thing that varies.
    """

    def __init__(self) -> None:
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._answer, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        if head.startswith(b"PING"):
            writer.write(b"PONG")
        else:
            host = next(
                (
                    line.split(b":", 1)[1].strip()
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"host:")
                ),
                b"?",
            )
            body = b"hello from " + host
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                % (len(body), body)
            )
        await writer.drain()
        writer.close()

    async def connect(self, host: str, port: int):
        return await asyncio.open_connection("127.0.0.1", self.port)


@pytest.fixture
async def upstream():
    server = Upstream()
    await server.start()
    yield server
    await server.stop()


async def http_via(proxy_port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    return data


# ── The proxy: the only way out, deciding by host and port ────────────


async def test_the_proxy_tunnels_to_an_allowed_host(upstream):
    proxy = EgressProxy("acme.probe", HostAllowList(["allowed.test"]), connect=upstream.connect)
    port = await proxy.start_tcp()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT allowed.test:443 HTTP/1.1\r\nHost: allowed.test:443\r\n\r\n")
        await writer.drain()
        assert b" 200 " in await reader.readuntil(b"\r\n\r\n")
        writer.write(b"PING\r\n\r\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 10) == b"PONG"
        writer.close()
    finally:
        await proxy.stop()


async def test_the_proxy_refuses_a_host_nobody_declared(upstream):
    proxy = EgressProxy("acme.probe", HostAllowList(["allowed.test"]), connect=upstream.connect)
    port = await proxy.start_tcp()
    try:
        answer = await http_via(port, b"CONNECT evil.test:443 HTTP/1.1\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 403")
        assert b"acme.probe may not reach evil.test:443" in answer
        # The declared host on another port is another destination.
        answer = await http_via(port, b"CONNECT allowed.test:22 HTTP/1.1\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 403")
        assert proxy.refused == ["evil.test:443", "allowed.test:22"]
    finally:
        await proxy.stop()


async def test_plain_http_is_forwarded_only_to_a_declared_host_and_port(upstream):
    proxy = EgressProxy(
        "acme.probe", HostAllowList(["allowed.test:8080"]), connect=upstream.connect
    )
    port = await proxy.start_tcp()
    try:
        ok = await http_via(
            port, b"GET http://allowed.test:8080/x HTTP/1.1\r\nHost: allowed.test:8080\r\n\r\n"
        )
        assert ok.startswith(b"HTTP/1.1 200") and ok.endswith(b"hello from allowed.test:8080")
        refused = await http_via(port, b"GET http://evil.test/ HTTP/1.1\r\nHost: evil.test\r\n\r\n")
        assert refused.startswith(b"HTTP/1.1 403")
    finally:
        await proxy.stop()


async def test_socks5_is_held_to_the_same_list(upstream):
    proxy = EgressProxy("acme.probe", HostAllowList(["allowed.test"]), connect=upstream.connect)
    port = await proxy.start_tcp()

    async def socks(host: bytes) -> int:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x01\x00")
        await reader.readexactly(2)
        writer.write(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (443).to_bytes(2, "big"))
        await writer.drain()
        code = (await reader.readexactly(10))[1]
        writer.close()
        return code

    try:
        assert await socks(b"allowed.test") == 0
        assert await socks(b"evil.test") == 2  # not allowed by ruleset
    finally:
        await proxy.stop()


async def test_a_proxy_with_a_token_refuses_a_client_without_it(upstream):
    proxy = EgressProxy(
        "acme.probe", HostAllowList(["allowed.test"]), connect=upstream.connect, token="s3cret"
    )
    port = await proxy.start_tcp()
    try:
        # Another extension that found the port, without this one's token.
        answer = await http_via(port, b"CONNECT allowed.test:443 HTTP/1.1\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 407")
        assert b'Proxy-Authenticate: Basic realm="igni"' in answer
        # The process itself, with the URL it was given.
        assert proxy.url() == f"http://igni:s3cret@127.0.0.1:{port}"
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"CONNECT allowed.test:443 HTTP/1.1\r\n"
            b"Proxy-Authorization: Basic aWduaTpzM2NyZXQ=\r\n\r\n"
        )
        assert b" 200 " in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        writer.close()
        # SOCKS5 without a password is not offered a way in.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x01\x00")
        assert await reader.readexactly(2) == b"\x05\xff"
        writer.close()
        # With it, it is.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x01\x02")
        assert await reader.readexactly(2) == b"\x05\x02"
        writer.write(b"\x01\x04igni\x06s3cret")
        assert await reader.readexactly(2) == b"\x01\x00"
        writer.close()
    finally:
        await proxy.stop()


# ── The plan: what one extension gets ─────────────────────────────────


def plan_for(tmp_path: Path, m: ExtensionManifest, **kwargs) -> SandboxPlan:
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    defaults = dict(
        command=sys.executable,
        env={},
        cwd=None,
        bundle=tmp_path,
        data_dir=tmp_path / "data",
        project_dir=None,
        service_url=None,
        development=False,
        network=NetworkPolicy(),
        run_dir=run,
    )
    defaults.update(kwargs)
    return SandboxPlan.build(m, **defaults)


def test_its_own_service_and_its_egress_are_what_it_may_reach(tmp_path):
    m = manifest(egress=["api.acme.com", "files.acme.com:8443"])
    plan = plan_for(tmp_path, m, service_url="https://svc.acme.com")
    assert plan.hosts.entries == [
        "api.acme.com:443",
        "files.acme.com:8443",
        "svc.acme.com:443",
    ]


def test_a_plain_http_service_outside_development_is_named_with_the_fix(tmp_path):
    """The tutorial's service is http://tickets.localhost:8201. Outside
    development the process may not reach it, and the proxy's refusal said
    it was "not its service" — it is; it is plain http. The plan names it,
    so the launcher can say what to do."""
    m = manifest()
    plan = plan_for(tmp_path, m, service_url="http://tickets.localhost:8201")
    assert plan.hosts.entries == []
    assert plan.plain_http_service == "http://tickets.localhost:8201"
    dev = plan_for(tmp_path, m, service_url="http://tickets.localhost:8201", development=True)
    assert dev.hosts.entries == ["tickets.localhost:8201"]
    assert dev.plain_http_service is None


def test_an_air_gap_narrows_the_list_to_hosts_inside_the_network(tmp_path):
    m = manifest(egress=["git.acme.internal", "api.github.com"])
    air_gapped = NetworkPolicy(air_gapped=True, internal_hosts=["*.acme.internal"])
    plan = plan_for(tmp_path, m, service_url="https://svc.acme.internal", network=air_gapped)
    assert plan.hosts.entries == ["git.acme.internal:443", "svc.acme.internal:443"]
    assert plan.withheld == ["api.github.com"]
    # And not air-gapped, the same manifest reaches all three.
    assert len(plan_for(tmp_path, m, service_url="https://svc.acme.internal").hosts.entries) == 3


async def test_an_air_gapped_proxy_refuses_a_granted_host_outside_the_network(tmp_path, upstream):
    m = manifest(egress=["git.acme.internal", "api.github.com"])
    air_gapped = NetworkPolicy(air_gapped=True, internal_hosts=["*.acme.internal"])
    plan = plan_for(tmp_path, m, network=air_gapped)
    proxy = EgressProxy(m.id, plan.hosts, connect=upstream.connect)
    port = await proxy.start_tcp()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT git.acme.internal:443 HTTP/1.1\r\n\r\n")
        assert b" 200 " in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        writer.close()
        assert (await http_via(port, b"CONNECT api.github.com:443 HTTP/1.1\r\n\r\n")).startswith(
            b"HTTP/1.1 403"
        )
    finally:
        await proxy.stop()


def test_plain_http_to_its_service_is_only_for_development(tmp_path):
    m = manifest()
    assert plan_for(tmp_path, m, service_url="http://devbox:8000").hosts.entries == []
    dev = plan_for(tmp_path, m, service_url="http://devbox:8000", development=True)
    assert dev.hosts.entries == ["devbox:8000"]


def test_the_workspace_is_readable_only_when_granted_and_writable_only_for_write(tmp_path):
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    none = plan_for(tmp_path, manifest(), project_dir=ws)
    assert ws not in none.read and ws not in none.write
    read = plan_for(tmp_path, manifest(workspace="read"), project_dir=ws)
    assert ws in read.read and ws not in read.write
    write = plan_for(tmp_path, manifest(workspace="write"), project_dir=ws)
    assert ws in write.write


def test_a_workspace_glob_is_granted_as_its_directory_and_never_beyond(tmp_path):
    ws = (tmp_path / "ws").resolve()
    (ws / "docs").mkdir(parents=True)
    paths = SandboxPlan.workspace_paths(ws, ["docs/**/*.md", "../outside/*", "*.txt"])
    assert paths == [ws / "docs", ws]


def test_igni_s_loopback_ports_are_noted_for_a_namespace_to_bridge(tmp_path):
    env = {
        "IGNI_HOST_URL": "ws://127.0.0.1:41001/v1/host",
        "IGNI_SERVICE_TOKEN_URL": "http://127.0.0.1:41002/token",
        "IGNI_SERVICE_URL": "https://svc.acme.com",
    }
    plan = plan_for(tmp_path, manifest(), env=env)
    assert plan.loopback_ports == [41001, 41002]


# ── The settings: who may turn it off, and who may require it ─────────


def write_yaml(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def test_on_by_default_and_not_required(tmp_path):
    settings = SandboxSettings.load(managed_path=lambda: None, environ={})
    assert settings.enabled and not settings.required


def test_sandbox_false_in_a_project_config_is_ignored(tmp_path, monkeypatch):
    project = tmp_path / "project"
    write_yaml(project / ".igni" / "config.yaml", "extensions:\n  sandbox: false\n")
    write_yaml(project / ".igni" / "config.local.yaml", "extensions:\n  sandbox: false\n")
    monkeypatch.chdir(project)
    settings = SandboxSettings.load(managed_path=lambda: None, environ={})
    assert settings.enabled
    # And through igni's own settings loader, a project still cannot.
    sandbox = ExtensionSandbox(settings=None, network=NetworkPolicy())
    assert sandbox.settings.enabled


def test_sandbox_false_from_the_user_or_managed_settings_turns_it_off(tmp_path):
    user = write_yaml(tmp_path / "user.yaml", "extensions:\n  sandbox: false\n")
    by_user = SandboxSettings.load(managed_path=lambda: None, user_file=user, environ={})
    assert not by_user.enabled and by_user.disabled_by == "user"
    managed = write_yaml(tmp_path / "managed.yaml", "extensions:\n  sandbox: false\n")
    by_admin = SandboxSettings.load(managed_path=lambda: managed, environ={})
    assert not by_admin.enabled and by_admin.disabled_by == "managed"


def test_the_users_own_home_config_is_where_user_settings_come_from(tmp_path):
    write_yaml(Path.home() / ".igni" / "config.yaml", "extensions:\n  sandbox: false\n")
    assert not SandboxSettings.load(managed_path=lambda: None, environ={}).enabled


def test_require_sandbox_comes_from_managed_or_the_env_and_never_the_user(tmp_path):
    user = write_yaml(tmp_path / "user.yaml", "extensions:\n  require_sandbox: true\n")
    assert not SandboxSettings.load(managed_path=lambda: None, user_file=user, environ={}).required
    managed = write_yaml(tmp_path / "managed.yaml", "extensions:\n  require_sandbox: true\n")
    assert SandboxSettings.load(managed_path=lambda: managed, environ={}).required
    assert SandboxSettings.load(
        managed_path=lambda: None, environ={REQUIRE_SANDBOX_ENV: "1"}
    ).required


def group_pack(extensions: dict):
    from datetime import UTC, datetime

    from ember_code.core.config.group_policy import GroupPolicyEntry, GroupPolicyPack

    return GroupPolicyPack(
        group_id="g1",
        group_name="Engineering",
        fetched_at=datetime.now(UTC),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="sandbox",
                content=json.dumps({"extensions": extensions}),
                content_type="json",
            )
        ],
    )


def test_a_verified_group_policy_may_require_the_sandbox_but_not_turn_it_off(tmp_path):
    required = SandboxSettings.load(
        managed_path=lambda: None,
        environ={},
        group=lambda: group_pack({"require_sandbox": True}),
    )
    assert required.required
    off = SandboxSettings.load(
        managed_path=lambda: None, environ={}, group=lambda: group_pack({"sandbox": False})
    )
    assert off.enabled


def test_a_group_policy_that_does_not_verify_requires_nothing(tmp_path):
    from ember_code.core.config.group_policy import GroupPolicyUnavailable

    def unverified():
        raise GroupPolicyUnavailable("the pack on disk does not verify")

    assert not SandboxSettings.load(
        managed_path=lambda: None, environ={}, group=unverified
    ).required
    assert not SandboxSettings.load(
        managed_path=lambda: None, environ={}, group=lambda: None
    ).required


def test_the_backend_reads_the_group_policy_through_the_verifying_fetcher(monkeypatch):
    from ember_code.core.config import group_policy

    seen = []

    class Fetcher:
        @classmethod
        def for_settings(cls, settings):
            seen.append(settings)
            return lambda: group_pack({"require_sandbox": True})

    monkeypatch.setattr(group_policy, "CachedSettingsFetcher", Fetcher)
    settings = object()
    sandbox = ExtensionSandbox.for_settings(settings)
    assert seen == [settings]
    assert sandbox.settings.required


def test_a_required_sandbox_overrides_the_users_off_switch(tmp_path):
    user = write_yaml(tmp_path / "user.yaml", "extensions:\n  sandbox: false\n")
    settings = SandboxSettings.load(
        managed_path=lambda: None, user_file=user, environ={REQUIRE_SANDBOX_ENV: "true"}
    )
    assert settings.enabled and settings.required


# ── No sandbox on this machine ────────────────────────────────────────


def stdio_config(
    command: str = "/bin/echo", args: list[str] | None = None, **kw
) -> MCPServerConfig:
    return MCPServerConfig(
        name="ext:acme.probe",
        type=MCPTransport.stdio,
        command=command,
        args=args or [],
        env=kw.pop("env", {}),
        cwd=kw.pop("cwd", ""),
        source="extension",
    )


async def test_require_sandbox_refuses_to_start_where_there_is_none(tmp_path):
    sandbox = ExtensionSandbox(settings=SandboxSettings(required=True), platform="win32")
    with pytest.raises(SandboxRefused, match="require_sandbox.*not sandboxed on Windows"):
        await sandbox.confine(
            manifest(),
            stdio_config(),
            bundle=tmp_path,
            data_dir=None,
            project_dir=None,
            service_url=None,
            development=False,
        )


async def test_without_require_it_runs_unsandboxed_and_says_so(tmp_path, caplog):
    sandbox = ExtensionSandbox(settings=SandboxSettings(), platform="win32")
    config = stdio_config()
    out = await sandbox.confine(
        manifest(),
        config,
        bundle=tmp_path,
        data_dir=None,
        project_dir=None,
        service_url=None,
        development=False,
    )
    assert out == config
    status = sandbox.status("acme.probe")
    assert status is not None and not status.confined
    assert (
        status.describe() == "not sandboxed: extension processes are not sandboxed on Windows yet"
    )
    assert "runs UNSANDBOXED" in caplog.text


class _Mcp:
    """Enough of the MCP manager for an install that never gets to start."""

    def __init__(self) -> None:
        self.configs: dict[str, MCPServerConfig] = {}

    async def connect(self, name):  # pragma: no cover — refused before
        raise AssertionError("a refused extension must not be started")

    async def disconnect_one(self, name):
        return None


async def test_the_installer_records_a_refusal_as_FAILED_with_the_reason(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(manifest().model_dump_json())
    registry = ExtensionRegistry()
    installer = ExtensionInstaller(
        registry=registry,
        rpc=ExtensionRpc(registry),
        mcp=_Mcp(),
        allow_development=True,
        sandbox=ExtensionSandbox(settings=SandboxSettings(required=True), platform="win32"),
    )
    entry = await installer.install(bundle)
    assert entry.state is ExtensionState.FAILED
    assert "require_sandbox" in entry.reason


async def test_an_endpoint_has_no_process_here_and_is_left_alone(tmp_path):
    sandbox = ExtensionSandbox(settings=SandboxSettings(required=True), platform="win32")
    config = MCPServerConfig(
        name="ext:x", type=MCPTransport.sse, url="https://x.test/sse", source="extension"
    )
    assert (
        await sandbox.confine(
            manifest(),
            config,
            bundle=tmp_path,
            data_dir=None,
            project_dir=None,
            service_url=None,
            development=False,
        )
        is config
    )


# ── macOS: the real Seatbelt ──────────────────────────────────────────

#: Run inside the sandbox; prints what it could and could not do.
PROBE = r"""
import errno, json, os, socket, sys, urllib.request, urllib.error
from pathlib import Path

out = {}
real_home = Path(sys.argv[1]); workspace = Path(sys.argv[2])

def attempt(name, fn):
    try:
        out[name] = ["ok", fn()]
    except urllib.error.HTTPError as exc:
        out[name] = ["http", exc.code]
    except OSError as exc:
        out[name] = ["oserror", exc.errno]

def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read().decode()

attempt("allowed_via_proxy", lambda: get("http://allowed.test:8080/hello"))
attempt("refused_via_proxy", lambda: get("http://evil.test:8080/"))

def direct(ip, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(5)
    try:
        s.connect((ip, port)); return "connected"
    finally:
        s.close()

attempt("direct_public_ip", lambda: direct("93.184.216.34", 443))
attempt("direct_other_ip", lambda: direct("1.1.1.1", 80))
attempt("ip_via_proxy", lambda: get("http://93.184.216.34/"))
attempt("dns", lambda: socket.getaddrinfo("example.com", 443) and "resolved")
for key in ("PROBE_HOST_CHANNEL_PORT", "PROBE_IGNI_LISTENER_PORT"):
    if os.environ.get(key):
        attempt(key.lower(), lambda: direct("127.0.0.1", int(os.environ[key])))

name = f"igni-sandbox-probe-{os.getpid()}"
attempt("write_workspace", lambda: (workspace / name).write_text("x") and "written")
attempt("write_home", lambda: (real_home / name).write_text("x") and "written")
attempt("write_ssh", lambda: (real_home / ".ssh" / name).write_text("x") and "written")
attempt("list_ssh", lambda: os.listdir(real_home / ".ssh"))
attempt("list_home", lambda: os.listdir(real_home))
attempt("read_igni_credentials", lambda: (real_home / ".igni" / "credentials.json").read_text()[:1])
attempt("write_data", lambda: (Path(os.environ["IGNI_EXTENSION_DATA"]) / name).write_text("x") and "written")
attempt("write_tmp", lambda: (Path(os.environ["TMPDIR"]) / name).write_text("x") and "written")
out["env"] = {k: os.environ.get(k, "") for k in ("HOME", "HTTP_PROXY", "NO_PROXY", "IGNI_SANDBOX")}
print(json.dumps(out))
"""


async def run_confined(
    sandbox: ExtensionSandbox,
    m: ExtensionManifest,
    tmp_path: Path,
    *args,
    env: dict[str, str] | None = None,
):
    bundle = (tmp_path / "bundle").resolve()
    bundle.mkdir(exist_ok=True)
    (bundle / "probe.py").write_text(PROBE)
    ws = (tmp_path / "ws").resolve()
    ws.mkdir(exist_ok=True)
    data = (tmp_path / "data").resolve()
    data.mkdir(exist_ok=True)
    config = stdio_config(
        sys.executable,
        [str(bundle / "probe.py"), *args],
        env={"IGNI_EXTENSION_DATA": str(data), **(env or {})},
        cwd=str(bundle),
    )
    wrapped = await sandbox.confine(
        m,
        config,
        bundle=bundle,
        data_dir=data,
        project_dir=ws,
        service_url=None,
        development=False,
    )
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **(wrapped.env or {})}
    proc = await asyncio.create_subprocess_exec(
        wrapped.command,
        *wrapped.args,
        cwd=wrapped.cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode == 0, err.decode()
    return wrapped, ws, json.loads(out.decode().strip().splitlines()[-1])


@pytest.mark.skipif(not ON_MACOS, reason="Seatbelt is macOS")
async def test_on_macos_the_process_is_held_to_its_grants(tmp_path, upstream):
    sandbox = ExtensionSandbox(
        settings=SandboxSettings(), network=NetworkPolicy(), connect=upstream.connect
    )
    if (await sandbox.mechanism())[0] != "seatbelt":
        pytest.skip(f"sandbox-exec cannot confine a process here: {await sandbox.mechanism()}")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    # Two listeners in this process, which stands in for igni: one the
    # process is told about (as the host channel is), one it is not (as
    # igni's backend socket is not).
    host_channel = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    igni_listener = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    host_port = host_channel.sockets[0].getsockname()[1]
    igni_port = igni_listener.sockets[0].getsockname()[1]
    try:
        wrapped, ws, seen = await run_confined(
            sandbox,
            manifest(workspace="write", egress=["allowed.test:8080"]),
            tmp_path,
            str(home),
            str(tmp_path / "ws"),
            env={
                "IGNI_HOST_URL": f"ws://127.0.0.1:{host_port}/v1/host",
                "PROBE_HOST_CHANNEL_PORT": str(host_port),
                "PROBE_IGNI_LISTENER_PORT": str(igni_port),
            },
        )
    finally:
        host_channel.close()
        igni_listener.close()
        await sandbox.close()

    assert wrapped.command == "/usr/bin/sandbox-exec"
    # Network: the declared host through the proxy, nothing else at all.
    assert seen["allowed_via_proxy"] == ["ok", "hello from allowed.test:8080"]
    assert seen["refused_via_proxy"] == ["http", 403]
    assert seen["direct_public_ip"] == ["oserror", 1]  # EPERM: the kernel said no
    assert seen["direct_other_ip"] == ["oserror", 1]
    assert seen["ip_via_proxy"] == ["http", 403]
    assert seen["dns"][0] == "oserror"
    # Files: its workspace grant, its data, its temp — not the person's home.
    assert seen["write_workspace"] == ["ok", "written"]
    assert seen["write_data"] == ["ok", "written"]
    assert seen["write_tmp"] == ["ok", "written"]
    assert seen["write_home"] == ["oserror", 1]
    assert seen["write_ssh"][0] == "oserror"
    assert seen["list_home"] == ["oserror", 1]
    if (home / ".ssh").is_dir():
        assert seen["list_ssh"] == ["oserror", 1]
    if (home / ".igni" / "credentials.json").is_file():
        assert seen["read_igni_credentials"] == ["oserror", 1]
    # What it was told.
    # igni's own loopback: what it was granted, and nothing else of igni's.
    assert seen["probe_host_channel_port"] == ["ok", "connected"]
    assert seen["probe_igni_listener_port"] == ["oserror", 1]
    assert seen["env"]["HTTP_PROXY"].startswith("http://igni:")
    assert seen["env"]["NO_PROXY"] == NO_PROXY
    assert seen["env"]["IGNI_SANDBOX"] == "seatbelt"
    assert seen["env"]["HOME"] != str(home)
    assert not any(p.name.startswith("igni-sandbox-probe") for p in home.iterdir())


@pytest.mark.skipif(not ON_MACOS, reason="Seatbelt is macOS")
async def test_on_macos_a_read_only_workspace_cannot_be_written(tmp_path, upstream):
    sandbox = ExtensionSandbox(settings=SandboxSettings(), connect=upstream.connect)
    if (await sandbox.mechanism())[0] != "seatbelt":
        pytest.skip("sandbox-exec cannot confine a process here")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    try:
        _, _, seen = await run_confined(
            sandbox,
            manifest(workspace="read"),
            tmp_path,
            str(home),
            str(tmp_path / "ws"),
        )
    finally:
        await sandbox.close()
    assert seen["write_workspace"] == ["oserror", 1]
    # And with no egress declared, nothing is reachable through the proxy.
    assert seen["allowed_via_proxy"] == ["http", 403]


def calculator_running() -> bool:
    return subprocess.run(["pgrep", "-x", "Calculator"], capture_output=True).returncode == 0


@pytest.mark.skipif(not ON_MACOS, reason="Seatbelt is macOS")
async def test_on_macos_the_process_cannot_hand_work_to_another_application(tmp_path):
    """``open``, ``open -R``, ``open -a`` and AppleScript all fail.

    Each is a way to reach outside the sandbox — a browser fetches what
    a URL says, Finder or another app acts as the person. ``open -R``
    used to exit 0 here while reaching nothing; now every one of them
    fails, and so does a copy of ``open`` run from the temp directory,
    which the exec rule does not name: what stops it is that
    LaunchServices is not reachable.
    """
    sandbox = ExtensionSandbox(settings=SandboxSettings())
    if (await sandbox.mechanism())[0] != "seatbelt":
        pytest.skip("sandbox-exec cannot confine a process here")
    target = (tmp_path / "reveal-me").resolve()
    target.mkdir()
    was_running = calculator_running()
    attempts = {
        "open": ["/usr/bin/open", str(tmp_path)],
        "open -R": ["/usr/bin/open", "-R", str(target)],
        "open -a": ["/usr/bin/open", "-g", "-j", "-a", "Calculator"],
        "osascript": [
            "/usr/bin/osascript",
            "-e",
            f'tell application "Finder" to reveal POSIX file "{target}"',
        ],
    }
    codes: dict[str, int] = {}
    try:
        for name, argv in attempts.items():
            wrapped = await sandbox.confine(
                manifest(),
                stdio_config(argv[0], argv[1:], cwd=str(tmp_path)),
                bundle=tmp_path,
                data_dir=None,
                project_dir=None,
                service_url=None,
                development=False,
            )
            if name == "open":
                # And a copy, which no exec rule names.
                copy = sandbox.status("acme.probe").plan.temp_dir / "open-copy"
                shutil.copyfile("/usr/bin/open", copy)
                copy.chmod(0o755)
                proc = await asyncio.create_subprocess_exec(
                    wrapped.command,
                    *[a if a != "/usr/bin/open" else str(copy) for a in wrapped.args[:3]],
                    "-g",
                    "-j",
                    "-a",
                    "Calculator",
                    env=wrapped.env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                codes["copied open -a"] = await asyncio.wait_for(proc.wait(), 30)
            proc = await asyncio.create_subprocess_exec(
                wrapped.command,
                *wrapped.args,
                env=wrapped.env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            codes[name] = await asyncio.wait_for(proc.wait(), 30)
    finally:
        await sandbox.close()
        if not was_running and calculator_running():
            subprocess.run(["pkill", "-x", "Calculator"])
            pytest.fail("a sandboxed process launched Calculator")
    assert all(code != 0 for code in codes.values()), codes
    assert set(codes) == {"open", "open -R", "open -a", "osascript", "copied open -a"}


SERVER = r"""
import os
from mcp.server.mcpserver import MCPServer
server = MCPServer("probe")

@server.tool()
def where() -> str:
    "Which sandbox this runs in."
    return os.environ.get("IGNI_SANDBOX", "none")

server.run()
"""


@pytest.mark.skipif(not ON_MACOS, reason="Seatbelt is macOS")
async def test_on_macos_an_installed_extension_speaks_mcp_from_inside(tmp_path):
    from ember_code.core.mcp.client import MCPClientManager

    sandbox = ExtensionSandbox(settings=SandboxSettings())
    if (await sandbox.mechanism())[0] != "seatbelt":
        pytest.skip("sandbox-exec cannot confine a process here")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "server.py").write_text(SERVER)
    raw = json.loads(manifest().model_dump_json())
    raw["dev"] = {"command": [sys.executable, "server.py"]}
    raw["permissions"]["rpc"] = ["ext.acme.probe.*"]
    raw["permissions"]["tools"] = [
        {
            "name": "acme.probe.where",
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }
    ]
    (bundle / "manifest.json").write_text(json.dumps(raw))
    registry = ExtensionRegistry()
    rpc = ExtensionRpc(registry)
    mcp = MCPClientManager(project_dir=tmp_path)
    installer = ExtensionInstaller(
        registry=registry,
        rpc=rpc,
        mcp=mcp,
        allow_development=True,
        sandbox=sandbox,
    )
    try:
        entry = await installer.install(bundle)
        assert entry.state is ExtensionState.READY, entry.reason
        assert entry.sandbox == "sandboxed (seatbelt)"
        assert mcp.configs["ext:acme.probe"].command == "/usr/bin/sandbox-exec"
        # Its tool answers over stdio, from inside the sandbox.
        answer = await rpc.dispatch(caller="acme.probe", method="ext.acme.probe.where", args={})
        assert "seatbelt" in json.dumps(answer)
    finally:
        await installer.uninstall("acme.probe")
        await mcp.disconnect_all()
    # Uninstalling stops its proxy and removes its run directory.
    assert sandbox.status("acme.probe") is None


# ── Linux: real bubblewrap, where it can run ──────────────────────────


def test_the_bwrap_command_hides_home_and_bridges_the_proxy_in(tmp_path):
    m = manifest(workspace="read")
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    plan = plan_for(
        tmp_path,
        m,
        project_dir=ws,
        env={"IGNI_HOST_URL": "ws://127.0.0.1:41001/v1/host"},
    )
    Bubblewrap.socket_dir(plan).mkdir(parents=True)
    command, args = Bubblewrap.command(
        plan, "/usr/bin/python3", ["server.py"], cwd=tmp_path, home=Path("/home/someone")
    )
    joined = " ".join(args)
    assert "--unshare-net" in args and "--die-with-parent" in args
    assert "--new-session" not in args  # the process group must stay bwrap's
    for hidden in ("/home", "/root", "/run", "/tmp"):
        if Path(hidden).exists():
            assert f"--tmpfs {hidden}" in joined
    assert f"--ro-bind-try {ws} {ws}" in joined
    assert f"--bind-try {plan.temp_dir} {plan.temp_dir}" in joined
    assert f"--forward {PROXY_PORT}={Bubblewrap.proxy_socket(plan)}" in joined
    assert f"--forward 41001={Bubblewrap.relay_socket(plan, 41001)}" in joined
    assert args[-2:] == ["/usr/bin/python3", "server.py"]
    # Binds come after the tmpfs that would hide them.
    assert args.index("--tmpfs") < args.index("--ro-bind-try")


@pytest.mark.skipif(not ON_LINUX or shutil.which("bwrap") is None, reason="needs bwrap on Linux")
async def test_on_linux_the_process_reaches_only_the_proxy(tmp_path, upstream):
    sandbox = ExtensionSandbox(settings=SandboxSettings(), connect=upstream.connect)
    mechanism, why = await sandbox.mechanism()
    if mechanism != "bubblewrap":
        pytest.skip(f"bwrap cannot make a sandbox here: {why}")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    try:
        wrapped, _, seen = await run_confined(
            sandbox,
            manifest(workspace="write", egress=["allowed.test:8080"]),
            tmp_path,
            str(home),
            str(tmp_path / "ws"),
        )
    finally:
        await sandbox.close()
    assert Path(wrapped.command).name == "bwrap"
    assert seen["allowed_via_proxy"] == ["ok", "hello from allowed.test:8080"]
    assert seen["refused_via_proxy"] == ["http", 403]
    assert seen["direct_public_ip"][0] == "oserror"  # no route: its own namespace
    assert seen["write_workspace"] == ["ok", "written"]
    assert seen["write_home"][0] == "oserror"
    assert seen["env"]["IGNI_SANDBOX"] == "bubblewrap"


@pytest.mark.skipif(not ON_LINUX, reason="Linux only")
async def test_on_linux_without_bwrap_require_sandbox_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(Bubblewrap, "executable", staticmethod(lambda: None))
    sandbox = ExtensionSandbox(settings=SandboxSettings(required=True))
    with pytest.raises(SandboxRefused, match="bwrap"):
        await sandbox.confine(
            manifest(),
            stdio_config(),
            bundle=tmp_path,
            data_dir=None,
            project_dir=None,
            service_url=None,
            development=False,
        )
