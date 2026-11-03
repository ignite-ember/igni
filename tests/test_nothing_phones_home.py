"""The air-gap claim, defended on the half that runs on developer machines.

DP-5 in ember-server's `docs/COMPLIANCE_TRACKER.md`. The server has its
own copy of this rule; this one matters more, because the CLI runs on a
developer's workstation with their source code open in front of it.

`COMPLIANCE.md` rests the GDPR position on nothing phoning home: a
self-hosted deployment where no customer data reaches the vendor usually
makes the customer the controller and the vendor neither controller nor
processor. That removes the per-customer DPA, the transfer mechanism and
the processor obligations. The claim is the asset, so it gets a rule
rather than good intentions.

The allow-list below doubles as an inventory of every host this CLI can
name, which is what an air-gap review actually asks for. Two entries are
open questions rather than settled answers, and they say so.
"""

from __future__ import annotations

import ast
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src/ember_code"

_ALLOWED_HOSTS: dict[str, str] = {
    # ── The customer's own deployment, or a provider they connected ──
    "github.com": "the provider, for release links and clone URLs",
    # Contacted only for a model entry that names `provider: anthropic`
    # and no `url`: the customer chose Anthropic's API for that model.
    # With a `url` the request goes there instead, and the builder never
    # lets the SDK take an endpoint or a credential from the machine
    # (`tests/test_anthropic_provider.py` runs a turn under the guard).
    "api.anthropic.com": "the anthropic provider's default endpoint, for a model "
    "entry that selects it without a `url`",
    # 127.0.0.1 rather than `localhost`: the callback server binds the
    # literal address, and `localhost` resolves to ::1 first on a
    # dual-stack machine — a connection refused on the one step of a
    # login the user cannot retry. F124.
    "127.0.0.1": "the sign-in callback listener, on this machine",
    "placeholder.invalid": "RFC 2606, a deliberately unroutable stand-in",
    "tauri.localhost": "not contacted: the desktop webview's Origin, which the "
    "backend's WebSocket accepts (transport/ws_auth.py)",
    # The one-time Neo4j and JDK downloads left with the local code
    # index: the CodeIndex extension makes them, and declares both hosts
    # in its manifest's `permissions.egress`.
    # ── The vendor's own hosts ───────────────────────────────────────
    "docs.ignite-ember.sh": "a documentation link in a generated config template",
    # A JSON Schema `$id`, written into the manifest `igni ext init`
    # scaffolds. Identifiers, not addresses: nothing dereferences it,
    # and an editor that does is resolving a schema the author asked
    # it to, on their machine, about their own file. The schema it
    # names ships in the repository at
    # docs/extension-manifest.schema.json.
    "igni.sh": "the $schema identifier in a scaffolded extension manifest",
    # `ignite-ember.sh` and `api.ignite-ember.sh` used to be here, and
    # the second carried a paragraph beginning "OPEN QUESTION, not a
    # settled answer": `api_url` defaulted to the vendor, so a CLI
    # nobody configured talked to infrastructure that should not exist,
    # and flipping the default to empty needed a refusal path first.
    #
    # DEC-14 answered it. `api_url` defaults to empty,
    # `core/config/endpoint.py` refuses with the setting name and an
    # example, and the paths for which no server is a supported state
    # ask `is_configured` and skip. `PortalClient` no longer has a
    # portal-host default at all — that one had no setting anywhere, so
    # a customer pointing the CLI at their own deployment still got sent
    # to the vendor's website to sign in.
    #
    # Their absence from this list is now the evidence. `#
    # test_the_allow_list_has_not_gone_stale` fails if either name comes
    # back without an entry, which is the check that keeps this
    # paragraph honest rather than merely well-intentioned.
    "igni.your-company.example": (
        "not a host anything contacts — the example URL in the refusal message "
        "`core/config/endpoint.py` prints when `api_url` is unset. RFC 2606 "
        "reserves `.example`, so it is unroutable by construction. Listed "
        "because the scan reads string literals and cannot tell an example "
        "from a destination, and a scan that guessed would be the wrong scan."
    ),
    "pypi.org": (
        "the update check. It was unconditional at every session start with "
        "no off switch; `update_check_ttl: 0` now disables it, which is what "
        "an air-gapped deployment should set. Still the only unconfigured "
        "outbound request an ordinary run makes, so it is listed here rather "
        "than treated as background noise."
    ),
}

#: Packages whose purpose is to send data to a vendor: error reporting,
#: product analytics, APM agents, and the LLM-observability SDKs whose
#: default destination is their maker's cloud. None may be in the tree.
_TELEMETRY_PACKAGES = (
    "sentry-sdk",
    "posthog",
    "mixpanel",
    "segment-analytics-python",
    "analytics-python",
    "datadog",
    "datadog-api-client",
    "ddtrace",
    "bugsnag",
    "rollbar",
    "pyrollbar",
    "newrelic",
    "elastic-apm",
    "scout-apm",
    "honeycomb-beeline",
    "honeycomb-opentelemetry",
    "azure-monitor-opentelemetry",
    "azure-monitor-opentelemetry-exporter",
    "opentelemetry-exporter-gcp-trace",
    "aws-xray-sdk",
    "logfire",
    "langfuse",
    "langsmith",
    "arize-otel",
    "arize-phoenix-otel",
    "agentops",
    "openlit",
    "traceloop-sdk",
    "wandb",
    "weave",
    "lunary",
    "opik",
    "braintrust",
)

#: Hosts that may never be named in the source, whatever the reason given:
#: Agno's own servers. Agno posts every run to ``os-api.agno.com`` unless
#: ``AGNO_TELEMETRY`` says otherwise (``core/agno_telemetry.py``), and its
#: AgentOS control plane is ``os.agno.com``. igni contacts neither, and
#: ``test_no_run_reaches_an_agno_host.py`` watches the sockets for it.
_NEVER_NAMED_SUFFIXES = ("agno.com",)

#: The OTLP exporters, allowed by name and for one reason each. They used to
#: be on the list above, and an exporter is a means of sending data; these
#: ones send only where the customer says:
#:
#: * there is **no default endpoint** — with none configured nothing is
#:   exported and no provider is built;
#: * the endpoint is **customer-configured** — only the managed-settings
#:   file, the group policy and the ``OTEL_*`` environment may set it, never
#:   a user's or a repository's config;
#: * under **air-gapped** mode it must be **inside the network**, or nothing
#:   is exported.
#:
#: ``test_telemetry_goes_only_to_the_customers_collector.py`` proves each of
#: the three; the test below checks those tests are still there.
_OTLP_EXPORTERS: dict[str, str] = {
    "opentelemetry-exporter-otlp-proto-http": "the customer's collector, over OTLP/HTTP",
    "opentelemetry-exporter-otlp-proto-grpc": "the customer's collector, over OTLP/gRPC",
    "opentelemetry-exporter-otlp-proto-common": "the encoding both exporters share",
    "opentelemetry-exporter-otlp-common": "the encoding both exporters share",
    "opentelemetry-exporter-http-transport": "the HTTP exporter's transport",
}

_HOST_IN_URL = re.compile(r"https?://([A-Za-z0-9._%-]+)")


def _code_strings(source: str) -> list[str]:
    """String literals that are not docstrings — a host named in prose
    cannot make a request, and comments never reach the AST."""
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _hosts_named() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        for literal in _code_strings(path.read_text()):
            for host in _HOST_IN_URL.findall(literal):
                found.setdefault(host, set()).add(str(path.relative_to(_ROOT)))
    return found


def test_the_scan_reads_the_package():
    """A rule that matches nothing passes forever."""
    assert len(list(_SRC.rglob("*.py"))) > 100
    hosts = _hosts_named()
    assert len(hosts) >= 5, sorted(hosts)
    assert "github.com" in hosts


def test_every_host_is_allow_listed_with_a_reason():
    found = _hosts_named()
    unknown = sorted(set(found) - set(_ALLOWED_HOSTS))

    assert not unknown, (
        "these hosts are named in src/ember_code and are not on the allow-list:\n"
        + "\n".join(f"  {h} — in {sorted(found[h])}" for h in unknown)
        + "\n\nThis CLI runs on a developer machine with their source open. If the host "
        "is somewhere the customer chose to send data, or a one-time bootstrap download, "
        "add it to _ALLOWED_HOSTS saying which."
    )


def test_the_allow_list_has_not_gone_stale():
    found = _hosts_named()
    stale = sorted(set(_ALLOWED_HOSTS) - set(found))

    assert not stale, (
        f"allow-list entries naming hosts the code no longer mentions: {stale}. "
        "The list is meant to be a readable inventory."
    )


def test_no_telemetry_package_is_installed():
    """The lock file, not `pyproject.toml`: a telemetry package arriving
    transitively reaches the network just as easily as a direct one."""
    lock = (_ROOT / "uv.lock").read_text()
    present = [
        pkg for pkg in _TELEMETRY_PACKAGES if re.search(rf'^name = "{re.escape(pkg)}"$', lock, re.M)
    ]

    assert not present, f"telemetry packages in the dependency tree: {present}"
    # And prove the pattern matches this lock file's format at all.
    assert re.search(r'^name = "httpx"$', lock, re.M), "the lock format changed"


def test_no_agno_host_is_named_or_allow_listed():
    named = [h for h in _hosts_named() if h.lower().endswith(_NEVER_NAMED_SUFFIXES)]
    listed = [h for h in _ALLOWED_HOSTS if h.lower().endswith(_NEVER_NAMED_SUFFIXES)]

    assert not named, f"igni's source names Agno's servers: {named}"
    assert not listed, f"Agno's servers are on the allow-list: {listed}"


def test_every_exporter_in_the_tree_is_one_the_customer_points():
    """An OTLP exporter is allowed, a vendor's is not, and the allowance
    rests on three properties that each have a test."""
    lock = (_ROOT / "uv.lock").read_text()
    exporters = re.findall(r'^name = "(opentelemetry-exporter-[^"]+)"$', lock, re.M)
    unexplained = sorted(set(exporters) - set(_OTLP_EXPORTERS))

    assert not unexplained, f"OpenTelemetry exporters with no reason to be here: {unexplained}"
    proofs = (_ROOT / "tests/test_telemetry_goes_only_to_the_customers_collector.py").read_text()
    for proof in (
        "def test_with_no_endpoint_nothing_is_set_up",
        "def test_a_user_or_project_config_cannot_name_the_collector",
        "def test_an_external_collector_is_refused_when_air_gapped",
    ):
        assert proof in proofs, f"the reason the OTLP exporters are allowed lost its test: {proof}"


def test_the_update_check_can_be_turned_off():
    """It was the only unconditional outbound request on an ordinary run,
    and `update_check_ttl` changed only its frequency. An air-gapped
    deployment needs an off switch, not a longer interval."""
    import asyncio

    import httpx

    from ember_code.core.config.settings import Settings
    from ember_code.core.utils.update_checker import UpdateChecker

    calls: list[str] = []

    class _Explode(httpx.AsyncClient):
        async def get(self, url, *args, **kwargs):  # type: ignore[override]
            calls.append(str(url))
            raise AssertionError(f"the disabled update check still requested {url}")

    monkey = httpx.AsyncClient
    httpx.AsyncClient = _Explode  # type: ignore[misc]
    try:
        result = asyncio.run(UpdateChecker(settings=Settings(update_check_ttl=0)).check())
    finally:
        httpx.AsyncClient = monkey  # type: ignore[misc]

    # Asserted on the *absence of a request* rather than on the returned
    # fields: `latest_version` defaults to `''` when nothing was fetched,
    # so an assertion about the value would have passed even if the call
    # had happened and failed. The first version of this test made exactly
    # that mistake.
    assert calls == [], calls
    assert result.available is False


def test_an_unconfigured_session_start_clones_no_marketplace(tmp_path):
    """No outbound call by default, air-gapped or not.

    Every session start of an install nobody configured used to register
    Anthropic's plugin marketplace: a `git clone` from github.com. It is
    opt-in now (`plugins.default_marketplace: true`), and this runs the
    real session-start phase on default settings with git refusing to
    start, so a clone anywhere in it fails the test.
    """
    import asyncio
    import subprocess
    from unittest.mock import patch

    from ember_code.core.config.schemas import StorageConfig
    from ember_code.core.config.settings import Settings
    from ember_code.core.session.core import Session
    from ember_code.core.session.startup.marketplace import MarketplaceWarmupPhase

    started: list[list[str]] = []

    def no_git(args, **kwargs) -> subprocess.CompletedProcess:
        started.append(list(args))
        raise AssertionError(f"git was started at session start: {args}")

    settings = Settings(storage=StorageConfig(data_dir=str(tmp_path / "data")))
    assert settings.air_gapped is False
    session = Session.__new__(Session)
    session.settings = settings
    with patch("ember_code.core.plugins.git.subprocess.run", no_git):
        asyncio.run(MarketplaceWarmupPhase(session)._refresh_all())

    assert started == []


def test_the_default_marketplace_is_registered_only_on_request(tmp_path):
    from ember_code.core.config.schemas import PluginsConfig
    from ember_code.core.config.settings import Settings
    from ember_code.core.plugins.marketplace_store import DEFAULT_MARKETPLACES
    from ember_code.core.plugins.source_policy import PluginSourcePolicy

    [(_name, official)] = DEFAULT_MARKETPLACES
    assert PluginSourcePolicy.from_settings(Settings()).marketplaces_to_register() == []
    opted_in = Settings(plugins=PluginsConfig(default_marketplace=True))
    assert PluginSourcePolicy.from_settings(opted_in).marketplaces_to_register() == [official]
    opted_in.air_gapped = True
    assert PluginSourcePolicy.from_settings(opted_in).marketplaces_to_register() == []
