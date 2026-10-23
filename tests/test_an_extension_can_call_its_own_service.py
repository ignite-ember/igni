"""The ``ext.<id>.*`` namespace, from refusal to round trip.

Before this, ``RpcMethod`` was a closed enum and the router validated
exhaustively against it, so ``ext.acme.scanner.list`` missed the
dispatch table and came back ``Unknown RPC method``. Every call a
third-party extension's UI could make was refused. An extension could
render igni's own data back at the user and nothing else, which is
not an extension.

Most of what is tested here is refusal, because that is most of what
this code does. The ordering is load-bearing: a refusal must not
depend on whether the extension's service happens to be reachable, or
an author with a permissions problem is handed a connection error and
goes looking in the wrong place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.registry import (
    ExtensionMethod,
    ExtensionRefused,
    ExtensionRegistry,
    ExtensionState,
)
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service import ExtensionServiceError, McpExtensionService

REPO = Path(__file__).resolve().parents[1]
REFERENCE = REPO / "examples" / "reference-extension" / "manifest.json"


class FakeService:
    """An extension's service, without a subprocess.

    Records what it was asked so a test can assert the *method* that
    arrived is the tail and not the whole wire name — getting that
    wrong would call a tool named ``ext.acme.scanner.list`` on a
    server whose tool is called ``list``, and the failure would look
    like the extension's fault.
    """

    def __init__(self, answer: Any = None, raises: Exception | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def offers(self, tool: str) -> bool:
        # Everything. This fake is about what reaches `call`, and a
        # discovery answer of "no" would make every test here look
        # like a service that is not connected.
        return True

    async def call(self, method: str, args: dict[str, Any]) -> Any:
        self.calls.append((method, args))
        if self.raises is not None:
            raise self.raises
        return self.answer


@pytest.fixture
def manifest() -> ExtensionManifest:
    return ExtensionManifest.load(REFERENCE)


@pytest.fixture
def wired(manifest: ExtensionManifest) -> tuple[ExtensionRpc, FakeService]:
    registry = ExtensionRegistry()
    registry.install(manifest, state=ExtensionState.READY)
    rpc = ExtensionRpc(registry)
    service = FakeService(answer=[{"id": "v1"}])
    rpc.bind(manifest.id, service)
    return rpc, service


# ── The grammar ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("ext.acme.scanner.list", ("acme.scanner", "list")),
        ("ext.acme.list", ("acme", "list")),
        ("ext.a.b.c.d", ("a.b.c", "d")),
    ],
)
def test_the_method_is_the_last_segment(wire: str, expected: tuple[str, str]) -> None:
    """``ext.a.b.c`` is ambiguous without a rule.

    Resolving it against the installed set instead would mean a
    call's meaning changed depending on what else happened to be
    installed.
    """
    parsed = ExtensionMethod.parse(wire)
    assert parsed is not None
    assert (parsed.extension_id, parsed.method) == expected
    assert parsed.wire == wire


@pytest.mark.parametrize("wire", ["ext.acme", "ext.", "ext", "codeindex_status", ""])
def test_what_is_not_an_extension_method(wire: str) -> None:
    assert ExtensionMethod.parse(wire) is None


def test_the_namespace_is_claimed_even_when_malformed() -> None:
    """A malformed ``ext.`` name must not fall back to the base table.

    It would come back ``Unknown RPC method``, which is exactly the
    message that hid this whole gap for as long as it did.
    """
    assert ExtensionRpc.owns("ext.acme")
    assert ExtensionRpc.owns("ext.")
    assert not ExtensionRpc.owns("codeindex_status")


# ── Refusals, in the order they are decided ────────────────────────


@pytest.mark.asyncio
async def test_a_malformed_name_says_how_they_are_spelled(
    wired: tuple[ExtensionRpc, FakeService],
) -> None:
    rpc, service = wired
    with pytest.raises(ExtensionRefused, match="is not an extension method"):
        await rpc.dispatch(caller="acme.scanner", method="ext.acme", args={})
    assert service.calls == []


@pytest.mark.asyncio
async def test_a_request_with_no_caller_is_refused(
    wired: tuple[ExtensionRpc, FakeService],
) -> None:
    """igni's own UI has no business in this namespace, and a client
    too old to identify itself cannot be trusted to be either."""
    rpc, service = wired
    with pytest.raises(ExtensionRefused, match="did not say which extension"):
        await rpc.dispatch(caller=None, method="ext.acme.scanner.list", args={})
    assert service.calls == []


@pytest.mark.asyncio
async def test_an_extension_cannot_read_another_extensions_service(
    wired: tuple[ExtensionRpc, FakeService],
) -> None:
    """The id is in the method name, so without this check spelling
    somebody else's namespace would be enough to reach it."""
    rpc, service = wired
    with pytest.raises(ExtensionRefused, match="reaches its own namespace"):
        await rpc.dispatch(caller="other.ext", method="ext.acme.scanner.list", args={})
    assert service.calls == []


@pytest.mark.asyncio
async def test_an_uninstalled_extension_has_nowhere_to_go(manifest: ExtensionManifest) -> None:
    rpc = ExtensionRpc(ExtensionRegistry())
    with pytest.raises(ExtensionRefused, match="no extension 'acme.scanner' is installed"):
        await rpc.dispatch(caller="acme.scanner", method="ext.acme.scanner.list", args={})


@pytest.mark.asyncio
async def test_a_failed_extension_refuses_with_its_reason(
    manifest: ExtensionManifest,
) -> None:
    """MCP reports connectivity. "Licence expired, install the renewed
    key" is not connectivity, which is why the state carries a reason
    and the refusal repeats it."""
    registry = ExtensionRegistry()
    registry.install(
        manifest,
        state=ExtensionState.FAILED,
        reason="Licence expired; install the renewed key.",
    )
    rpc = ExtensionRpc(registry)
    rpc.bind(manifest.id, FakeService())
    with pytest.raises(ExtensionRefused, match="Licence expired"):
        await rpc.dispatch(caller="acme.scanner", method="ext.acme.scanner.list", args={})


@pytest.mark.asyncio
async def test_an_ungranted_method_names_the_manifest_line(
    manifest: ExtensionManifest,
) -> None:
    narrowed = manifest.model_copy(
        update={"permissions": manifest.permissions.model_copy(update={"rpc": []})}
    )
    registry = ExtensionRegistry()
    registry.install(narrowed)
    rpc = ExtensionRpc(registry)
    rpc.bind(narrowed.id, FakeService())
    with pytest.raises(ExtensionRefused, match="permissions.rpc"):
        await rpc.dispatch(caller="acme.scanner", method="ext.acme.scanner.list", args={})


@pytest.mark.asyncio
async def test_a_missing_binding_blames_igni_not_the_manifest(
    manifest: ExtensionManifest,
) -> None:
    """Installed, READY and granted, with nothing bound. Sending the
    author back to their permissions would be a wild goose chase."""
    registry = ExtensionRegistry()
    registry.install(manifest)
    rpc = ExtensionRpc(registry)
    with pytest.raises(ExtensionRefused, match="fault in igni"):
        await rpc.dispatch(caller="acme.scanner", method="ext.acme.scanner.list", args={})


@pytest.mark.asyncio
async def test_authorisation_is_decided_before_the_service_is_consulted(
    manifest: ExtensionManifest,
) -> None:
    """A service that always explodes must not change any refusal.

    This is the ordering guarantee, tested rather than asserted: if
    routing came first, a permissions problem would surface as a
    connection error.
    """
    registry = ExtensionRegistry()
    registry.install(manifest)
    rpc = ExtensionRpc(registry)
    rpc.bind(manifest.id, FakeService(raises=ExtensionServiceError("service is on fire")))

    with pytest.raises(ExtensionRefused, match="reaches its own namespace"):
        await rpc.dispatch(caller="someone.else", method="ext.acme.scanner.list", args={})


# ── The round trip ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_granted_call_reaches_the_service(
    wired: tuple[ExtensionRpc, FakeService],
) -> None:
    rpc, service = wired
    result = await rpc.dispatch(
        caller="acme.scanner",
        method="ext.acme.scanner.list",
        args={"severity": "high"},
    )
    assert result == [{"id": "v1"}]
    # The tail, not the wire name: the server's tool is called
    # ``list``, not ``ext.acme.scanner.list``.
    assert service.calls == [("list", {"severity": "high"})]


@pytest.mark.asyncio
async def test_every_method_the_reference_extension_calls_is_granted(
    wired: tuple[ExtensionRpc, FakeService],
) -> None:
    """The reference extension is the standing check on this contract.

    Its four calls are read off `examples/reference-extension/src/`,
    so a manifest edit that narrows the grant fails here rather than
    in somebody's browser.
    """
    rpc, _ = wired
    for method in ("list", "status", "scan", "accept"):
        await rpc.dispatch(caller="acme.scanner", method=f"ext.acme.scanner.{method}", args={})


@pytest.mark.asyncio
async def test_a_service_failure_reaches_the_author_as_a_sentence(
    manifest: ExtensionManifest,
) -> None:
    registry = ExtensionRegistry()
    registry.install(manifest)
    rpc = ExtensionRpc(registry)
    rpc.bind(manifest.id, FakeService(raises=ExtensionServiceError("the sidecar is not up")))
    with pytest.raises(ExtensionRefused, match="the sidecar is not up"):
        await rpc.dispatch(caller="acme.scanner", method="ext.acme.scanner.list", args={})


# ── Grants ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("grants", "method", "allowed"),
    [
        (["ext.acme.scanner.*"], "ext.acme.scanner.list", True),
        (["ext.acme.scanner.*"], "ext.acme.scanner.accept", True),
        (["ext.acme.scanner.*"], "ext.acme.other.list", False),
        (["ext.acme.scanner.list"], "ext.acme.scanner.list", True),
        (["ext.acme.scanner.list"], "ext.acme.scanner.accept", False),
        ([], "ext.acme.scanner.list", False),
        (["codeindex_status"], "codeindex_status", True),
        (["codeindex_"], "codeindex_status", False),
    ],
)
def test_what_a_grant_covers(
    manifest: ExtensionManifest, grants: list[str], method: str, allowed: bool
) -> None:
    """A prefix grant ends at ``*`` and an exact grant is exact.

    ``codeindex_`` without a star granting ``codeindex_status`` would
    be the classic off-by-one here, and it is the kind an
    administrator approving an install would never spot.
    """
    narrowed = manifest.model_copy(
        update={"permissions": manifest.permissions.model_copy(update={"rpc": grants})}
    )
    assert narrowed.grants_rpc(method) is allowed


# ── Turning an MCP answer into a value ─────────────────────────────


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Result:
    def __init__(self, content: list[Any], structured: Any = None, error: bool = False) -> None:
        self.content = content
        self.structuredContent = structured
        self.isError = error


def test_text_wins_over_structured_content() -> None:
    """The opposite of what this pinned first, and a real server is why.

    The spec says a tool returning structured content should *also*
    return the serialised JSON as text, so text is always present and
    always canonical. Meanwhile a FastMCP tool annotated ``-> str``
    gets a synthesised output schema of ``{"result": string}``, so
    its ``structuredContent`` is a wrapper around the answer rather
    than the answer. Preferring it handed every such extension an
    envelope to unpick.

    This stub could not have caught that — it was written from an
    assumption about what servers send, and agreed with the code for
    the same reason. ``test_an_extension_installs_and_answers.py``
    runs a real server, and found it on the first call.
    """
    assert McpExtensionService.unwrap(
        _Result([_Block('{"rows": 2}')], structured={"result": '{"rows": 2}'})
    ) == {"rows": 2}


def test_structured_content_is_used_when_there_is_no_text() -> None:
    assert McpExtensionService.unwrap(_Result([], structured={"rows": 2})) == {"rows": 2}


def test_json_text_is_parsed_because_that_is_what_a_ui_server_sends() -> None:
    assert McpExtensionService.unwrap(_Result([_Block('[{"id": "v1"}]')])) == [{"id": "v1"}]


def test_text_that_is_not_json_stays_text() -> None:
    """A tool that returns a sentence is a tool, not a fault."""
    assert McpExtensionService.unwrap(_Result([_Block("scan started")])) == "scan started"


def test_an_answer_a_ui_cannot_use_is_an_error_not_an_empty_success() -> None:
    with pytest.raises(ExtensionServiceError, match="no text content"):
        McpExtensionService.unwrap(_Result([]))
