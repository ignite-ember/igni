"""Group policy pack — fetched from igni server, cached on disk, applied as a settings tier."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, model_validator

from ember_code.core.config.group_policy_signature import (
    PackRejected,
    jwks_url_for,
    may_trust_unpinned_jwks_from,
    unverified_claims,
    verify_pack,
)
from ember_code.core.paths import CONFIG_DIR, home_config_dir

if TYPE_CHECKING:
    from ember_code.core.plugins.installer import PluginError, PluginInstaller
else:
    PluginInstaller = Any  # type: ignore[misc,assignment]
    PluginError = Exception

logger = logging.getLogger(__name__)

# Cache TTL: 5 minutes. Group policies don't change often enough to need
# sub-minute freshness; a short TTL keeps stale admin pushes from lasting forever.
_CACHE_TTL_SECONDS = 300

#: kind → (directory under the cache, filename for one entry). The
#: directories are the ones each loader is told to scan; the filenames
#: are the conventions those loaders already expect — a skill is a
#: directory with SKILL.md in it, a workflow is an ES module.
_PLAIN_KINDS: dict[str, tuple[str, str]] = {
    "agents": ("agents", "{name}.md"),
    "skills": ("skills", "{name}/SKILL.md"),
    "commands": ("commands", "{name}.md"),
    "workflows": ("workflows", "{name}.mjs"),
    "rules": ("rules", "{name}.md"),
    "output-styles": ("output-styles", "{name}.md"),
    "tools": ("tools", "{name}.py"),
    "scripts": ("scripts", "{name}.sh"),
}

#: Written with the executable bit set. A hook command runs through
#: ``bash -c``; a script arriving mode 0644 fails with 127, which the
#: hook runner treats as non-blocking — a silent no-op on every matching
#: tool call rather than an error anyone sees.
_EXECUTABLE_KINDS: frozenset[str] = frozenset({"scripts"})

#: What a group's hook writes to reach a script the same group ships.
#: Expanded here, because only this side knows where the pack landed.
GROUP_SCRIPTS_PLACEHOLDER = "{group_scripts}"

#: What a stale file of each kind looks like, for pruning.
_SUFFIX: dict[str, str] = {
    "agents": ".md",
    "commands": ".md",
    "workflows": ".mjs",
    "rules": ".md",
    "output-styles": ".md",
    "tools": ".py",
    "mcps": ".json",
    "scripts": ".sh",
}

# Same split :class:`AgentMarkdownFile` uses, so a model written here
# parses back out of the file the loader reads.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def _with_model(content: str, model: str | None) -> str:
    """Return ``content`` with ``model:`` set in its frontmatter.

    An agent's model is already a frontmatter key the loader honours
    (:attr:`AgentDefinition.model` → :meth:`AgentBuilder._resolve_model`),
    so the pack's per-agent model needs no new plumbing — it just has to
    reach the file the loader reads. A legal group can then send contract
    review to one adapter and case summaries to another.

    Content we cannot parse is returned untouched: it would fail to load
    as an agent anyway, and the loader reports that with the file name.
    """
    if not model:
        return content

    match = _FRONTMATTER_RE.match(content)
    if not match:
        logger.warning("Group agent has no YAML frontmatter; cannot set model %r", model)
        return content
    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        logger.warning("Group agent has unreadable frontmatter; cannot set model %r", model)
        return content
    if not isinstance(frontmatter, dict):
        return content

    frontmatter["model"] = model
    rendered = yaml.safe_dump(frontmatter, sort_keys=False).strip()
    return f"---\n{rendered}\n---\n\n{match.group(2).strip()}\n"


def _try_parse_json(text: str) -> Any:
    """Parse an entry's content as JSON if possible, else pass through.

    The BE serialises ``mcps`` entries as raw MCP server JSON (matching
    :class:`MCPServerConfig` fields), but some legacy entries store
    JSON-as-string already wrapped in ``{...}``. Both shapes should be
    accepted at load time.
    """
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


class GroupPolicyEntry(BaseModel):
    """One entry from a group pack.

    ``source_url`` / ``source_ref`` / ``source_subdir`` are an optional
    trio that mirrors :class:`MarketplacePluginEntry.resolved_source`.
    When ``kind == "plugins"`` and ``source_url`` is set, the policy
    cache treats the entry as "install this plugin from this git URL"
    (via :class:`PluginInstaller`) rather than "write this YAML file
    to disk". The cache also takes them on other kinds for forward
    compatibility.

    There is no ``extensions`` kind. An extension reaches a machine
    from its service through igni server (its ``docs/service-identity.md``
    §8), never from a group's policy; an entry of that kind is
    ignored with a warning.
    """

    kind: str  # agents | mcps | plugins | settings | hooks | …
    entry_name: str
    content: str
    content_type: str  # markdown | yaml | json
    enabled: bool = True

    # Agents only: the model (usually a LoRA adapter) this one runs
    # against. Written into the materialised file's frontmatter, where
    # the loader already reads it. Null inherits the session default.
    model: str | None = None

    # Plugin install source — same shape as marketplace. All three are
    # optional; if any is set, ``source_url`` must be too (half-filled
    # installs would fail in ``PluginInstaller.install`` anyway).
    source_url: str | None = None
    source_ref: str | None = None
    source_subdir: str | None = None

    @model_validator(mode="after")
    def _source_fields_are_complete(self) -> GroupPolicyEntry:
        """Reject half-filled plugin source specs.

        The ``git-subdir`` flow needs the parent URL; ``ref`` and
        ``subdir`` are meaningless without it. Empty strings are
        treated as absent — wire formats commonly serialize "no
        value" as ``""``, and ``is None`` alone would let an
        ``""`` + ``"main"`` slip past the guard.
        """
        url_present = bool(self.source_url and self.source_url.strip())
        ref_present = bool(self.source_ref and self.source_ref.strip())
        subdir_present = bool(self.source_subdir and self.source_subdir.strip())

        if (ref_present or subdir_present) and not url_present:
            raise ValueError("source_ref / source_subdir require source_url to be set")
        return self


class UnchangedPack:
    """The server's answer when the pack we hold is still current.

    A distinct type rather than ``None`` because the two mean opposite
    things to the caller: ``None`` is "I could not tell you", this is "I
    checked, and you are up to date". Conflating them would make a
    failed request look like a confirmation.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "PACK_UNCHANGED"


PACK_UNCHANGED = UnchangedPack()


class SignedAbsence:
    """igni server's signed answer "this person has no pack".

    Distinct from ``None`` ("could not tell you; keep what you have") and
    from :class:`UnchangedPack`: once its signature verifies, it means the
    cached policy and everything derived from it must go.
    """

    __slots__ = ("etag", "signed_pack")

    def __init__(self, signed_pack: str, etag: str | None = None) -> None:
        self.signed_pack = signed_pack
        self.etag = etag


class GroupPolicyPack(BaseModel):
    """Everything a group gives its people — fetched from igni server."""

    group_id: str
    group_name: str
    fetched_at: datetime
    entries: list[GroupPolicyEntry] = []

    # What this group's people get when nothing names a model. Routing
    # is resolved server-side, so this is carried for display and for
    # the frontmatter fallback rather than acted on here.
    default_model: str | None = None

    #: The server's tag for this pack's content. Sent back on the next
    #: poll so an unchanged pack costs a 304 and no payload.
    etag: str | None = None

    # Brand overrides for this group — accent colours, a wordmark, a
    # logo. Group-level, not an override entry: entries are
    # materialised as files for agents to read, and this is UI config
    # for the frontend.
    #
    # Deliberately ``dict`` rather than a typed model. Nothing here
    # interprets it — the backend hands it to the webview, which
    # validates every value against its own allowlist before letting
    # any of it near a stylesheet. A schema here would be a second
    # place to update each time the server learns a new token, and it
    # would not be the place doing the checking.
    theme: dict | None = None

    #: The same pack as igni server signed it — a compact JWS, kept exactly
    #: as received. Only a pack that arrived with one, and whose signature
    #: verified, is ever applied (``group_policy_signature.py``).
    signed_pack: str | None = None

    @classmethod
    def from_policy(
        cls,
        policy: dict,
        *,
        etag: str | None = None,
        signed_pack: str | None = None,
    ) -> GroupPolicyPack:
        """A pack built from the *signed* content, not the plain fields.

        The response carries both; once the signature has verified, this is
        what is materialised and merged, so nothing that was not signed can
        ride along beside it.
        """
        return cls(
            group_id=str(policy.get("group_id") or ""),
            group_name=str(policy.get("group_name") or ""),
            fetched_at=policy.get("fetched_at") or datetime.now(timezone.utc),
            entries=[GroupPolicyEntry(**entry) for entry in policy.get("entries") or []],
            default_model=policy.get("default_model"),
            theme=policy.get("theme"),
            etag=etag,
            signed_pack=signed_pack,
        )

    def to_settings_dict(self) -> dict:
        """Convert to a settings dict for the accumulator.

        Only the 'settings' kind entries are merged as config;
        'agents', 'mcps', and 'plugins' are materialized to disk by
        GroupPolicyCache and picked up by their respective loaders.
        """
        result: dict = {}
        for o in self.entries:
            if o.kind == "settings" and o.enabled:
                try:
                    parsed = json.loads(o.content) if o.content_type == "json" else {}
                    if isinstance(parsed, dict):
                        result = _deep_merge(result, parsed)
                except Exception as exc:
                    # An organisation's setting that did not apply must not
                    # go unnoticed: it was dropped with no trace at all.
                    logger.warning("group policy: a settings entry was not applied: %s", exc)
        return result

    def agent_entries(self) -> list[GroupPolicyEntry]:
        return [o for o in self.entries if o.kind == "agents" and o.enabled]

    def mcp_entries(self) -> list[GroupPolicyEntry]:
        return [o for o in self.entries if o.kind == "mcps" and o.enabled]

    def plugin_disabled_names(self) -> list[str]:
        return [o.entry_name for o in self.entries if o.kind == "plugins" and not o.enabled]


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class UnsafeEntryName(ValueError):
    """An entry name that would not stay inside the cache directory."""


def safe_entry_name(entry_name: str) -> str:
    """*entry_name* if it is a plain filename, else raise.

    The server sends these names and the cache turns them into paths:
    ``dir_for(kind) / f"{entry_name}.json"``. ``Path`` happily accepts a
    separator, so an entry named ``../../../../../../tmp/x`` wrote
    ``/tmp/x.json`` — outside the cache, on every machine that synced the
    pack. For ``scripts`` the file is written mode 0700, which makes it
    an executable dropped anywhere the user can write.

    Checked here rather than only on the server because this is the side
    that must not be talked into it: a client can be pointed at a server
    somebody else runs, and "the server validates it" is not a property
    this process can verify. The server validates it too.

    Rejects rather than sanitises. A name that has to be rewritten to be
    safe is not the name the admin thinks they published, and silently
    writing ``....tmp.x`` would leave both sides believing something
    different about what shipped.
    """
    name = (entry_name or "").strip()
    if not name:
        raise UnsafeEntryName("entry name is empty")
    if name in {".", ".."}:
        raise UnsafeEntryName(f"entry name {entry_name!r} is a directory reference")
    if "/" in name or "\\" in name:
        raise UnsafeEntryName(f"entry name {entry_name!r} contains a path separator")
    if "\x00" in name:
        raise UnsafeEntryName(f"entry name {entry_name!r} contains a NUL byte")
    if name.startswith("~"):
        raise UnsafeEntryName(f"entry name {entry_name!r} starts with a home reference")
    # ``Path('C:x')`` is drive-relative on Windows, and a bare drive
    # letter is not a filename anywhere.
    if len(name) >= 2 and name[1] == ":":
        raise UnsafeEntryName(f"entry name {entry_name!r} looks like a drive path")
    return name


class GroupPolicyCache:
    """Materializes a group pack's file-type entries to disk.

    Agents (markdown) → ~/.igni/group-policy/agents/<name>.md
    MCPs (json)       → ~/.igni/group-policy/mcps/<name>.json

    Plugins are special-cased: an entry with ``source_url`` is
    installed via :class:`PluginInstaller` into the new ``group-policy``
    root (``<data_dir>/group-policy/plugins/``, priority 4.5 between
    project-igni and managed-igni). An entry without ``source_url``
    falls back to writing its YAML to the legacy path so old entry
    shapes keep working until they're migrated.

    The cache is invalidated whenever the pack's content hash changes.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        installer: PluginInstaller | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._cache_dir = cache_dir or (Path.home() / CONFIG_DIR / "group-policy")
        # Optional deps for the install path. Tests pass a stub
        # installer; production wires ``PluginInstaller(data_dir=...)``
        # from the same data_dir so install + cache land under the
        # same root and the plugins loader can find them.
        self._installer = installer
        self._data_dir = data_dir or (Path.home() / CONFIG_DIR)

    def dir_for(self, kind: str) -> Path:
        """The directory this kind's entries live in, created on demand."""
        name = _PLAIN_KINDS[kind][0] if kind in _PLAIN_KINDS else kind
        d = self._cache_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, kind: str, entry_name: str) -> Path:
        """Where one entry of this kind is written.

        Raises :class:`UnsafeEntryName` for a name that would leave the
        cache directory — see that function for why the client checks
        this itself rather than trusting the server.
        """
        _, filename = _PLAIN_KINDS[kind]
        safe = safe_entry_name(entry_name)
        path = self.dir_for(kind) / filename.format(name=safe)

        # Belt to the braces above, and it does real work: ``filename``
        # is a template from ``_PLAIN_KINDS``, and ``skills`` is already
        # ``{name}/SKILL.md`` — a legitimate separator the template owns
        # rather than the name. So the property is "under the kind's
        # directory", not "directly in it"; asserting the latter broke
        # every skill, which is how this comment came to be accurate.
        root = self.dir_for(kind).resolve()
        resolved = path.resolve()
        if root != resolved and root not in resolved.parents:
            raise UnsafeEntryName(f"entry name {entry_name!r} would write outside {root}")
        return path

    # Named accessors for the kinds other modules reach for directly.
    @property
    def agents_dir(self) -> Path:
        return self.dir_for("agents")

    @property
    def mcps_dir(self) -> Path:
        return self.dir_for("mcps")

    @property
    def plugins_dir(self) -> Path:
        return self.dir_for("plugins")

    def _content_hash(self, content: str) -> str:
        # Change detection, not integrity: the pack's signature is checked elsewhere.
        return hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()[:12]

    def _materialize_plugin(self, o: GroupPolicyEntry) -> None:
        """Install or skip-write a single plugin entry.

        If ``o.source_url`` is set, defer to :class:`PluginInstaller`,
        scoped to ``<data_dir>/group-policy/plugins/<entry name>`` — the
        pack's directory, so it is rebuilt and removed with the pack.
        Older releases cloned these into the user's own
        ``<data_dir>/plugins``; :meth:`_adopt_user_install` moves one
        across, once.
        On install failure (``GitError`` / network / bad manifest /
        already installed) we log and continue —
        one bad plugin must not abort the rest of the pack.

        If ``source_url`` is empty, fall back to writing the entry's
        YAML to the legacy path. This keeps the old entry shape
        working until admins migrate to source-url form.
        """
        if not o.source_url:
            path = self.plugins_dir / f"{safe_entry_name(o.entry_name)}.yaml"
            path.write_text(o.content, encoding="utf-8")
            return

        name = safe_entry_name(o.entry_name)
        target = self._cache_dir / "plugins" / name
        if target.is_dir():
            # Installed already, where the pack keeps it. Not re-cloned on
            # every poll; removed by the pack when the entry goes.
            return
        installer = self._installer
        scoped = isinstance(getattr(type(installer), "scoped_to", None), Callable)
        if scoped:
            self._adopt_user_install(o, target)
            if target.is_dir():
                return

        if installer is None:
            # Cache instantiated without an installer — pack author
            # probably meant to ship a plugin source but the wiring
            # is incomplete. Log loud; don't fail the session.
            logger.warning(
                "Group policy plugin entry %r has source_url but no installer "
                "is configured; skipping install. Pass PluginInstaller(data_dir=...) "
                "when constructing GroupPolicyCache to enable remote sources.",
                o.entry_name,
            )
            return

        try:
            if scoped:
                # Into ``group-policy/plugins/<entry name>``, where the pack
                # rebuilds and removes it — not the user's own directory.
                installer.scoped_to(self._cache_dir / "plugins").install(
                    o.source_url,
                    ref=o.source_ref,
                    subdir=o.source_subdir,
                    directory_name=name,
                )
            else:
                installer.install(
                    o.source_url,
                    ref=o.source_ref,
                    subdir=o.source_subdir,
                )
            logger.info(
                "Group policy plugin %r installed from %s",
                o.entry_name,
                o.source_url,
            )
        except PluginError as exc:
            # ``PluginError`` covers manifest-missing, already
            # installed, etc. Treat "already
            # installed" as success — the installer is idempotent
            # at the manifest level, not at the URL level.
            msg = str(exc).lower()
            if "already installed" in msg or "already exists" in msg:
                logger.info(
                    "Group policy plugin %r is already installed from %s; reusing",
                    o.entry_name,
                    o.source_url,
                )
                return
            logger.warning(
                "Group policy plugin %r install failed: %s",
                o.entry_name,
                exc,
            )
        except Exception as exc:
            # GitError from the system git binary, network drop,
            # auth failure — log and move on. We never want a
            # broken upstream plugin to disable the rest of the
            # user's plugins.
            logger.warning(
                "Group policy plugin %r install errored: %s",
                o.entry_name,
                exc,
            )

    def _adopt_user_install(self, o: GroupPolicyEntry, target: Path) -> None:
        """Move a group plugin an older release put in the user's directory.

        Recognised by the source ``plugins.json`` recorded for it — the
        entry's ``source_url``. Its record is dropped either way (it is the
        pack's now, not something ``/plugin update`` manages); the first
        such directory becomes the pack's install, and any other is removed.
        """
        try:
            released = self._installer.release_installed_from(o.source_url)
        except Exception as exc:  # noqa: BLE001 — a migration must not stop the pack
            logger.debug("group-policy: no user-tier install to adopt (%s)", exc)
            return
        for path in released:
            try:
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(target))
                    logger.info(
                        "group-policy: moved plugin %s from %s to %s", o.entry_name, path, target
                    )
                else:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError as exc:
                logger.warning("group-policy: could not move %s (%s)", path, exc)

    def missing_plugins(self, pack: GroupPolicyPack) -> list[str]:
        """The pack's ``source_url`` plugins with nothing installed for them."""
        return [
            o.entry_name
            for o in pack.entries
            if o.kind == "plugins"
            and o.enabled
            and o.source_url
            and not (self._cache_dir / "plugins" / o.entry_name).is_dir()
        ]

    def _stored_pack_lacks_plugins(self) -> bool:
        """Whether the stored pack names a plugin that is not installed.

        Read without verifying: the only thing it decides is whether the
        next fetch asks for the whole pack instead of a 304, so a forged
        file can cost a download and nothing else.
        """
        compact = self.signed_pack_text()
        if not compact:
            return False
        policy = unverified_claims(compact).get("policy")
        entries = policy.get("entries") if isinstance(policy, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if (
                not isinstance(entry, dict)
                or entry.get("kind") != "plugins"
                or not entry.get("source_url")
            ):
                continue
            name = str(entry.get("entry_name") or "")
            if name and not (self._cache_dir / "plugins" / name).is_dir():
                return True
        return False

    def rebuild(self, pack: GroupPolicyPack) -> None:
        """Re-derive every file-shaped entry from a *verified* pack.

        Called at start-up with the pack ``pack.jws`` verified to, so what
        the loaders then read is what igni server signed: an org agent or
        hook that was deleted is written back, one that was edited is
        overwritten, and a file added beside them is pruned. No network:
        a plugin installed from a ``source_url`` is not re-cloned here
        (the next fetch does that), but a directory under ``plugins/``
        that no such entry names is removed — nothing but the pack puts
        one there.
        """
        self.materialize(pack, rebuild=True)

    def clear_derived(self) -> None:
        """Remove everything derived from a pack: every kind's directory.

        For when there is no pack this install may apply — igni server
        signed "no pack for you", or the stored one does not verify. What
        was materialised from a pack is then nobody's policy, and a loader
        must not find it. The signed pack, the cached keys and the version
        are kept: they are what the next start checks against.
        """
        names = {directory for directory, _ in _PLAIN_KINDS.values()} | {"mcps", "hooks", "plugins"}
        for name in sorted(names):
            path = self._cache_dir / name
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        self._drop_legacy_settings()

    def _prune_planted_plugins(self, pack: GroupPolicyPack) -> None:
        if not self.plugins_dir.is_dir():
            return
        installed = {o.entry_name for o in pack.entries if o.kind == "plugins" and o.source_url}
        for path in self.plugins_dir.iterdir():
            if path.is_dir() and path.name not in installed:
                logger.warning(
                    "group-policy: %s is not in the organisation's signed pack; removed", path
                )
                shutil.rmtree(path, ignore_errors=True)

    def materialize(self, pack: GroupPolicyPack, *, rebuild: bool = False) -> None:
        """Write every file-shaped entry to disk, removing stale ones.

        One directory per kind, each of which a loader is told to scan —
        the same arrangement MCP servers have always had. Plugin entries
        with a ``source_url`` are installed by :class:`PluginInstaller`
        instead of written, and settings are merged as config rather
        than materialised at all.

        Also writes ``pack_meta.json`` for the RPC handler to read back.
        """
        # Every kind that owns files gets a set, including the two the
        # loop below writes through their own branch. Missing one is a
        # KeyError the moment a group ships that kind.
        active: dict[str, set[str]] = {kind: set() for kind in _PLAIN_KINDS}
        active["mcps"] = set()
        active["plugins"] = set()
        hooks: dict[str, list] = {}

        for o in pack.entries:
            if not o.enabled:
                continue

            # One unsafe name must not cost the group its whole policy.
            #
            # ``safe_entry_name`` raises, and every write below goes
            # through it — so without this the first bad entry aborts the
            # materialise and the session comes up with no agents, no
            # hooks and no MCP servers. Skipping the entry keeps the other
            # N working, and the warning names it so an admin can fix the
            # one thing that is wrong.
            try:
                safe_entry_name(o.entry_name)
            except UnsafeEntryName as exc:
                logger.warning(
                    "Group policy entry skipped: %s. A %s entry cannot be written "
                    "under that name, so it has been ignored; the rest of the pack "
                    "was applied.",
                    exc,
                    o.kind,
                )
                continue

            if o.kind in _PLAIN_KINDS:
                self._write_plain(o)
                active[o.kind].add(o.entry_name)
            elif o.kind == "mcps":
                # Wrap the per-server content in the ``{mcpServers: {...}}``
                # envelope :class:`MCPConfigLoader` expects. The server
                # sends one server definition per entry; the loader reads
                # N-server files, so it goes under its entry name.
                path = self.dir_for("mcps") / f"{safe_entry_name(o.entry_name)}.json"
                path.write_text(
                    json.dumps({"mcpServers": {o.entry_name: _try_parse_json(o.content)}}),
                    encoding="utf-8",
                )
                active["mcps"].add(o.entry_name)
            elif o.kind == "hooks":
                self._collect_hook(o, hooks)
            elif o.kind == "plugins":
                if not (rebuild and o.source_url):
                    self._materialize_plugin(o)
                active["plugins"].add(o.entry_name)
            elif o.kind == "extensions":
                # Withdrawn: extensions come from their services
                # through igni, and a policy is somebody else deciding
                # what runs on this machine. Said, so an admin whose
                # pack still carries one learns why nothing arrives.
                logger.warning(
                    "Group policy entry %r is an extension, and a group policy "
                    "no longer installs extensions — they arrive from their "
                    "services through igni server. Ignored.",
                    o.entry_name,
                )

        self._write_hooks(hooks)
        # Settings are not written out: they are read back from the signed
        # pack (``pack.jws``), verified, at the next start. A derived,
        # unsigned copy would be a second thing to forge.
        self._drop_legacy_settings()
        self._prune(active)
        # The pack's plugin directory holds only what the pack installs, so a
        # plugin whose entry was withdrawn goes now, and one nobody's pack
        # names was planted.
        self._prune_planted_plugins(pack)

        meta = {
            "group_id": pack.group_id,
            "group_name": pack.group_name,
            "fetched_at": pack.fetched_at.isoformat() if pack.fetched_at else None,
            "entry_count": len(pack.entries),
            "default_model": pack.default_model,
            "etag": pack.etag,
            "theme": pack.theme,
        }
        (self._cache_dir / "pack_meta.json").write_text(json.dumps(meta), encoding="utf-8")

    #: Where older versions persisted the pack's merged ``settings`` entries,
    #: unsigned. Never read any more, and removed when a pack is
    #: materialised, so nothing on disk looks like a policy that is not one.
    SETTINGS_FILE = "group_settings.json"

    #: The pack exactly as igni server signed it (a compact JWS).
    SIGNED_PACK_FILE = "pack.jws"

    #: igni server's JWKS as fetched beside the pack, for offline starts.
    #: Without a pinned key (``group_policy.trusted_keys``) this copy is only
    #: as trustworthy as this directory: it keeps a repository or a stray
    #: file from planting a policy, not the person who owns the machine.
    JWKS_FILE = "jwks.json"

    #: The newest pack version this install has accepted, for refusing a
    #: rollback to an older one.
    STATE_FILE = "pack_state.json"

    def _drop_legacy_settings(self) -> None:
        (self._cache_dir / self.SETTINGS_FILE).unlink(missing_ok=True)

    def store_signed(self, compact: str, *, jwks: dict | None, version: int) -> None:
        """Keep a verified pack as received, the keys it verified against,
        and its version as the newest one accepted.

        The version is the live pack's, not the larger of it and the last:
        a pack fetched over TLS from igni server is the authority on what is
        current, so a server whose clock went backwards does not lock its
        clients out. The rollback check is for the copy on disk.
        """
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        (self._cache_dir / self.SIGNED_PACK_FILE).write_text(compact, encoding="utf-8")
        if jwks is not None:
            (self._cache_dir / self.JWKS_FILE).write_text(json.dumps(jwks), encoding="utf-8")
        (self._cache_dir / self.STATE_FILE).write_text(
            json.dumps({"version": version}), encoding="utf-8"
        )
        self._drop_legacy_settings()

    def signed_pack_text(self) -> str | None:
        """The stored JWS, or ``None`` when there is none to read."""
        try:
            return (self._cache_dir / self.SIGNED_PACK_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def cached_jwks(self) -> dict | None:
        try:
            loaded = json.loads((self._cache_dir / self.JWKS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def applied_version(self) -> int | None:
        try:
            loaded = json.loads((self._cache_dir / self.STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        version = loaded.get("version") if isinstance(loaded, dict) else None
        return version if isinstance(version, int) and not isinstance(version, bool) else None

    def verified_pack(self, trust: PackTrust) -> GroupPolicyPack | None:
        """The stored pack, if it verifies; raises otherwise.

        ``None`` when what is stored is igni server's signed "no pack for
        this person" — verified, and meaning there is no policy.

        :class:`PackMissing` when there is no stored pack at all — the
        ordinary state for somebody in no organisation — and
        :class:`~ember_code.core.config.group_policy_signature.PackRejected`
        when there is one and it may not be applied.
        """
        compact = self.signed_pack_text()
        if not compact:
            raise PackMissing(f"no signed group pack in {self._cache_dir}")
        verified = verify_pack(
            compact,
            jwks=self.cached_jwks(),
            pins=trust.pins,
            api_url=trust.api_url,
            subject=trust.subject,
            min_version=self.applied_version(),
            org=trust.org,
        )
        if verified.absent or verified.policy is None:
            # igni server's signed "no pack for this person".
            return None
        meta = self.read_pack_meta() or {}
        pack = GroupPolicyPack.from_policy(
            verified.policy, etag=meta.get("etag"), signed_pack=compact
        )
        return pack

    def _write_plain(self, o: GroupPolicyEntry) -> None:
        """One entry, one file, at the path its loader looks in."""
        path = self.path_for(o.kind, o.entry_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = _with_model(o.content, o.model) if o.kind == "agents" else o.content
        path.write_text(content, encoding="utf-8")
        if o.kind in _EXECUTABLE_KINDS:
            # 0o700 rather than 0o755: nothing else on the machine needs
            # to run it, and this is code that arrived over the network.
            path.chmod(0o700)

    @property
    def scripts_dir(self) -> Path:
        """Where a hook's ``{group_scripts}`` resolves to."""
        return self.dir_for("scripts")

    def _expand_scripts_placeholder(self, command: str) -> str:
        """Point a hook command at the scripts this pack materialised.

        Only this side knows the directory, and it is per-machine, so the
        server stores a placeholder and the substitution happens here.
        Left untouched when absent, so a command naming an absolute path
        or something baked into the image still works.
        """
        return command.replace(GROUP_SCRIPTS_PLACEHOLDER, str(self.scripts_dir))

    def _collect_hook(self, o: GroupPolicyEntry, hooks: dict[str, list]) -> None:
        """Fold one hook entry into the shared ``hooks.json`` shape.

        Hooks are the one kind that is not a file per entry: they are
        declarations keyed by event, and igni reads them from a
        settings-shaped file. An entry carries either a bare declaration
        or a whole ``{"hooks": {...}}`` block; both end up merged into
        one file the loader is pointed at.
        """
        try:
            parsed = json.loads(o.content)
        except (TypeError, ValueError):
            logger.warning("Group hook %r is not valid JSON; skipping", o.entry_name)
            return

        block = parsed.get("hooks") if isinstance(parsed, dict) else None
        if isinstance(block, dict):
            for event, declarations in block.items():
                hooks.setdefault(event, []).extend(
                    declarations if isinstance(declarations, list) else [declarations]
                )
            return

        # A bare declaration has to say which event it is for.
        if isinstance(parsed, dict) and parsed.get("event"):
            event = str(parsed.pop("event"))
            hooks.setdefault(event, []).append(parsed)
            return

        logger.warning(
            "Group hook %r names no event and carries no hooks block; skipping",
            o.entry_name,
        )

    def _write_hooks(self, hooks: dict[str, list]) -> None:
        """Write (or remove) the merged hooks file."""
        path = self.dir_for("hooks") / "settings.json"
        if hooks:
            # Expanded here rather than at each parse site: a hook can
            # arrive as a bare declaration or nested inside a ``hooks``
            # block, and doing it once on the merged result covers both.
            for declarations in hooks.values():
                for declaration in declarations:
                    if isinstance(declaration, dict) and isinstance(
                        declaration.get("command"), str
                    ):
                        declaration["command"] = self._expand_scripts_placeholder(
                            declaration["command"]
                        )
            path.write_text(json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)

    def _prune(self, active: dict[str, set[str]]) -> None:
        """Remove what the group no longer ships.

        Plugins installed from a source are pruned separately, by
        :meth:`_prune_planted_plugins`: their directory is named after the
        entry rather than carrying a suffix.
        """
        for kind, names in active.items():
            directory = self.dir_for(kind)
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                stem = path.name[: -len(_SUFFIX[kind])] if kind in _SUFFIX else path.name
                if kind == "skills":
                    stale = path.is_dir() and path.name not in names
                elif kind == "plugins":
                    stale = path.suffix == ".yaml" and path.stem not in names
                else:
                    stale = path.suffix == _SUFFIX.get(kind, "") and stem not in names
                if not stale:
                    continue
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                except Exception as exc:  # noqa: BLE001 — a leftover file is not fatal
                    logger.debug("Could not remove stale group policy path %s: %s", path, exc)

    def read_pack_meta(self) -> dict | None:
        """Read the pack metadata written by materialize(). Returns None if no pack is cached."""
        meta_path = self._cache_dir / "pack_meta.json"
        if not meta_path.is_file():
            return None
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def clear(self) -> None:
        """Remove the entire cache directory."""
        import shutil

        if self._cache_dir.exists():
            shutil.rmtree(self._cache_dir)

    def _is_stale(self) -> bool:
        """Return True when the cached pack is missing or older than TTL."""
        meta = self.read_pack_meta()
        if not meta or not meta.get("fetched_at"):
            return True
        try:
            fetched_at = datetime.fromisoformat(meta["fetched_at"])
        except (TypeError, ValueError):
            return True
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        return age > _CACHE_TTL_SECONDS

    def stored_etag(self) -> str | None:
        """The tag the server gave us for the pack on disk."""
        meta = self.read_pack_meta() or {}
        tag = meta.get("etag")
        return str(tag) if tag else None

    async def _call_fetch(self, fetch: Any, token: str, etag: str | None = None) -> Any:
        """Call the fetcher, handing it our tag when it takes one.

        Tests and older callers pass a one-argument coroutine; the real
        client takes ``(token, etag)``. Trying the richer call first
        keeps both working without every stub having to grow a
        parameter it does not use.
        """
        try:
            return await fetch(token, etag)
        except TypeError:
            return await fetch(token)

    def _touch(self) -> None:
        """Mark the cached pack as checked just now."""
        meta = self.read_pack_meta()
        if not meta:
            return
        meta["fetched_at"] = datetime.now(timezone.utc).isoformat()
        (self._cache_dir / "pack_meta.json").write_text(json.dumps(meta), encoding="utf-8")

    async def refresh_if_stale(
        self,
        token: str,
        fetch: Any,
        *,
        force: bool = False,
        verifier: PackVerifier | None = None,
    ) -> bool:
        """Refresh the on-disk pack if the cached one is missing or stale.

        ``fetch`` is an awaitable callable ``(token) -> GroupPolicyPack | None``
        — passed in (rather than constructing PortalClient here) so the auth
        package can stay above the cache in the import graph.

        ``force`` skips the age check and asks anyway. It does **not**
        skip the ETag: an unchanged pack still comes back as
        :class:`UnchangedPack`, so forcing costs a conditional request
        rather than a download. Used at the start of a session, where a
        developer should pick up an admin's change immediately and the
        event is rare enough to afford the round trip — as distinct from
        a CLI invocation, which the age check exists to keep quiet.

        ``verifier`` checks the pack's signature before anything is
        written (:class:`PackVerifier`); production always passes one. A
        pack it refuses leaves the cache as it was. Without one — tests of
        the file-shaped kinds, which predate signing — the pack's files are
        materialised but no signed pack is stored, so its ``settings``
        never apply.

        Returns True only when the cache was actually updated. Returns
        False when the cache was fresh, fetch raised, fetch returned
        None, the pack was refused, or materialize failed — caller can
        treat any False as "we did not write a new pack".
        """
        if not token:
            return False
        if not force and not self._is_stale():
            return False
        # With no signed pack on disk there is nothing for a 304 to
        # confirm, so ask without the tag and get the pack itself.
        etag = self.stored_etag()
        if verifier is not None and (
            not self.signed_pack_text() or self._stored_pack_lacks_plugins()
        ):
            # A 304 would confirm a pack whose plugin was removed from disk;
            # the full pack is what reinstalls it.
            etag = None
        try:
            pack = await self._call_fetch(fetch, token, etag)
        except Exception as exc:
            # Warning (not debug) so portal outages are visible in the
            # standard log without flipping a flag.
            logger.warning("group-policy fetch failed: %s", exc)
            return False
        if isinstance(pack, UnchangedPack):
            # Nothing to write, but the pack on disk is confirmed
            # current — so stamp it, or every poll would find it stale
            # and ask again.
            self._touch()
            return False
        if pack is None:
            return False
        if isinstance(pack, SignedAbsence) and verifier is None:
            return False
        if verifier is not None:
            try:
                accepted = await verifier(pack)
            except PackRejected as exc:
                logger.warning(
                    "group-policy: the pack igni server sent was refused (%s); "
                    "nothing from it has been applied",
                    exc,
                )
                return False
            # Stored first: the settings it carries are what the next start
            # enforces, and they must not wait on a plugin clone below.
            self.store_signed(accepted.compact, jwks=accepted.jwks, version=accepted.version)
            if accepted.pack is None:
                # Signed "no pack for this person": the policy is withdrawn.
                self.clear_derived()
                # Kept as the only meta: the tag turns the next poll into a 304.
                (self._cache_dir / "pack_meta.json").write_text(
                    json.dumps(
                        {
                            "etag": getattr(pack, "etag", None),
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                            "entry_count": 0,
                        }
                    ),
                    encoding="utf-8",
                )
                logger.info("group-policy: igni server says there is no pack; cache cleared")
                return True
            pack = accepted.pack
        try:
            self.materialize(pack)
        except Exception as exc:
            logger.warning("group-policy materialize failed: %s", exc)
            return False
        return True


async def refresh(
    token: str,
    fetch: Any,
    *,
    cache_dir: Path | None = None,
    data_dir: Path | None = None,
    installer: Any = None,
    force: bool = False,
    verifier: PackVerifier | None = None,
) -> bool:
    """One-shot cache refresh — used by AuthController on cold start + login.

    Construct a single GroupPolicyCache (mirroring the production defaults)
    and delegate to :meth:`GroupPolicyCache.refresh_if_stale`. Returns False
    when the cache was fresh and no fetch was attempted.

    ``cache_dir`` wins when both are supplied. Otherwise we derive it from
    ``data_dir`` (``<data_dir>/group-policy``) — the same path
    :meth:`GroupPolicyCache.__init__` would use if ``cache_dir`` were the
    canonical root, so callers can hand in just one argument.

    ``installer`` is forwarded to :class:`GroupPolicyCache` so plugin
    entries with ``source_url`` get git-installed instead of being
    skipped. Optional — when omitted, source-URL plugin entries write a
    warning and fall through (same behavior the cache had before).

    ``force`` asks the server regardless of how fresh the cache is —
    see :meth:`GroupPolicyCache.refresh_if_stale`.
    """
    if cache_dir is None and data_dir is not None:
        cache_dir = Path(data_dir).expanduser() / "group-policy"
    cache = GroupPolicyCache(cache_dir=cache_dir, data_dir=data_dir, installer=installer)
    return await cache.refresh_if_stale(token, fetch, force=force, verifier=verifier)


def group_policy_dir(data_dir: Any = None) -> Path:
    """Where a pack is cached for this install.

    The writer (:func:`refresh`, called by ``AuthController``) derives this
    from ``settings.storage.data_dir``; the reader below has to land on the
    same directory or it reads an empty one and reports "no policy" for a
    group that has one. So the derivation lives here once, and both sides
    call it.

    A missing or nonsensical ``data_dir`` falls back to
    :func:`~ember_code.core.paths.home_config_dir` — the same directory
    ``storage.data_dir`` itself defaults to, and migration-aware, so an
    install still on ``~/.ember`` is not told it has no policy. Anything that
    is not a path is *ignored* rather than coerced: a caller holding a stub
    settings object should get the default, not ``Path('<MagicMock ...>')``.
    """
    if isinstance(data_dir, (str, Path)) and str(data_dir).strip():
        return Path(data_dir).expanduser() / "group-policy"
    return home_config_dir() / "group-policy"


class PackMissing(PackRejected):
    """No signed pack is stored — somebody in no organisation, or never fetched."""


class GroupPolicyUnavailable(RuntimeError):
    """``group_policy.required`` is set and no verified pack could be had.

    The message is written for the person in front of igni: what is wrong
    and what to do about it.
    """


class GroupPolicyGate:
    """Whether this process may run agent sessions.

    Closed by the backend when ``group_policy.required`` is set and no
    verified pack could be applied at start-up. The backend still boots —
    the person has to be able to sign in, which is how a valid pack is
    fetched — but every place that starts an agent run asks here first and
    refuses with :meth:`reason`. Stays closed until the process restarts:
    the settings this process loaded were built without the policy, so a
    pack that arrives later cannot make *them* compliant.
    """

    _reason: str | None = None

    @classmethod
    def close(cls, reason: str) -> None:
        cls._reason = reason

    @classmethod
    def reason(cls) -> str | None:
        return cls._reason

    @classmethod
    def reset(cls) -> None:
        cls._reason = None


def _signed_in_subject(credentials_file: Any) -> tuple[str | None, str | None, str | None]:
    """``(sub, org, token)`` of the session stored in ``credentials_file``.

    Decoded, not verified: the token is checked by igni server on every
    call, and here it only names *whose* pack may be applied. A person who
    edits it to name somebody else still needs a pack igni server signed
    for that somebody.
    """
    try:
        from ember_code.core.auth.credentials import CredentialsStore
        from ember_code.core.auth.schemas import JwtClaims

        loaded = CredentialsStore(
            credentials_file if isinstance(credentials_file, str) else None
        ).load()
        if not loaded.ok or loaded.creds is None:
            return None, None, None
        token = loaded.creds.access_token
        claims = JwtClaims.decode(token)
        if claims is None:
            return None, None, token
        return claims.sub, claims.org, token
    except Exception as exc:  # noqa: BLE001 — no session is "nobody signed in"
        logger.debug("group-policy: could not read the signed-in session (%s)", exc)
        return None, None, None


@dataclass(frozen=True)
class PackTrust:
    """What a pack is checked against on this install."""

    #: The igni server this install is configured for — the pack's ``iss``.
    api_url: str | None
    #: The person signed in — the pack's ``sub``.
    subject: str | None
    #: Thumbprints from ``group_policy.trusted_keys`` (managed or env only).
    pins: tuple[str, ...] = ()
    #: ``group_policy.required``.
    required: bool = False
    #: The stored session token, for the one live fetch ``required`` allows.
    token: str | None = None
    #: The organisation the session is for, when it names one — the pack's ``org``.
    org: str | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> PackTrust:
        subject, org, token = _signed_in_subject(settings.auth.credentials_file)
        return cls(
            api_url=settings.api_url or None,
            subject=subject,
            pins=tuple(settings.group_policy.trusted_keys or ()),
            required=bool(settings.group_policy.required),
            token=token,
            org=org,
        )


@dataclass(frozen=True)
class AcceptedPack:
    """A fetched pack whose signature verified."""

    #: ``None`` when igni server signed "no pack for this person".
    pack: GroupPolicyPack | None
    compact: str
    jwks: dict
    version: int


class PackVerifier:
    """Checks a freshly fetched pack before anything of it is written.

    Used by ``AuthController._hydrate_group_policy``. The keys come from
    ``jwks_fetch`` — igni server's ``/.well-known/jwks.json``, fetched now —
    narrowed to the pinned thumbprints when there are any. Without a pin the
    JWKS is trusted only over TLS (or from loopback): over plain HTTP it
    could be swapped in transit together with the pack.
    """

    def __init__(
        self,
        *,
        api_url: str | None,
        subject: str | None,
        pins: tuple[str, ...] = (),
        jwks_fetch: Callable[[], Awaitable[dict | None]],
        org: str | None = None,
    ) -> None:
        self._api_url = api_url
        self._subject = subject
        self._pins = tuple(pins)
        self._jwks_fetch = jwks_fetch
        self._org = org

    async def __call__(self, pack: GroupPolicyPack | SignedAbsence) -> AcceptedPack:
        compact = pack.signed_pack
        if not compact:
            raise PackRejected("igni server sent the pack unsigned")
        if not self._pins and not may_trust_unpinned_jwks_from(self._api_url or ""):
            raise PackRejected(
                f"{self._api_url!r} is not HTTPS, so igni server's keys cannot be fetched "
                "safely; pin one in group_policy.trusted_keys"
            )
        jwks = await self._jwks_fetch()
        if not isinstance(jwks, dict):
            raise PackRejected("igni server's signing keys could not be fetched")
        verified = verify_pack(
            compact,
            jwks=jwks,
            pins=self._pins,
            api_url=self._api_url,
            subject=self._subject,
            org=self._org,
        )
        if isinstance(pack, SignedAbsence) != verified.absent:
            raise PackRejected("the signed answer does not say what the response says")
        return AcceptedPack(
            pack=None
            if verified.policy is None
            else GroupPolicyPack.from_policy(verified.policy, etag=pack.etag, signed_pack=compact),
            compact=compact,
            jwks=jwks,
            version=verified.version,
        )


def fetch_signed_pack_now(
    trust: PackTrust, cache: GroupPolicyCache, *, timeout: float = 10.0
) -> bool:
    """Fetch, verify and store a pack synchronously. Returns whether one was stored.

    The one network call a settings load makes, and only when
    ``group_policy.required`` is set and the stored pack is unusable: a
    required policy is worth a start-up round trip, an optional one is
    not. Only the signed pack is stored; the caller rebuilds the
    file-shaped kinds from it.
    """
    if not (trust.token and trust.api_url):
        return False
    if not trust.pins and not may_trust_unpinned_jwks_from(trust.api_url):
        logger.warning(
            "group-policy: %s is not HTTPS and no key is pinned; not fetching a pack",
            trust.api_url,
        )
        return False
    from ember_code.core.activity import http as busy_http

    base = trust.api_url.rstrip("/")
    try:
        with busy_http.BusyClient(timeout=timeout, busy_holder="igni-server") as client:
            response = client.get(
                f"{base}/v1/portal/me/group/pack",
                headers={"Authorization": f"Bearer {trust.token}"},
            )
            if response.status_code != 200:
                logger.warning("group-policy: igni server answered %s", response.status_code)
                return False
            body = response.json()
            jwks = client.get(jwks_url_for(trust.api_url)).json()
    except Exception as exc:  # noqa: BLE001 — offline is the case this exists for
        logger.warning("group-policy: could not reach igni server (%s)", exc)
        return False
    compact = body.get("signed_pack") if isinstance(body, dict) else None
    try:
        verified = verify_pack(
            compact or "",
            jwks=jwks,
            pins=trust.pins,
            api_url=trust.api_url,
            subject=trust.subject,
            org=trust.org,
        )
    except PackRejected as exc:
        logger.warning("group-policy: the pack igni server sent was refused (%s)", exc)
        return False
    cache.store_signed(
        compact, jwks=jwks if isinstance(jwks, dict) else None, version=verified.version
    )
    if verified.absent:
        cache.clear_derived()
    return True


class CachedSettingsFetcher:
    """The ``group_policy_fetcher`` production hands to ``load_settings``.

    Zero-argument and callable, which is the whole contract
    :class:`~ember_code.core.config.merge_plan.GroupPolicyTier` asks for.
    Build it with :meth:`for_settings` from the local-only ``Settings`` of
    the first load pass: that is where the data directory, ``api_url``, the
    credentials and the managed-only ``group_policy.trusted_keys`` /
    ``group_policy.required`` come from.

    ## Why this reads disk instead of the portal

    A settings load runs before the process holds a bearer token — the token
    is read out of ``Settings``, and ``AuthController`` is constructed with
    the ``Settings`` this fetcher helped build. A fetcher that called the
    portal would therefore make every start-up wait on a network round trip
    that can hang.

    So the fetch and the read are deliberately in different places. The fetch
    is ``AuthController._hydrate_group_policy`` — cold start, login, and every
    ``settings.group_policy.poll_seconds`` — which verifies the pack and stores
    it exactly as igni server signed it (``pack.jws``). The read is here, and
    it verifies again: the file is the user's to write, so what is on disk is
    trusted only as far as its signature (``group_policy_signature.py``).

    * Offline, portal down, token expired: the last pack that verified is
      still applied. A pack has no expiry.
    * A policy published while igni is running reaches ``Settings`` at the
      next start.
    * A pack that is missing, unsigned, edited, for another person, server
      or organisation, or older than one already applied is **never**
      merged, and what was derived from an earlier pack is removed.
    * A verified pack's agents, hooks, MCP servers, scripts and plugins are
      rebuilt from it here, so an org file deleted or edited on disk is
      restored and one added beside them is removed (:meth:`GroupPolicyCache.rebuild`).
    * igni server's signed "no pack for this person" clears the cache.

    ## When there is no usable pack

    ``group_policy.required`` off: a warning (or, for no pack at all, a
    debug line — the ordinary case for anybody outside an organisation), and
    the settings load continues on local configuration. On: one live fetch if
    somebody is signed in (:func:`fetch_signed_pack_now`), and otherwise
    :class:`GroupPolicyUnavailable` — the callers start no agent session.

    Memoized: the read happens once per fetcher.
    """

    def __init__(
        self,
        data_dir: Any = None,
        *,
        cache_dir: Path | None = None,
        trust: PackTrust | None = None,
    ) -> None:
        self._cache_dir = cache_dir if cache_dir is not None else group_policy_dir(data_dir)
        self._cache = GroupPolicyCache(cache_dir=self._cache_dir)
        #: Without a trust context nothing can be verified, so nothing applies.
        self._trust = trust or PackTrust(api_url=None, subject=None)
        self._pack: GroupPolicyPack | None = None
        self._error: GroupPolicyUnavailable | None = None
        self._read = False

    @classmethod
    def for_settings(cls, settings: Any) -> CachedSettingsFetcher:
        """The fetcher for an install configured by ``settings``."""
        return cls(settings.storage.data_dir, trust=PackTrust.from_settings(settings))

    @property
    def required(self) -> bool:
        return self._trust.required

    def __call__(self) -> GroupPolicyPack | None:
        if not self._read:
            try:
                self._pack = self._read_once()
            except GroupPolicyUnavailable as exc:
                self._error = exc
            self._read = True
        if self._error is not None:
            raise self._error
        return self._pack

    def _verified(self) -> GroupPolicyPack | None:
        return self._cache.verified_pack(self._trust)

    def _rebuilt(self, pack: GroupPolicyPack) -> GroupPolicyPack:
        """Re-derive the pack's agents, hooks, MCP servers, scripts and
        plugins from the verified pack, so the loaders read what igni server
        signed rather than whatever is in the directory now.

        Raises when that fails: the settings verified, but the files the
        session is about to load would not be the organisation's.
        """
        self._cache.rebuild(pack)
        missing = self._cache.missing_plugins(pack)
        if missing:
            # Start-up does not clone: the next fetch reinstalls them (it
            # asks for the full pack while one is missing). Until then the
            # pack is not whole on disk.
            raise RuntimeError(
                "plugin(s) " + ", ".join(missing) + " are not installed; the next fetch "
                "from igni server reinstalls them"
            )
        return pack

    def _read_once(self) -> GroupPolicyPack | None:
        """Verify the stored pack, rebuild what derives from it, and say, once,
        what was found."""
        try:
            pack = self._verified()
        except PackMissing:
            # Nothing signed: anything left in the kind directories is
            # nobody's policy, and must not load as the organisation's.
            self._clear_derived()
            if not self._trust.required:
                logger.debug(
                    "no cached org group policy in %s; settings loaded from local "
                    "configuration only",
                    self._cache_dir,
                )
                return None
            return self._required_fallback("there is no group policy on this machine yet")
        except Exception as exc:  # noqa: BLE001 — settings must load regardless
            self._clear_derived()
            if not self._trust.required:
                logger.warning(
                    "group policy in %s ignored: %s. Settings loaded from local "
                    "configuration only.",
                    self._cache_dir,
                    exc,
                )
                return None
            return self._required_fallback(str(exc))

        if pack is None:
            # Verified "no pack for this person": nothing applies, and nothing
            # derived from an earlier pack may linger.
            self._clear_derived()
            logger.info("igni server says this person has no group policy; none applied")
            return None

        try:
            self._rebuilt(pack)
        except Exception as exc:  # noqa: BLE001
            if self._trust.required:
                raise self._unavailable(
                    f"its agents, hooks and tools could not be rebuilt: {exc}"
                ) from exc
            logger.warning(
                "group policy in %s: its agents, hooks and tools could not be rebuilt (%s); "
                "its settings still apply",
                self._cache_dir,
                exc,
            )

        logger.info(
            "org group policy applied to settings: group %s (%s), settings %s, verified, from %s",
            pack.group_name,
            pack.group_id,
            ", ".join(sorted(pack.to_settings_dict())) or "(none)",
            self._cache_dir,
        )
        return pack

    def _clear_derived(self) -> None:
        try:
            self._cache.clear_derived()
        except Exception as exc:  # noqa: BLE001
            logger.warning("group-policy: could not clear %s (%s)", self._cache_dir, exc)

    def _required_fallback(self, why: str) -> GroupPolicyPack | None:
        """``required`` and no usable pack: fetch one now, or refuse."""
        if fetch_signed_pack_now(self._trust, self._cache):
            try:
                pack = self._verified()
                if pack is not None:
                    self._rebuilt(pack)
            except Exception as exc:  # noqa: BLE001
                why = str(exc)
            else:
                if pack is None:
                    # igni server, asked just now, says this person has none.
                    return None
                logger.info(
                    "org group policy fetched from igni server at start-up: group %s (%s)",
                    pack.group_name,
                    pack.group_id,
                )
                return pack
        raise self._unavailable(why)

    def _unavailable(self, why: str) -> GroupPolicyUnavailable:
        server = self._trust.api_url or "igni server"
        return GroupPolicyUnavailable(
            "Your organisation requires its group policy, and igni has no valid copy "
            f"({why}). Connect to {server} and sign in so igni can fetch it, then "
            "restart igni. No agent session will start until then."
        )
