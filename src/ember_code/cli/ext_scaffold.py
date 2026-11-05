"""What ``igni ext init`` writes.

Four packagings exist — ``python``, ``dev``, ``endpoint`` and
``sidecar`` — and two of them are scaffolded:

* **dev** (``--dev``, the default): a command igni runs on this machine.
  The quickest thing that installs and starts, and the one way to run
  an extension with nothing else deployed. It never leaves the laptop.
* **service** (``--service``): what reaches other people — a
  ``python``-packaged extension (two tools, a hook, a page, a VM
  request), the service that ships it (``igni-service``, with export
  and erasure), and tests for both on the SDKs' fakes.

``endpoint`` (a service someone else runs) and ``sidecar`` (a container
image, which igni cannot start yet) need something an author does not
have on a laptop, so they are not scaffolded.

The page is a file and its script another: an extension's origin serves
it under ``script-src 'self'`` (:mod:`ember_code.extensions.origin`),
which refuses an inline ``<script>`` — the first scaffold had one, so
the page it wrote never ran.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

#: Where the manifest's JSON Schema is published. The same file is
#: ``docs/extension-manifest.schema.json`` in igni's repository.
SCHEMA_URL = "https://ignite-ember.sh/schema/extension-manifest-1.json"

#: The bridge, inlined so neither scaffold has a build step. A real
#: extension imports @ignite-ember/extension-bridge and gets this with
#: types. ``{{ON_HELLO}}`` is what the page does once igni greets it.
_BRIDGE = """// The bridge to igni, inlined so this page has no build step. A real
// extension imports @ignite-ember/extension-bridge and gets this with
// types — see examples/reference-extension in igni's repository.
//
// This is a file of its own, loaded with <script src>, because the
// page is served with a Content-Security-Policy of script-src 'self':
// an inline <script> in index.html would never run.
const host = window.parent;
let nonce = null;
let next = 0;
const waiting = new Map();

window.addEventListener("message", (event) => {
  const message = event.data;
  if (event.source !== host || message?.igni !== 1) return;
  if (message.kind === "hello") {
    nonce = message.nonce;
    onHello();
    return;
  }
  if (message.kind === "result") {
    const waiter = waiting.get(message.id);
    if (!waiter) return;
    waiting.delete(message.id);
    message.ok ? waiter.resolve(message.value) : waiter.reject(new Error(message.error));
  }
});

// Call a backend method this extension's permissions.rpc grants.
function call(method, args = {}) {
  const id = String(++next);
  return new Promise((resolve, reject) => {
    waiting.set(id, { resolve, reject });
    host.postMessage({ igni: 1, kind: "call", nonce, id, method, args }, "*");
  });
}

{{ON_HELLO}}
"""

_PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>{{TITLE}}</title>
  </head>
  <body class="igni-root">
{{BODY}}
    <script type="module" src="main.js"></script>
  </body>
</html>
"""


class Scaffold:
    """Writes one scaffold into an empty directory."""

    def __init__(self, directory: Path, extension_id: str | None) -> None:
        self.directory = directory
        self.title = directory.resolve().name
        self.id = extension_id or "local." + re.sub(r"[^a-z0-9-]+", "-", self.title.lower()).strip(
            "-"
        )

    # ── helpers ───────────────────────────────────────────────────

    @property
    def module(self) -> str:
        """The extension's Python package: ``acme.tickets`` → ``acme_tickets_extension``."""
        return re.sub(r"[^a-z0-9]+", "_", self.id.lower()).strip("_") + "_extension"

    @property
    def distribution(self) -> str:
        return self.module.replace("_", "-")

    def _fill(self, text: str) -> str:
        return (
            text.replace("{{ID}}", self.id)
            .replace("{{TITLE}}", self.title)
            .replace("{{MODULE}}", self.module)
            .replace("{{DIST}}", self.distribution)
            .replace("{{SCHEMA}}", SCHEMA_URL)
        )

    def _write(self, relative: str, text: str) -> None:
        target = self.directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._fill(text))

    def _page(self, ui: str, body: str, on_hello: str) -> None:
        self._write(f"{ui}/index.html", _PAGE.replace("{{BODY}}", body))
        self._write(f"{ui}/main.js", _BRIDGE.replace("{{ON_HELLO}}", on_hello))

    # ── the two scaffolds ─────────────────────────────────────────

    def write_dev(self) -> list[str]:
        manifest = {
            "$schema": SCHEMA_URL,
            "id": self.id,
            "version": "0.1.0",
            "contract": ">=1.0 <2",
            "name": self.title,
            "trust": "unsigned",
            # The interpreter igni runs on: it has the mcp package
            # server.py imports, and a bare "python" is on no PATH on a
            # stock Mac. Point it at your own environment's once
            # server.py needs more than mcp.
            "dev": {"command": [sys.executable, "server.py"]},
            "permissions": {"rpc": [f"ext.{self.id}.*"]},
            "contributes": {
                "pages": [{"kind": f"{self.id}.main", "label": self.title[:24], "sidebar": True}]
            },
        }
        self._write("manifest.json", json.dumps(manifest, indent=2) + "\n")
        self._write("server.py", _DEV_SERVER)
        self._page("ui", _DEV_BODY, _DEV_ON_HELLO)
        self._write("README.md", _DEV_README)
        return ["manifest.json", "server.py", "ui/index.html", "ui/main.js", "README.md"]

    def write_service(self) -> list[str]:
        tool = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        }
        manifest = {
            "$schema": SCHEMA_URL,
            "id": self.id,
            "version": "0.1.0",
            "contract": ">=1.0 <2",
            "name": self.title,
            "description": "Look up a support ticket by its id.",
            "vendor": "You",
            "trust": "partner",
            "python": {
                "package": self.distribution,
                "entry": self.distribution,
                "wheels": "wheels",
                "mcp": {"transport": "stdio"},
            },
            "permissions": {
                "workspace": "none",
                "tools": [
                    {
                        "name": f"{self.id}.lookup_ticket",
                        "annotations": {**tool, "openWorldHint": False},
                    },
                    {
                        "name": f"{self.id}.ticket_from_service",
                        "annotations": {**tool, "openWorldHint": True},
                    },
                ],
                "rpc": [f"ext.{self.id}.lookup_ticket"],
                "hooks": ["PostToolUse"],
            },
            "vm": {"memoryMib": 2048, "packages": {"apt": ["jq"]}},
            "contributes": {
                "pages": [{"kind": self.id, "label": self.title[:24], "sidebar": True}]
            },
        }
        self._write("extension/manifest.json", json.dumps(manifest, indent=2) + "\n")
        self._write("extension/pyproject.toml", _SERVICE_EXTENSION_PYPROJECT)
        self._write("extension/README.md", _SERVICE_EXTENSION_README)
        self._write(f"extension/src/{self.module}/__init__.py", _SERVICE_EXTENSION)
        self._page("extension/ui", _SERVICE_BODY, _SERVICE_ON_HELLO)
        self._write("service/service.py", _SERVICE_APP)
        self._write("tests/test_extension_and_service.py", _SERVICE_TESTS)
        self._write("pyproject.toml", _SERVICE_WORKSPACE)
        self._write("README.md", _SERVICE_README)
        return [
            "README.md",
            "pyproject.toml",
            "extension/manifest.json",
            "extension/pyproject.toml",
            "extension/README.md",
            f"extension/src/{self.module}/__init__.py",
            "extension/ui/index.html",
            "extension/ui/main.js",
            "service/service.py",
            "tests/test_extension_and_service.py",
        ]


# ── dev ───────────────────────────────────────────────────────────

_DEV_SERVER = '''"""This extension's backend.

An MCP server. igni calls its tools as ``ext.<id>.<tool>`` when this
extension's UI asks, and only the ones ``permissions.rpc`` grants.
"""

import json

from mcp.server.mcpserver import MCPServer

server = MCPServer("extension")


@server.tool()
def hello() -> str:
    """Whatever your UI needs. Return JSON and it arrives as data."""
    return json.dumps({"message": "hello from the extension backend"})


if __name__ == "__main__":
    server.run()
'''

_DEV_BODY = """    <div id="root">Loading…</div>"""

_DEV_ON_HELLO = """function render(text) {
  document.getElementById("root").textContent = text;
}

function onHello() {
  call("ext.{{ID}}.hello").then(
    (value) => render(value.message),
    (error) => render("could not reach the backend: " + error.message),
  );
}"""

_DEV_README = """# {{ID}}

An igni extension, scaffolded by `igni ext init --dev`.

```
manifest.json   what it is and what it may do
server.py       its backend, an MCP server
ui/index.html   the page igni frames
ui/main.js      the page's script (a file: the page's CSP refuses inline scripts)
```

`dev.command` runs `server.py` with the Python igni itself runs on,
which has the `mcp` package. Point it at your own environment's
`python` once `server.py` needs more.

## Run it

```bash
igni ext doctor .                          # check it, and start it once
IGNI_DEV_EXTENSIONS=1 igni ext install .   # a development install
```

Then quit the igni app (or the `igni` backend it started) and start it
again with `IGNI_DEV_EXTENSIONS=1` in its environment: a development
install starts when igni's backend boots, and does not run without the
flag. The page appears in the sidebar. A `igni -m "…"` run starts it
too, with the same flag.

## What to change first

- **`permissions.rpc`** decides which backend methods your UI may
  call. It is enforced by the backend against this manifest, not by
  the frame — narrow it and your own panel stops working, and the
  refusal will tell you which line to add.
- **`server.py`** is where your data comes from. A tool that returns
  JSON arrives in the UI as data rather than a string.
- **`ui/main.js`** inlines a minimal bridge so this has no build
  step. For anything real, use `@ignite-ember/ui-react` and
  `@ignite-ember/extension-bridge` — see `examples/reference-extension`.

## Before anybody else installs it

Nobody else installs it from here: `dev` packaging runs only on this
machine. An extension reaches people's machines from its service,
through igni server — `igni ext init --service` scaffolds that shape,
and igni's `docs/YOUR_FIRST_EXTENSION.md` walks through it. Once the
service is running, two people decide: a **super admin** approves the
service in igni's portal (Services), and an **organisation admin**
switches the extension on for a group (Extensions). Then everyone in
that group gets it at their next sync — `igni ext sync`, or the igni
app's start-up.
"""

# ── service ───────────────────────────────────────────────────────

_SERVICE_EXTENSION_PYPROJECT = """[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{{DIST}}"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["igni-extension"]

[project.scripts]
{{DIST}} = "{{MODULE}}:main"

[tool.hatch.build.targets.wheel]
packages = ["src/{{MODULE}}"]
"""

_SERVICE_EXTENSION_README = """# {{TITLE}}

Two tools — `lookup_ticket`, answering from canned tickets, and
`ticket_from_service`, asking this extension's service as the signed-in
person — a `PostToolUse` hook that keeps an audit line per tool call,
and a page that calls `lookup_ticket`.
"""

_SERVICE_EXTENSION = '''"""{{TITLE}}: two tools, one hook, and a page that calls a tool.

The agent sees each tool as ``{{ID}}.<tool>`` in the manifest's
``permissions.tools``; the page calls it as ``ext.{{ID}}.<tool>``.
"""

import json
from pathlib import Path

from igni_extension import Hooks, IgniExtension, NotSignedIn, ToolHints
from igni_extension.hooks import PostToolUse

ext = IgniExtension("{{ID}}")

READS = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)

TICKETS = {
    "T-1001": {"id": "T-1001", "title": "VPN drops every hour", "status": "open"},
    "T-1002": {"id": "T-1002", "title": "Printer on 3rd floor offline", "status": "closed"},
}


@ext.tool(hints=READS)
async def lookup_ticket(ticket_id: str) -> str:
    """Look up a support ticket by id (e.g. T-1001). Returns JSON."""
    ticket = TICKETS.get(ticket_id.upper())
    if ticket is None:
        return json.dumps({"error": f"no ticket {ticket_id}", "known": sorted(TICKETS)})
    return json.dumps(ticket)


@ext.tool(hints=ToolHints(read_only=True, destructive=False, idempotent=True, open_world=True))
async def ticket_from_service(ticket_id: str) -> str:
    """Look a ticket up on this extension's service, as the signed-in person."""
    try:
        response = await ext.service.get(f"/v1/tickets/{ticket_id}")
    except NotSignedIn:
        return "Sign in to igni to reach the ticket service."
    response.raise_for_status()
    return response.text


@ext.hook(Hooks.POST_TOOL_USE)
async def audit(call: PostToolUse) -> None:
    """Record every tool the agent ran in this extension's data directory."""
    try:
        log = Path(ext.environment.data_dir) / "tool-audit.log"
        with log.open("a") as f:
            f.write(f"{call.tool_name}\\n")
    except Exception:  # never hold the agent up over an audit line
        pass
    return None


def main() -> None:
    ext.run()
'''

_SERVICE_BODY = """    <form id="lookup"><input id="ticket" value="T-1001" /> <button>Look up</button></form>
    <pre id="out">Loading…</pre>"""

_SERVICE_ON_HELLO = """const out = document.getElementById("out");

function lookUp() {
  call("ext.{{ID}}.lookup_ticket", { ticket_id: document.getElementById("ticket").value }).then(
    (value) => (out.textContent = JSON.stringify(value, null, 2)),
    (error) => (out.textContent = "error: " + error.message),
  );
}

function onHello() {
  lookUp();
}

document.getElementById("lookup").addEventListener("submit", (event) => {
  event.preventDefault();
  lookUp();
});"""

_SERVICE_APP = '''"""{{TITLE}}'s service.

Registers with igni server, ships the extension bundle, answers erasure
and export, and reports what became of the extension's VM profile
request.

    uv run uvicorn --factory service:create_app --app-dir service --port 8201
"""

from fastapi import Depends, FastAPI

from igni_service import ExportSection, IgniService, IgniSettings, Person, Subject


def create_app(igni: IgniService | None = None) -> FastAPI:
    """The app; ``igni`` is a FakeIgni's service in tests, the real one otherwise."""
    igni = igni or IgniService.from_settings(IgniSettings())
    app = FastAPI(lifespan=igni.lifespan)
    igni.install(app)

    # What this service keeps about people: who looked which ticket up.
    # A real service keeps it in its database.
    lookups: dict[tuple[str, str], list[str]] = {}

    @igni.on_erase
    async def erase(subject: Subject) -> None:
        lookups.pop((subject.org_id, subject.user_id), None)

    @igni.on_export
    async def export(subject: Subject) -> list[ExportSection]:
        rows = [{"ticket": t} for t in lookups.get((subject.org_id, subject.user_id), [])]
        return [ExportSection(name="Ticket lookups", rows=rows)]

    @app.get("/v1/tickets/{ticket_id}")
    async def ticket(ticket_id: str, person: Person = Depends(igni.person)) -> dict:
        lookups.setdefault((person.org_id, person.user_id), []).append(ticket_id)
        return {"id": ticket_id, "looked_up_by": person.email}

    @app.get("/v1/vm-profile")
    async def vm_profile(person: Person = Depends(igni.org_admin)) -> dict:
        """What became of the extension's ``vm`` request in the caller's organisation."""
        status = await igni.host(person.org_id).vm_profile()
        return status.model_dump(mode="json")

    return app
'''

_SERVICE_TESTS = '''"""The extension and its service, on the SDKs' fakes — no igni needed."""

import json

import httpx
import pytest
from igni_extension import Caller, CallerKind
from igni_extension.testing import ConcurrencyHarness, ConcurrentCall, HostHarness
from igni_service.testing import FakeIgni

from {{MODULE}} import ext
from service import create_app

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


ADA = Caller(kind=CallerKind.PERSON, id="u-ada", org="org-1", group="g-1")
GRACE = Caller(kind=CallerKind.PERSON, id="u-grace", org="org-1", group="g-1")


async def test_the_tool_answers_as_igni_calls_it() -> None:
    async with HostHarness(ext) as host:
        result = await host.call("lookup_ticket", {"ticket_id": "T-1001"}, caller=ADA)
    assert json.loads(result.content[0].text)["title"] == "VPN drops every hour"


async def test_many_callers_at_once() -> None:
    report = await ConcurrencyHarness(ext).run(
        [
            ConcurrentCall(tool="lookup_ticket", arguments={"ticket_id": "T-1001"}, caller=ADA),
            ConcurrentCall(tool="lookup_ticket", arguments={"ticket_id": "T-1002"}, caller=GRACE),
        ]
    )
    assert report.results[0].result != report.results[1].result


async def test_the_service_takes_a_person_and_answers_export_and_erasure() -> None:
    fake = FakeIgni(service_url="http://tickets.test")
    fake.add_member("org-1", "u-ada", email="ada@acme.test", name="Ada")
    app = create_app(fake.service())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tickets.test") as http:
        person = {"Authorization": f"Bearer {fake.person_token('u-ada', org_id='org-1')}"}
        r = await http.get("/v1/tickets/T-1001", headers=person)
        assert r.status_code == 200, r.text
        assert r.json()["looked_up_by"] == "ada@acme.test"

        igni = {"Authorization": f"Bearer {fake.igni_token('org-1')}"}
        r = await http.get("/v1/igni/subjects/u-ada/export", headers=igni)
        assert r.status_code == 200, r.text
        assert "T-1001" in r.text
        r = await http.delete("/v1/igni/subjects/u-ada", headers=igni)
        assert r.status_code in (200, 204), r.text
        r = await http.get("/v1/igni/subjects/u-ada/export", headers=igni)
        assert "T-1001" not in r.text
'''

_SERVICE_WORKSPACE = """# The development environment for the extension and its service:
# both SDKs, the service's server, and the tests. Not a package.
#
#   uv sync
#   uv run pytest
#
# Until igni-extension and igni-service are on PyPI, build their wheels
# from an igni checkout and add `--find-links <that directory>` to
# `uv sync` (igni's docs/YOUR_FIRST_EXTENSION.md, step 0).
[project]
name = "{{DIST}}-workspace"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "igni-extension",
    "igni-service",
    "uvicorn",
    "aiosqlite",
    "httpx",
    "pytest",
    "anyio",
]

[tool.uv]
package = false

[tool.pytest.ini_options]
pythonpath = ["service", "extension/src"]
testpaths = ["tests"]
"""

_SERVICE_README = """# {{TITLE}}

An igni extension and the service that ships it, scaffolded by
`igni ext init --service`. igni's `docs/YOUR_FIRST_EXTENSION.md` walks
through every step below.

```
extension/                     what runs on each person's machine
  manifest.json                what it is and what it may do
  pyproject.toml               the Python package igni installs
  src/{{MODULE}}/      two tools and a PostToolUse hook
  ui/index.html, ui/main.js    the page igni frames
service/service.py             the service: registers, ships the bundle, export/erasure
tests/                         both, on the SDKs' fakes
pyproject.toml                 the development environment
```

## Test it

```bash
uv sync          # until the SDKs are on PyPI: uv sync --find-links <sdk wheels>
uv run pytest
igni ext doctor extension
```

## Ship it

```bash
uv run igni-service-bundle --extension extension --out service/extension-bundle.tar.gz
export IGNI_KEY_ENCRYPTION_KEY=$(uv run python -c "from igni_service import KeyCipher; print(KeyCipher.generate())")
IGNI_SERVER_URL=http://localhost:8000 PUBLIC_BASE_URL=http://localhost:8201 \\
  SERVICE_NAME="{{TITLE}}" IGNI_KEY_DATABASE_URL=sqlite+aiosqlite:///service/keys.db \\
  IGNI_EXTENSION_BUNDLE=service/extension-bundle.tar.gz \\
  uv run uvicorn --factory service:create_app --app-dir service --port 8201
```

Then two people decide, in igni's portal:

1. a **super admin** approves the service (Services), after checking the
   thumbprint the service printed is the one shown there;
2. an **organisation admin** switches the extension on for a group
   (Extensions).

Approval alone installs it nowhere. Once both are done, everyone in that
group gets it at their next sync: `igni ext sync`, an `igni -m` run, or
the igni app's start-up.
"""
