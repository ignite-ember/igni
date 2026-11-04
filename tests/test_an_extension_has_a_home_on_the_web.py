"""An extension introduced through igni server has a home on the web.

Its service's URL — the distribution record's ``service_url`` — is the
service's ``client_uri``, which RFC 7591 defines as the client's home
page. igni shows it as "Open on the web" and the host object model
reports it as ``web_url``. A development install uses its ``serviceUrl``
setting when one is set; with neither, there is no link.
"""

from __future__ import annotations

import json
from pathlib import Path

from ember_code.backend.rpc_handlers.base import RpcHandlerContext
from ember_code.backend.rpc_handlers.extensions import ExtensionsRpcHandler
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.store import DistributionRecord, ExtensionStore
from ember_code.extensions.web_home import ExtensionWebHome
from tests.host_model_helpers import manifest


def record(extension_id: str, service_url: str) -> DistributionRecord:
    return DistributionRecord(
        extension_id=extension_id, version="1.0.0", sha256="0" * 64, service_url=service_url
    )


def test_a_distributed_extension_is_at_its_service(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path)
    store.record_distribution(record("acme.reviewer", "https://reviewer.acme.test"))
    assert (
        ExtensionWebHome(store).url_for(manifest("acme.reviewer")) == "https://reviewer.acme.test"
    )


def test_the_distribution_record_wins_over_a_setting(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path)
    m = manifest("acme.reviewer")
    store.save_settings(m, {"serviceUrl": "https://dev.acme.test"})
    store.record_distribution(record("acme.reviewer", "https://reviewer.acme.test"))
    assert ExtensionWebHome(store).url_for(m) == "https://reviewer.acme.test"


def test_a_development_install_uses_its_service_url_setting_or_has_none(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path)
    m = manifest("acme.dev")
    home = ExtensionWebHome(store)
    assert home.url_for(m) is None
    store.save_settings(m, {"serviceUrl": "  https://dev.acme.test  "})
    assert home.url_for(m) == "https://dev.acme.test"


def test_only_a_web_address_is_a_home_page(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path)
    m = manifest("acme.dev")
    store.save_settings(m, {"serviceUrl": "javascript:alert(1)"})
    assert ExtensionWebHome(store).url_for(m) is None


async def test_the_extension_list_carries_it_for_the_window() -> None:
    """``list_extensions`` is what the Extensions panel and pages draw from."""
    registry = ExtensionRegistry()
    registry.install(manifest("acme.reviewer", host=["chat.read"]), state=ExtensionState.READY)
    registry.install(manifest("acme.plain"), state=ExtensionState.READY)
    ExtensionStore().record_distribution(record("acme.reviewer", "https://reviewer.acme.test"))
    ctx = RpcHandlerContext(
        backend=None,
        transport=None,
        login=None,
        push=None,
        extensions=ExtensionRpc(registry),  # type: ignore[arg-type]
    )

    listed = await ExtensionsRpcHandler(ctx).list_extensions({})

    by_id = {e.id: json.loads(e.model_dump_json()) for e in listed.extensions}
    assert by_id["acme.reviewer"]["web_url"] == "https://reviewer.acme.test"
    assert by_id["acme.reviewer"]["host"] == ["chat.read"]
    assert by_id["acme.plain"]["web_url"] is None
