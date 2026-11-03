"""What a developer meets in their first half hour with ``igni ext``.

Each test is a finding from a fresh-clone review: a command that
crashed, a scaffold the browser refused, an error that named nothing.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from ember_code.cli.ext import ext
from ember_code.extensions.origin import ExtensionOrigin

TOOLS_MANIFEST = {
    "id": "acme.tickets",
    "version": "0.1.0",
    "contract": ">=1.0 <2",
    "name": "Tickets",
    "trust": "unsigned",
    "dev": {"command": ["python", "server.py"]},
    "permissions": {
        "tools": [
            {
                "name": "acme.tickets.lookup_ticket",
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            }
        ],
        "rpc": ["ext.acme.tickets.lookup_ticket"],
    },
}


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    runner = CliRunner()
    store = tmp_path / "store"

    def invoke(*args: str, rooted: bool = False):
        argv = [*args, "--root", str(store)] if rooted else list(args)
        return runner.invoke(ext, argv)

    return invoke


def _bundle(tmp_path: Path, manifest: dict, name: str = "b") -> Path:
    bundle = tmp_path / name
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    (bundle / "server.py").write_text("")
    return bundle


# ── B2: install printed ToolGrant objects, then crashed ────────────


def test_installing_a_manifest_that_declares_tools_names_them(run, tmp_path):
    bundle = _bundle(tmp_path, TOOLS_MANIFEST)
    result = run("install", str(bundle), rooted=True)
    assert result.exit_code == 0, result.output
    assert "acme.tickets.lookup_ticket" in result.output
    assert "ToolGrant" not in result.output


# ── B3: the scaffold's page under the CSP it is served with ────────


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline: list[str] = []
        self.sources: list[str] = []
        self.handlers: list[str] = []
        self._in_inline = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.handlers += [name for name in attributes if name.startswith("on")]
        if tag == "script":
            if attributes.get("src"):
                self.sources.append(attributes["src"])
            else:
                self._in_inline = True
                self.inline.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_inline = False

    def handle_data(self, data):
        if self._in_inline:
            self.inline[-1] += data


def _script_src(csp: str) -> list[str]:
    for directive in csp.split(";"):
        name, *values = directive.split()
        if name == "script-src":
            return values
    for directive in csp.split(";"):
        name, *values = directive.split()
        if name == "default-src":
            return values
    return ["*"]


@pytest.mark.parametrize("packaging", [[], ["--dev"], ["--service"]])
async def test_the_scaffold_page_runs_under_the_origin_csp(run, tmp_path, packaging):
    """The page is served by :class:`ExtensionOrigin`, and its CSP is
    read off the response — not copied here, so it cannot drift."""
    target = tmp_path / "mine"
    result = run("init", str(target), *packaging)
    assert result.exit_code == 0, result.output

    bundle = target / "extension" if "--service" in packaging else target
    origin = ExtensionOrigin("local.mine", bundle / "ui")
    await origin.start()
    try:
        async with httpx.AsyncClient(base_url=origin.url) as http:
            page = await http.get("/index.html")
            assert page.status_code == 200
            allowed = _script_src(page.headers["content-security-policy"])
            scripts = _Scripts()
            scripts.feed(page.text)

            if "'unsafe-inline'" not in allowed:
                assert [s for s in scripts.inline if s.strip()] == [], (
                    "an inline <script> is blocked by script-src " + " ".join(allowed)
                )
                assert scripts.handlers == []
            assert scripts.sources, "the page loads no script, so it does nothing"
            for src in scripts.sources:
                served = await http.get(f"/{src.lstrip('./')}")
                assert served.status_code == 200, src
                assert "javascript" in served.headers["content-type"]
    finally:
        await origin.stop()


# ── init --service: the shape that reaches other people ────────────

SERVICE_FILES = (
    "README.md",
    "pyproject.toml",
    "extension/manifest.json",
    "extension/pyproject.toml",
    "extension/ui/index.html",
    "extension/ui/main.js",
    "extension/src/acme_tickets_extension/__init__.py",
    "service/service.py",
    "tests/test_extension_and_service.py",
)


def test_init_service_writes_an_extension_its_service_and_their_tests(run, tmp_path):
    target = tmp_path / "tickets"
    result = run("init", str(target), "--service", "--id", "acme.tickets")
    assert result.exit_code == 0, result.output
    for name in SERVICE_FILES:
        assert (target / name).is_file(), name

    manifest = json.loads((target / "extension" / "manifest.json").read_text())
    assert manifest["python"]["entry"] == "acme-tickets-extension"
    assert [t["name"] for t in manifest["permissions"]["tools"]] == [
        "acme.tickets.lookup_ticket",
        "acme.tickets.ticket_from_service",
    ]
    assert manifest["permissions"]["hooks"] == ["PostToolUse"]
    assert "vm" in manifest

    doctor = run("doctor", str(target / "extension"), "--no-start")
    assert doctor.exit_code == 0, doctor.output
    assert "no problems found" in doctor.output


def test_init_refuses_an_id_igni_would_refuse(run, tmp_path):
    result = run("init", str(tmp_path / "x"), "--id", "Acme Tickets")
    assert result.exit_code != 0
    assert "--id acme.tickets" in result.output
    assert not (tmp_path / "x").exists()


def test_init_help_counts_the_packagings_there_are(run):
    result = run("init", "--help")
    assert "the other two" not in result.output
    assert "four packagings" in result.output.lower()


# ── C18 / B6: what to do next is said exactly ──────────────────────


def test_install_says_exactly_what_to_restart(run, tmp_path):
    bundle = _bundle(tmp_path, TOOLS_MANIFEST)
    result = run("install", str(bundle), rooted=True)
    assert "Restart igni for it to start" not in result.output
    assert "quit the igni app" in result.output
    assert "IGNI_DEV_EXTENSIONS=1" in result.output


@pytest.mark.parametrize("packaging", [["--dev"], ["--service"]])
def test_the_scaffold_says_two_people_decide(run, tmp_path, packaging):
    """Approval by a super admin installs nothing on its own: an
    organisation admin switches the extension on for a group."""
    target = tmp_path / "mine"
    run("init", str(target), *packaging)
    readme = (target / "README.md").read_text()
    assert "super admin" in readme
    assert "organisation admin" in readme and "group" in readme
    assert (
        "Once a super admin\napproves the service, every signed-in igni installs it" not in readme
    )


def _sdk_python() -> str | None:
    """An interpreter with both SDKs: this one, or ``IGNI_SDK_PYTHON``."""
    import importlib.util
    import os
    import sys

    if all(importlib.util.find_spec(m) for m in ("igni_extension", "igni_service", "fastapi")):
        return sys.executable
    return os.environ.get("IGNI_SDK_PYTHON") or None


def test_the_service_scaffold_tests_pass_as_written(run, tmp_path):
    """Its tests are the ones the tutorial tells an author to run first.

    igni's own environment does not carry the SDKs, so this runs with an
    interpreter that does — ``IGNI_SDK_PYTHON`` — and is skipped without
    one.
    """
    import subprocess

    python = _sdk_python()
    if python is None:
        pytest.skip("no interpreter with igni-extension and igni-service (set IGNI_SDK_PYTHON)")
    target = tmp_path / "tickets"
    assert run("init", str(target), "--service", "--id", "acme.tickets").exit_code == 0
    done = subprocess.run(
        [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=target,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "3 passed" in done.stdout
