"""The cloud profile: ``IGNI_PROFILE=cloud``, and what it changes.

``docs/CLOUD.md`` section 9. In the cloud nothing is written to or read
from ``~/.igni`` for logs, credentials or settings: logs are JSON lines
on standard output, credentials come from the environment the gateway
sets, settings only from the image and igni server, and state lives in
``IGNI_DATABASE_URL``. The desktop is unchanged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from igni_cloud.credentials import EnvironmentCredentials
from igni_cloud.layer import CloudLayer
from igni_cloud.logs import StdoutLog
from igni_cloud.mcp_approvals import CloudMcpApprovals, PersonMcpApprovals

from ember_code.backend.hitl_tracer import HITLTracer
from ember_code.backend.logging_setup import configure_logging
from ember_code.core.auth.credentials import (
    CloudCredentials,
    Credentials,
    CredentialsStore,
)
from ember_code.core.config.llm_call_logger import LlmCallLogger
from ember_code.core.config.managed_policy import ManagedPolicySource
from ember_code.core.config.settings import Settings, load_settings
from ember_code.core.db.locator import StorageLocator
from ember_code.core.layer import Layer, Layers, UnknownLayer
from ember_code.core.mcp.approval import MCPApprovalManager
from ember_code.core.utils.audit import AuditLogger
from ember_code.core.utils.audit_schemas import AuditEntry
from ember_code.extensions.store import ExtensionStore, StoredGroup


@pytest.fixture
def cloud(cloud_layer: CloudLayer) -> CloudLayer:
    return cloud_layer


@pytest.fixture
def clean_loggers() -> Iterator[None]:
    names = ("ember_code", "ember_code.llm_calls", "httpx", "httpcore")
    saved = {n: list(logging.getLogger(n).handlers) for n in names}
    # A handler another test left behind would answer for the one
    # under test (``LlmCallLogger`` installs only when there is none).
    for name in names:
        logging.getLogger(name).handlers[:] = []
    yield
    for name, handlers in saved.items():
        logging.getLogger(name).handlers[:] = handlers


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    """A home directory of the test's own, with an igni config in it."""
    fake = tmp_path / "home"
    (fake / ".igni").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: fake)
    return fake


def test_one_setting_installs_the_layer_this_package_declares(monkeypatch):
    assert type(Layers.current()) is Layer, "unset: the desktop's"
    monkeypatch.setenv(Layers.ENV, "cloud")
    Layers.reset()
    assert isinstance(Layers.current(), CloudLayer), "found through the igni.layers entry point"
    monkeypatch.setenv(Layers.ENV, "laptop")
    Layers.reset()
    with pytest.raises(UnknownLayer, match="laptop"):
        Layers.current()


def test_the_backend_logs_json_lines_to_stdout_and_writes_no_file(
    cloud, clean_loggers, capsys, tmp_path: Path
):
    assert configure_logging(path=tmp_path / "backend.log") is None
    logging.getLogger("ember_code.backend.test").warning("hello %s", "cloud")
    StdoutLog.flush()
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["message"] == "hello cloud"
    assert line["level"] == "WARNING"
    assert line["logger"] == "ember_code.backend.test"
    assert not (tmp_path / "backend.log").exists()


def test_the_hitl_trace_writes_no_file(cloud, clean_loggers, capsys, tmp_path: Path):
    configure_logging()
    HITLTracer(path=tmp_path / "hitl_trace.log").trace("paused run r1")
    StdoutLog.flush()
    assert not (tmp_path / "hitl_trace.log").exists()
    assert "paused run r1" in capsys.readouterr().out


def test_the_audit_log_is_a_json_line_on_stdout(cloud, clean_loggers, capsys, tmp_path: Path):
    configure_logging()
    settings = Settings()
    settings.storage.audit_log = str(tmp_path / "audit" / "audit.log")
    result = AuditLogger(settings).log(
        AuditEntry.success(session_id="s1", agent_name="igni", tool_name="run_shell_command")
    )
    assert result.ok
    StdoutLog.flush()
    assert not (tmp_path / "audit").exists()
    lines = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    [entry] = [line["audit"] for line in lines if "audit" in line]
    assert (entry["session_id"], entry["tool"], entry["status"]) == (
        "s1",
        "run_shell_command",
        "success",
    )


def test_llm_calls_have_no_log_file(cloud, clean_loggers, tmp_path: Path):
    LlmCallLogger(log_dir=tmp_path).ensure_configured()
    assert not (tmp_path / "llm_calls.log").exists()
    assert StdoutLog.handler() in logging.getLogger("ember_code.llm_calls").handlers


def test_credentials_come_from_the_environment_and_are_never_written(
    cloud, monkeypatch, tmp_path: Path
):
    path = tmp_path / "credentials.json"
    path.write_text(Credentials(access_token="from-a-file").model_dump_json())
    store = CredentialsStore(str(path))
    assert not store.load().ok, "the file is not read"
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, "from-the-gateway")
    assert store.load().creds.access_token == "from-the-gateway"  # type: ignore[union-attr]
    assert CloudCredentials(str(path)).access_token == "from-the-gateway"

    elsewhere = tmp_path / "new.json"
    CredentialsStore(str(elsewhere)).save(Credentials(access_token="x"))
    assert not elsewhere.exists()
    CredentialsStore(str(path)).clear()
    assert path.exists(), "clearing writes nothing either"


def _configured(home: Path, tmp_path: Path) -> Path:
    (home / ".igni" / "config.yaml").write_text("storage:\n  max_history_runs: 7\n")
    project = tmp_path / "project"
    (project / ".igni").mkdir(parents=True)
    (project / ".igni" / "config.yaml").write_text("storage:\n  max_history_runs: 9\n")
    return project


def test_settings_are_not_read_from_the_home_or_the_workspace(cloud, home, tmp_path: Path):
    project = _configured(home, tmp_path)
    assert load_settings(project_dir=project).storage.max_history_runs == 10000


def test_the_desktop_reads_them_as_before(home, tmp_path: Path):
    project = _configured(home, tmp_path)
    assert load_settings(project_dir=project).storage.max_history_runs == 9


def test_the_cloud_needs_its_database(cloud, tmp_path: Path):
    with pytest.raises(RuntimeError, match="IGNI_DATABASE_URL"):
        StorageLocator(database_url=None, data_dir=tmp_path)
    assert StorageLocator(database_url=f"sqlite:///{tmp_path}/x.db", data_dir=tmp_path).shared


def test_priority_and_group_are_igni_servers_and_never_written(cloud, tmp_path: Path):
    root = tmp_path / "extensions"
    assert ExtensionStore(root).record_priority(["acme.a", "acme.b"])
    ExtensionStore(root).record_group("engineering")
    assert ExtensionStore(root).priority() == ["acme.a", "acme.b"]
    assert ExtensionStore(root).group() == StoredGroup(group="engineering")
    assert not root.exists(), "nothing written in the VM"


class PersonApprovalsServer:
    """igni server's ``/v1/me/mcp-approvals``, for one person."""

    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.method)
        assert request.headers["Authorization"] == "Bearer the-session"
        if request.method == "POST":
            body = json.loads(request.content)
            if body not in [{"server": r["server"], "source": r["source"]} for r in self.rows]:
                self.rows.append({**body, "approved_at": "2026-10-26T12:00:00Z"})
        elif request.method == "DELETE":
            key = (request.url.params["server"], request.url.params["source"])
            self.rows = [r for r in self.rows if (r["server"], r["source"]) != key]
            return httpx.Response(204)
        return httpx.Response(200, json={"approvals": self.rows})


def test_mcp_servers_are_offered_by_group_policy_and_approved_by_the_person(cloud, tmp_path: Path):
    delivered = tmp_path / "group-policy" / "mcps"
    delivered.mkdir(parents=True)
    approvals_file = tmp_path / "mcp-approved.json"
    approvals_file.write_text('{"local": ["/repo/.mcp.json"]}')
    server = PersonApprovalsServer()
    person = PersonMcpApprovals(
        api_url=lambda: "https://igni.test",
        token=lambda: "the-session",
        transport=httpx.MockTransport(server.handle),
    )
    approvals = MCPApprovalManager(
        approval_path=approvals_file,
        policy=CloudMcpApprovals(delivered_dir=delivered, person=person),
    )
    search = str(delivered / "search.json")

    assert not approvals.is_approved("search", search), "offered, not yet approved"
    approvals.approve("search", search)
    assert approvals.is_approved("search", search), "approved on igni server, for every VM"
    assert server.rows[0]["source"] == search

    assert not approvals.is_approved("local", "/repo/.mcp.json"), "the file is not read"
    approvals.approve("local", "/repo/.mcp.json")
    assert not approvals.is_approved("local", "/repo/.mcp.json"), "not offered: not approvable"
    assert not approvals.check_approval("local", "/repo/.mcp.json")
    assert approvals_file.read_text() == '{"local": ["/repo/.mcp.json"]}', "nothing written"


def test_a_persons_approvals_are_read_once_a_minute_and_withdrawn_there():
    server = PersonApprovalsServer()
    server.rows.append({"server": "search", "source": "/gp/search.json"})
    now = [0.0]
    person = PersonMcpApprovals(
        api_url=lambda: "https://igni.test",
        token=lambda: "the-session",
        transport=httpx.MockTransport(server.handle),
        clock=lambda: now[0],
    )
    assert person.approved("search", "/gp/search.json")
    assert person.approved("search", "/gp/search.json")
    assert server.calls == ["GET"], "cached"
    person.withdraw("search", "/gp/search.json")
    assert not person.approved("search", "/gp/search.json")
    now[0] += PersonMcpApprovals.TTL_SECONDS
    person.approved("search", "/gp/search.json")
    assert server.calls == ["GET", "DELETE", "GET"]


def test_an_unreachable_server_approves_nothing():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    person = PersonMcpApprovals(
        api_url=lambda: "https://igni.test",
        token=lambda: "the-session",
        transport=httpx.MockTransport(down),
    )
    assert not person.approved("search", "/gp/search.json")
    signed_out = PersonMcpApprovals(api_url=lambda: "https://igni.test", token=lambda: None)
    assert not signed_out.approved("search", "/gp/search.json")


def test_a_fresh_token_reaches_a_running_vm_through_the_file_the_gateway_writes(
    cloud, monkeypatch, tmp_path: Path
):
    token_file = tmp_path / "token"
    token_file.write_text("first\n")
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_FILE_ENV, str(token_file))
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, "ignored when the file is named")
    assert CloudCredentials().access_token == "first"
    fresh = tmp_path / "token.new"
    fresh.write_text("second")
    fresh.replace(token_file)  # how the gateway refreshes it
    assert CloudCredentials().access_token == "second"


def test_managed_settings_are_what_the_node_agent_wrote_to_the_run_directory(
    cloud, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("IGNI_RUN_DIR", str(tmp_path))
    (tmp_path / "managed-settings.yaml").write_text("update_check_ttl: 0\n")
    assert ManagedPolicySource.platform_path() == tmp_path / "managed-settings.yaml"
    assert ManagedPolicySource.load() == {"update_check_ttl": 0}
