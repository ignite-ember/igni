"""``igni ext`` — build, check and install extensions.

EXTENSIBILITY §9 has promised ``igni ext init`` and ``igni ext
doctor`` for as long as there has been an extension system, and
neither existed. Installing meant calling ``ExtensionInstaller`` from
Python, which is not a thing an administrator does and not a thing a
partner can be handed.

Everything here works against the **store** rather than a running
backend. An install is a decision that outlives a process: the bundle
is copied under ``<data>/extensions/<id>/`` and the backend reads
that directory at boot. The alternative — a CLI that talks to a live
backend over RPC — would mean nothing could be installed before one
was running, and that an install vanished when it stopped.

``doctor`` reads and never writes, which is what makes it safe to
tell somebody to run first.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from ember_code.extensions.manifest import ExtensionManifest, Trust, Workspace
from ember_code.extensions.store import ExtensionStore, StoreError

#: What this build of igni implements. An extension declares a range
#: and this is what it is checked against — the only compatibility
#: axis, and never an igni version.
CONTRACT_VERSION = "1.0"


@click.group("ext")
def ext() -> None:
    """Build, check and install extensions."""


def _store(root: str | None) -> ExtensionStore:
    return ExtensionStore(Path(root).expanduser() if root else None)


@ext.command("doctor")
@click.argument(
    "bundle",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
)
def doctor(bundle: Path) -> None:
    """Check a bundle and say what is wrong with it.

    Reads and never writes, so it is safe to run first and safe to
    run on somebody else's directory.
    """
    click.echo(f"contract implemented by this igni: {CONTRACT_VERSION}")

    try:
        manifest = ExtensionStore().read(bundle)
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"{manifest.display_name} ({manifest.id} {manifest.version})")
    click.echo(f"  contract   {manifest.contract}")
    click.echo(f"  trust      {manifest.trust.value}")

    packaging = "dev" if manifest.dev else "sidecar" if manifest.sidecar else "endpoint"
    click.echo(f"  packaging  {packaging}")

    # Two lists, because they are not the same question. A *problem*
    # stops this working; a *note* is something to know. The
    # distinction matters more than it looks: the first version
    # reported an unsigned development bundle as a problem, so
    # ``ext doctor`` failed on the output of ``ext init`` — teaching,
    # as the very first thing an author sees, that the tooling
    # contradicts itself. Development packaging *requires* unsigned.
    problems: list[str] = []
    notes: list[str] = []

    if manifest.trust is Trust.UNSIGNED:
        if manifest.dev is not None:
            notes.append(
                "unsigned, which development packaging requires. A deployment "
                "that has not enabled development mode will refuse it, so sign "
                "it before anybody else installs it."
            )
        else:
            problems.append(
                "unsigned — a deployment that has not enabled development mode will refuse it"
            )
    elif not manifest.signature:
        problems.append(f"trust is {manifest.trust.value} and there is no signature")

    if manifest.sidecar is not None:
        problems.append(
            f"packaged as a sidecar image ({manifest.sidecar.image}); igni "
            "cannot start container images yet. Use 'endpoint' or 'dev'."
        )

    if manifest.contributes.pages and not (bundle / "ui").is_dir():
        problems.append(
            "contributes pages and the bundle has no 'ui' directory, so there is nothing to serve"
        )

    if manifest.endpoint is not None:
        host_declared = manifest.permissions.egress
        if not host_declared:
            problems.append(
                "has an endpoint and declares no egress, so igni will refuse to dial it"
            )
        # igni sets an environment for a process it starts, and an
        # endpoint is somebody else's process. A grant that cannot be
        # delivered is the failure this system keeps removing.
        if manifest.permissions.workspace is not Workspace.NONE:
            problems.append(
                f"declares workspace: {manifest.permissions.workspace.value} and "
                "is an endpoint, so the grant could never be delivered"
            )
        if manifest.permissions.secrets:
            problems.append(
                f"declares secrets {list(manifest.permissions.secrets)} and is an "
                "endpoint, so they could never be delivered"
            )

    if manifest.sidecar is not None or manifest.dev is not None:
        notes.append(
            "runs as a process on the machine igni is on, so it installs only on "
            "a single-user backend. One process cannot serve a group: there is "
            "one workspace and one resolution of each secret, and no answer to "
            "whose."
        )
    if manifest.contributes.pages:
        notes.append(
            "contributes pages, which only a single-user backend shows — a frame "
            "is hosted by the igni app beside the user."
        )

    if manifest.dev is not None:
        command = Path(manifest.dev.command[0])
        looks_local = command.parts and command.parts[0] in {".", ".."}
        if looks_local and not (bundle / command).exists():
            problems.append(f"its dev command {command} is not in the bundle")

    # Reported rather than refused. A declared permission nobody
    # enforces used to be this system's defining bug, so a grant that
    # is wider than it looks is worth a line even when it is legal.
    if any(grant == "*" for grant in manifest.permissions.rpc):
        problems.append("permissions.rpc contains '*', which grants every method")

    if notes:
        click.echo()
        for note in notes:
            click.echo(f"  - {note}")

    if problems:
        click.echo()
        for problem in problems:
            click.echo(f"  ! {problem}")
        raise click.ClickException(
            f"{len(problems)} problem(s) found — it will not install as it is"
        )

    click.echo()
    click.echo("  no problems found")


@ext.command("install")
@click.argument("bundle", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--replace",
    is_flag=True,
    help="Upgrade an already-installed extension. Re-asks for its permissions.",
)
@click.option("--root", default=None, help="Install somewhere other than the default.")
def install(bundle: Path, replace: bool, root: str | None) -> None:
    """Install a bundle into this deployment.

    Copies it into the store. The backend starts it at its next boot
    — this command does not reach into a running one, so a backend
    that is up will not see it until it restarts.
    """
    store = _store(root)
    try:
        manifest = store.read(bundle)
        target = store.add(bundle, replace=replace)
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"installed {manifest.display_name} ({manifest.id}) to {target}")
    _echo_grants(manifest)
    click.echo()
    click.echo("Restart igni for it to start.")


def _echo_grants(manifest: ExtensionManifest) -> None:
    """Print what this extension was granted.

    Shown on install because that is the moment an administrator is
    deciding, and because a permission nobody reads is the same as a
    permission nobody enforces.
    """
    permissions = manifest.permissions
    granted = [
        ("workspace", permissions.workspace.value),
        ("workspace paths", ", ".join(permissions.workspacePaths) or "all"),
        ("egress", ", ".join(permissions.egress) or "none"),
        ("secrets", ", ".join(permissions.secrets) or "none"),
        ("agent tools", ", ".join(permissions.tools) or "none"),
        ("backend methods", ", ".join(permissions.rpc) or "none"),
    ]
    click.echo()
    click.echo("  it may use:")
    for label, value in granted:
        click.echo(f"    {label:<16} {value}")


@ext.command("list")
@click.option("--root", default=None, help="Read somewhere other than the default.")
def list_installed(root: str | None) -> None:
    """What is installed in this deployment."""
    store = _store(root)
    bundles = store.installed()
    if not bundles:
        click.echo(f"nothing installed in {store.root}")
        return
    for bundle in bundles:
        try:
            manifest = store.read(bundle)
        except StoreError as exc:
            click.echo(f"{bundle.name:<28} unreadable — {exc}")
            continue
        click.echo(f"{manifest.id:<28} {manifest.version:<10} {manifest.trust.value}")


@ext.command("remove")
@click.argument("extension_id")
@click.option("--root", default=None, help="Read somewhere other than the default.")
def remove(extension_id: str, root: str | None) -> None:
    """Uninstall an extension from this deployment."""
    store = _store(root)
    try:
        removed = store.remove(extension_id)
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc
    if not removed:
        raise click.ClickException(f"{extension_id} is not installed")
    click.echo(f"removed {extension_id}")
    click.echo("Restart igni for it to stop.")


@ext.command("init")
@click.argument("directory", type=click.Path(path_type=Path))
@click.option("--id", "extension_id", default=None, help="Reverse-DNS id.")
def init(directory: Path, extension_id: str | None) -> None:
    """Scaffold an extension that runs.

    Development packaging, because the other two need something an
    author does not have on a laptop: a container registry, or a
    deployed service. What this writes installs and starts as-is —
    a scaffold that needs edits before it works is a scaffold that
    teaches nothing about whether the install path is sound.
    """
    if directory.exists() and any(directory.iterdir()):
        raise click.ClickException(f"{directory} is not empty")

    name = extension_id or f"local.{directory.resolve().name.replace('_', '-')}"
    (directory / "ui").mkdir(parents=True, exist_ok=True)

    manifest = {
        "$schema": "https://igni.sh/schema/extension-manifest-1.json",
        "id": name,
        "version": "0.1.0",
        "contract": f">={CONTRACT_VERSION} <2",
        "name": directory.resolve().name,
        "trust": "unsigned",
        "dev": {"command": ["python", "server.py"]},
        "permissions": {"rpc": [f"ext.{name}.*"]},
        "contributes": {
            "pages": [
                {"kind": f"{name}.main", "label": directory.resolve().name[:24], "sidebar": True}
            ]
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (directory / "server.py").write_text(_SERVER)
    (directory / "ui" / "index.html").write_text(_PAGE.replace("{{ID}}", name))
    (directory / "README.md").write_text(_README.replace("{{ID}}", name))

    click.echo(f"created {directory}")
    click.echo()
    click.echo(f"  igni ext doctor {directory}")
    click.echo(f"  igni ext install {directory}")


_SERVER = '''"""This extension's backend.

An MCP server. igni calls its tools as ``ext.<id>.<tool>`` when this
extension's UI asks, and only the ones ``permissions.rpc`` grants.
"""

import json

from mcp.server.fastmcp import FastMCP

server = FastMCP("extension")


@server.tool()
def hello() -> str:
    """Whatever your UI needs. Return JSON and it arrives as data."""
    return json.dumps({"message": "hello from the extension backend"})


if __name__ == "__main__":
    server.run()
'''

_PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Extension</title>
  </head>
  <body class="igni-root">
    <div id="root">Loading…</div>
    <script type="module">
      // The bridge, inlined so this scaffold has no build step. A
      // real extension imports @igni/extension-bridge and gets this
      // with types — see examples/reference-extension.
      const host = window.parent;
      let nonce = null;
      let next = 0;
      const waiting = new Map();

      window.addEventListener("message", (event) => {
        const message = event.data;
        if (event.source !== host || message?.igni !== 1) return;
        if (message.kind === "hello") {
          nonce = message.nonce;
          call("ext.{{ID}}.hello").then(
            (value) => render(value.message),
            (error) => render("could not reach the backend: " + error.message),
          );
          return;
        }
        if (message.kind === "result") {
          const waiter = waiting.get(message.id);
          if (!waiter) return;
          waiting.delete(message.id);
          message.ok ? waiter.resolve(message.value) : waiter.reject(new Error(message.error));
        }
      });

      function call(method, args = {}) {
        const id = String(++next);
        return new Promise((resolve, reject) => {
          waiting.set(id, { resolve, reject });
          host.postMessage({ igni: 1, kind: "call", nonce, id, method, args }, "*");
        });
      }

      function render(text) {
        document.getElementById("root").textContent = text;
      }
    </script>
  </body>
</html>
"""

_README = """# {{ID}}

An igni extension, scaffolded by `igni ext init`.

```
manifest.json   what it is and what it may do
server.py       its backend, an MCP server
ui/index.html   the page igni frames
```

## Run it

```bash
igni ext doctor .     # check it
igni ext install .    # copy it into this deployment
```

Then restart igni. The page appears in the sidebar.

## What to change first

- **`permissions.rpc`** decides which backend methods your UI may
  call. It is enforced by the backend against this manifest, not by
  the frame — narrow it and your own panel stops working, and the
  refusal will tell you which line to add.
- **`server.py`** is where your data comes from. A tool that returns
  JSON arrives in the UI as data rather than a string.
- **`ui/index.html`** inlines a minimal bridge so this has no build
  step. For anything real, use `@igni/ui-react` and
  `@igni/extension-bridge` — see `examples/reference-extension`.

## Before anybody else installs it

`trust: unsigned` is development only, and a deployment that has not
enabled development mode will refuse it. Sign the bundle and set
`trust` to `partner`.
"""
