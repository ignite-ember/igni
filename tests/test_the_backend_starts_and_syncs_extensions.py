"""The backend's half: start what is stored, then sync in the background.

``BackendApp._start_extensions`` is where the store, the installer, the
token endpoint and the distribution sync meet. These check the wiring
rather than the parts — each part has its own file:

* a stored development install starts only with ``IGNI_DEV_EXTENSIONS``;
* the sync is scheduled at start-up, never awaited by it;
* a sign-in schedules another.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ember_code.backend.app import BackendApp
from ember_code.backend.login_coordinator import LoginCoordinator
from ember_code.extensions.origin import ExtensionOrigins
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.store import ExtensionStore


class StubMcp:
    def __init__(self) -> None:
        self.configs: dict[str, Any] = {}

    async def connect(self, name: str) -> Any:
        return SimpleNamespace(ok=True, reason="")

    async def disconnect_one(self, name: str) -> None:
        self.configs.pop(name, None)

    def get_client(self, name: str) -> None:
        return None

    def get_tools(self, name: str) -> dict[str, Any]:
        return {}

    def require_confirmation(self, server: str, tools: set[str]) -> None:
        pass

    async def tool_annotations(self, name: str) -> dict[str, Any]:
        return {}


class NoBackend:
    async def login(self, on_status: Any = None) -> Any:
        raise AssertionError("not used")

    def reload_cloud_credentials(self) -> None:
        return None


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BackendApp:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("IGNI_DEV_EXTENSIONS", raising=False)
    built = BackendApp(
        socket_path=None,
        ws_port=None,
        project_dir=tmp_path / "project",
        resume_session_id=None,
        additional_dirs=None,
    )
    built._backend = SimpleNamespace(  # type: ignore[assignment]
        settings=SimpleNamespace(
            api_url="",
            auth=SimpleNamespace(credentials_file=str(tmp_path / "credentials.json")),
            storage=SimpleNamespace(data_dir=str(tmp_path / "data")),
        ),
        mcp_manager=StubMcp(),
    )
    registry = ExtensionRegistry()
    built._extensions = ExtensionRpc(registry)
    built._origins = ExtensionOrigins()
    built._extension_contributions = None  # type: ignore[assignment]
    built._login = LoginCoordinator(backend=NoBackend(), push_bridge=None)  # type: ignore[arg-type]
    return built


def _dev_install(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "id": "acme.mine",
                "version": "0.1.0",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["python", "server.py"]},
            }
        )
    )
    ExtensionStore().add(source)


async def _settle(app: BackendApp) -> None:
    assert app._distribution is not None
    for task in list(app._distribution._tasks):
        await task


async def test_a_stored_development_install_needs_the_flag(app, tmp_path):
    _dev_install(tmp_path)
    await app._start_extensions()
    await _settle(app)
    assert app._extensions.registry.get("acme.mine") is None
    await app._service_tokens.stop()


async def test_with_the_flag_it_starts(app, tmp_path, monkeypatch):
    _dev_install(tmp_path)
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    await app._start_extensions()
    await _settle(app)
    entry = app._extensions.registry.get("acme.mine")
    assert entry is not None and entry.state is ExtensionState.READY, entry
    await app._service_tokens.stop()


async def test_a_sync_is_scheduled_at_start_and_on_sign_in(app):
    await app._start_extensions()
    assert app._distribution is not None
    assert app._distribution._tasks, "start-up schedules a sync without waiting for it"
    await _settle(app)

    app._login._notify_signed_in()
    assert app._distribution._tasks, "a sign-in schedules another"
    await _settle(app)
    await app._service_tokens.stop()
