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
    "localhost": "the OAuth callback listener, on this machine",
    "placeholder.invalid": "RFC 2606, a deliberately unroutable stand-in",
    # ── One-time bootstrap downloads ─────────────────────────────────
    #
    # Not telemetry: nothing about the customer goes out, and they run
    # once at install. They do mean an air-gapped machine cannot bootstrap
    # unaided, which self-hosting.md §7 covers as a packaging gap rather
    # than a privacy one.
    "api.adoptium.net": "JDK download, for the JetBrains plugin bootstrap",
    "dist.neo4j.org": "Neo4j download, for the local code index",
    # ── The vendor's own hosts ───────────────────────────────────────
    "docs.ignite-ember.sh": "a documentation link in a generated config template",
    "ignite-ember.sh": "the portal URL a login flow opens in a browser",
    "api.ignite-ember.sh": (
        "OPEN QUESTION, not a settled answer. This is the default value of "
        "`api_url`, so a CLI nobody configured talks to the vendor. For a "
        "product whose stated decision is self-hosted only — no hosted tier — "
        "that default points at infrastructure that should not exist, and it "
        "means an unconfigured install reaches out from the customer network. "
        "Changing it needs a refusal path: nothing today validates an empty "
        "`api_url`, so an empty default would produce malformed requests "
        "instead of a clear error. Recorded in F94 and left as the product "
        "owner's call."
    ),
    "pypi.org": (
        "the update check. It was unconditional at every session start with "
        "no off switch; `update_check_ttl: 0` now disables it, which is what "
        "an air-gapped deployment should set. Still the only unconfigured "
        "outbound request an ordinary run makes, so it is listed here rather "
        "than treated as background noise."
    ),
}

_TELEMETRY_PACKAGES = (
    "sentry-sdk",
    "posthog",
    "mixpanel",
    "segment-analytics-python",
    "analytics-python",
    "datadog",
    "ddtrace",
    "bugsnag",
    "rollbar",
    "pyrollbar",
    "newrelic",
    "elastic-apm",
    "scout-apm",
    "opentelemetry-exporter-otlp",
    "honeycomb-beeline",
)

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
