"""Group policy pack — fetched from ember-server, cached on disk, applied as a settings tier."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, model_validator

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
    compatibility, but the install path only fires for ``plugins``.
    """

    kind: str  # agents | mcps | plugins | settings
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


class GroupPolicyPack(BaseModel):
    """Everything a group gives its people — fetched from ember-server."""

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
                except Exception:
                    pass
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


class GroupPolicyCache:
    """Materializes a group pack's file-type entries to disk.

    Agents (markdown) → ~/.ember/group-policy/agents/<name>.md
    MCPs (json)       → ~/.ember/group-policy/mcps/<name>.json

    Plugins are special-cased: an entry with ``source_url`` is
    installed via :class:`PluginInstaller` into the new ``group-policy``
    root (``<data_dir>/group-policy/plugins/``, priority 4.5 between
    project-ember and managed-ember). An entry without ``source_url``
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
        self._cache_dir = cache_dir or (Path.home() / ".ember" / "group-policy")
        # Optional deps for the install path. Tests pass a stub
        # installer; production wires ``PluginInstaller(data_dir=...)``
        # from the same data_dir so install + cache land under the
        # same root and the plugins loader can find them.
        self._installer = installer
        self._data_dir = data_dir or (Path.home() / ".ember")

    def dir_for(self, kind: str) -> Path:
        """The directory this kind's entries live in, created on demand."""
        name = _PLAIN_KINDS[kind][0] if kind in _PLAIN_KINDS else kind
        d = self._cache_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, kind: str, entry_name: str) -> Path:
        """Where one entry of this kind is written."""
        _, filename = _PLAIN_KINDS[kind]
        return self.dir_for(kind) / filename.format(name=entry_name)

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
        return hashlib.md5(content.encode()).hexdigest()[:12]

    def _materialize_plugin(self, o: GroupPolicyEntry) -> None:
        """Install or skip-write a single plugin entry.

        If ``o.source_url`` is set, defer to :class:`PluginInstaller`,
        which clones to ``<data_dir>/group-policy/plugins/<name>``.
        On install failure (``GitError`` / network / bad manifest /
        already installed by another group) we log and continue —
        one bad plugin must not abort the rest of the pack.

        If ``source_url`` is empty, fall back to writing the entry's
        YAML to the legacy path. This keeps the old entry shape
        working until admins migrate to source-url form.
        """
        if not o.source_url:
            path = self.plugins_dir / f"{o.entry_name}.yaml"
            path.write_text(o.content, encoding="utf-8")
            return

        if self._installer is None:
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
            self._installer.install(
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
            # installed by another group, etc. Treat "already
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

    def materialize(self, pack: GroupPolicyPack) -> None:
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
            if o.kind in _PLAIN_KINDS:
                self._write_plain(o)
                active[o.kind].add(o.entry_name)
            elif o.kind == "mcps":
                # Wrap the per-server content in the ``{mcpServers: {...}}``
                # envelope :class:`MCPConfigLoader` expects. The server
                # sends one server definition per entry; the loader reads
                # N-server files, so it goes under its entry name.
                path = self.dir_for("mcps") / f"{o.entry_name}.json"
                path.write_text(
                    json.dumps({"mcpServers": {o.entry_name: _try_parse_json(o.content)}}),
                    encoding="utf-8",
                )
                active["mcps"].add(o.entry_name)
            elif o.kind == "hooks":
                self._collect_hook(o, hooks)
            elif o.kind == "plugins":
                self._materialize_plugin(o)
                active["plugins"].add(o.entry_name)

        self._write_hooks(hooks)
        self._prune(active)

        meta = {
            "group_id": pack.group_id,
            "group_name": pack.group_name,
            "fetched_at": pack.fetched_at.isoformat() if pack.fetched_at else None,
            "entry_count": len(pack.entries),
            "default_model": pack.default_model,
            "etag": pack.etag,
        }
        (self._cache_dir / "pack_meta.json").write_text(json.dumps(meta), encoding="utf-8")

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
        declarations keyed by event, and ember-code reads them from a
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
                    if isinstance(declaration, dict) and isinstance(declaration.get("command"), str):
                        declaration["command"] = self._expand_scripts_placeholder(
                            declaration["command"]
                        )
            path.write_text(json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)

    def _prune(self, active: dict[str, set[str]]) -> None:
        """Remove what the group no longer ships.

        Plugins installed from a source keep their directory: a re-add
        from the same URL skips the install, and deleting a clone to
        reinstall it moments later is a poor trade.
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

    async def _call_fetch(self, fetch: Any, token: str) -> Any:
        """Call the fetcher, handing it our tag when it takes one.

        Tests and older callers pass a one-argument coroutine; the real
        client takes ``(token, etag)``. Trying the richer call first
        keeps both working without every stub having to grow a
        parameter it does not use.
        """
        try:
            return await fetch(token, self.stored_etag())
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

        Returns True only when the cache was actually updated. Returns
        False when the cache was fresh, fetch raised, fetch returned
        None, or materialize failed — caller can treat any False as
        "we did not write a new pack".
        """
        if not token:
            return False
        if not force and not self._is_stale():
            return False
        try:
            pack = await self._call_fetch(fetch, token)
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
    return await cache.refresh_if_stale(token, fetch, force=force)
