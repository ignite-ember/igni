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
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click

from ember_code.core.plugins.git import GitClient, GitError
from ember_code.core.plugins.marketplace_store import MarketplaceRegistryStore
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


#: What we treat as a repository rather than a directory. Checked
#: before the filesystem, so a relative path that happens to contain
#: a colon is still a path and a URL that happens to exist on disk
#: is still a URL.
#:
#: ``file://`` is here on purpose: it is how an air-gapped
#: deployment installs from a mirror it already has, and it is what
#: makes the clone path testable without a network.
_REPOSITORY = ("http://", "https://", "git@", "ssh://", "git://", "file://")


def _looks_like_sha(ref: str) -> bool:
    """Whether ``ref`` is a commit rather than a branch or a tag.

    Errs towards "branch": a seven-to-forty-character hex string is
    a commit, and anything else is passed to ``git clone --branch``,
    which is where a wrong guess produces a clear error rather than
    a silent default-branch checkout.
    """
    if not 7 <= len(ref) <= 40:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in ref)


@contextmanager
def _bundle_from(source: str, *, ref: str | None, subdir: str | None) -> Iterator[Path]:
    """A bundle directory, cloning first when ``source`` is a repo.

    The clone goes to a temporary directory and is deleted on the
    way out: the store keeps its own copy, so a second one under
    ``~/.igni`` would be a stale checkout nobody updates and
    everybody finds.
    """
    marketplace = MarketplaceRegistryStore().resolve_extension_ref(source)
    if marketplace is not None:
        resolved, row = marketplace
        click.echo(f"{row.name} — {row.description or 'from the catalog'}")
        # The catalog's own ref wins only when the caller gave
        # none: somebody typing `--ref` has said something more
        # specific than the listing did.
        with _bundle_from(
            resolved.url, ref=ref or resolved.ref, subdir=subdir or resolved.subdir
        ) as bundle:
            yield bundle
        return
    if source.startswith("@"):
        raise click.ClickException(
            f"{source} does not name an extension in any registered "
            "marketplace. `igni ext marketplace list` shows what is "
            "registered, and `igni ext marketplace add <url>` adds one."
        )

    if not source.startswith(_REPOSITORY):
        bundle = Path(source)
        if not bundle.is_dir():
            raise click.ClickException(
                f"{source} is not a directory, and does not look like a repository URL either."
            )
        if ref or subdir:
            raise click.ClickException(
                f"--ref and --subdir are for installing from a repository; {source} is a directory."
            )
        yield bundle
        return

    git = GitClient()
    if not git.is_available():
        raise click.ClickException(
            "git is not on PATH, so a repository cannot be cloned. Clone it "
            "yourself and pass the directory."
        )
    with tempfile.TemporaryDirectory(prefix="igni-ext-") as tmp:
        checkout = Path(tmp) / "bundle"
        click.echo(f"cloning {source}{f' at {ref}' if ref else ''} …")
        try:
            # A commit cannot be given to ``git clone --branch`` on
            # older gits, so a SHA is cloned-then-checked-out — the
            # same split ``PluginInstaller`` makes, because ``--ref``
            # takes whatever a person would think to type.
            sha = _looks_like_sha(ref) if ref else False
            # A shallow clone holds only the tip, so checking out an
            # older commit fails with "unable to read tree" — which
            # reads like a corrupt repository rather than like the
            # depth it is. Full history only when a commit was
            # asked for; a branch, a tag and "latest" stay shallow.
            git.clone(source, checkout, ref=None if sha else ref, shallow=not sha)
            if ref and sha:
                git.checkout(checkout, ref)
            commit = git.current_sha(checkout)
        except GitError as exc:
            raise click.ClickException(f"cloning {source} failed: {exc}") from exc

        # What was taken, as a fact rather than as advice. Whether to
        # pin is the installer's call: "the latest" is a legitimate
        # answer, and this is the commit it meant today.
        click.echo(f"  at {commit}")

        bundle = checkout / subdir if subdir else checkout
        if not bundle.is_dir():
            raise click.ClickException(f"{subdir!r} is not a directory in {source}.")
        yield bundle


@ext.command("install")
@click.argument("source")
@click.option(
    "--replace",
    is_flag=True,
    help="Upgrade an already-installed extension. Re-asks for its permissions.",
)
@click.option(
    "--ref",
    default=None,
    help=(
        "Branch, tag or commit to install from, when SOURCE is a repository. "
        "Without it you get the default branch as it stands now, which is a "
        "choice rather than an oversight."
    ),
)
@click.option(
    "--subdir",
    default=None,
    help="The bundle's directory inside the repository, when it is not the root.",
)
@click.option("--root", default=None, help="Install somewhere other than the default.")
def install(
    source: str, replace: bool, ref: str | None, subdir: str | None, root: str | None
) -> None:
    """Install a bundle into this deployment.

    SOURCE is a marketplace ref (``@acme/scanner``), a directory, or
    a repository URL — the same three things ``igni plugin install``
    takes, and deliberately so. A plugin is
    shared as a repository because that is how people share things,
    and an extension that could only be installed from a directory
    somebody already had was an extension nobody could hand you.

    A clone is a bundle like any other: the signature is over the
    bundle digest, so ``trust: signed`` is verified exactly as it is
    for a directory, and an unsigned one is refused in the same
    place. Pin with ``--ref`` — an extension is a process, which is
    more than a plugin is, so installing whatever ``main`` says
    today is worth less here than it is there.

    An air-gapped deployment clones nothing and passes a directory,
    which is why the directory form stays rather than being replaced.

    Copies it into the store. The backend starts it at its next boot
    — this command does not reach into a running one, so a backend
    that is up will not see it until it restarts.
    """
    store = _store(root)
    with _bundle_from(source, ref=ref, subdir=subdir) as bundle:
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


@ext.group("marketplace")
def marketplace() -> None:
    """Catalogs of extensions you can install by name.

    **The same registry ``igni plugin marketplace`` uses**, and the
    same ``.claude-plugin/marketplace.json`` file — igni reads an
    ``extensions`` array beside the ``plugins`` one. A catalog can
    offer either or both, a Claude Code marketplace still parses,
    and adding one here makes its plugins available there too.

    Two doors, one room. The commands are duplicated because
    somebody installing an extension should not have to know that
    the registry lives under ``plugin``; the registry is not.

    Self-hosted: a marketplace is a git repository you control.
    There is no index igni publishes and nothing to submit to.
    """


def _marketplace_store() -> MarketplaceRegistryStore:
    return MarketplaceRegistryStore()


@marketplace.command("add")
@click.argument("url")
def marketplace_add(url: str) -> None:
    """Register the catalog at URL."""
    try:
        entry = _marketplace_store().add(url)
    except Exception as exc:
        raise click.ClickException(f"adding {url} failed: {exc}") from exc
    catalog = entry.cached
    extensions = len(catalog.extensions) if catalog else 0
    plugins = len(catalog.plugins) if catalog else 0
    click.echo(f"added {entry.name} — {extensions} extension(s), {plugins} plugin(s)")


@marketplace.command("remove")
@click.argument("name")
def marketplace_remove(name: str) -> None:
    """Forget a registered catalog. Installed extensions stay."""
    if not _marketplace_store().remove(name):
        raise click.ClickException(f"no marketplace named {name!r} is registered")
    click.echo(f"removed {name}")


@marketplace.command("refresh")
@click.argument("name", required=False)
def marketplace_refresh(name: str | None) -> None:
    """Re-fetch one catalog, or every one."""
    store = _marketplace_store()
    if name:
        if store.refresh(name) is None:
            raise click.ClickException(f"no marketplace named {name!r} is registered")
        click.echo(f"refreshed {name}")
        return
    outcomes = store.refresh_all()
    if not outcomes:
        click.echo("no marketplaces registered")
        return
    for outcome in outcomes:
        click.echo(f"  {'ok  ' if outcome.ok else 'fail'} {outcome.name} {outcome.reason}")


@marketplace.command("list")
def marketplace_list() -> None:
    """What is registered, and what each one offers.

    Extensions only. A catalog's plugins are listed by ``igni
    plugin marketplace list``, which is the command somebody
    looking for a plugin would reach for.
    """
    registry = _marketplace_store().load()
    if not registry.marketplaces:
        click.echo("no marketplaces registered")
        click.echo()
        click.echo("  igni ext marketplace add <git-url>")
        return
    for entry in registry.marketplaces:
        catalog = entry.cached
        click.echo(f"{entry.name}  {entry.url}")
        if catalog is None:
            click.echo("    not fetched yet — igni ext marketplace refresh")
            continue
        if not catalog.extensions:
            click.echo("    no extensions (it may still offer plugins)")
            continue
        for row in catalog.extensions:
            summary = f" — {row.description}" if row.description else ""
            click.echo(f"    @{entry.name}/{row.name}{summary}")
