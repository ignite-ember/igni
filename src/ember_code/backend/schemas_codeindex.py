"""Typed view models for the ``/codeindex`` slash command's chat output.

Extracted out of :mod:`ember_code.backend.cmd_codeindex` — the old
procedural module built markdown strings inline inside eight free
functions, duplicating the ``SyncResult``-branching logic across
``_sync`` and ``_resync``. Every markdown template that the
:class:`CodeIndexCommand` coordinator emits into chat now lives
here as a Pydantic view model with a single
``.to_command_result()`` render entry point.

Same naming + purpose pattern as the sibling
:mod:`schemas_history` / :mod:`schemas_run` modules already in
``backend/``.

Consumers:

* :class:`CodeIndexStatusView` — the ``/codeindex status`` chat
  card. Wraps the SHAs + optional :class:`ResolvedRepository`
  and renders the 13-line status template.
* :class:`SyncCommandView` / :class:`ResyncCommandView` — wrap a
  :class:`SyncResult` (the
  :mod:`ember_code.core.code_index.sync.schemas` Pydantic model)
  so the "needs link", "skipped", "error", and "success" branches
  live inside a single ``.to_command_result()`` method instead of
  being duplicated inline in ``_sync`` and ``_resync``.
  ``open_browser`` is passed as a *call argument* to
  ``.to_command_result()`` — never stored on the model — so
  Pydantic serialization stays clean.
* :class:`CodeIndexSearchView` — the ``/codeindex search`` result
  list.
* :class:`CodeIndexItemView` — the ``/codeindex item <id>`` card,
  including the 1500-char content preview cap.
* :class:`CodeIndexCommitsView` — the ``/codeindex commits``
  listing.
* :class:`CodeIndexHelpView` — the static ``/codeindex`` help
  markdown block; a classmethod-only view because there is no
  per-invocation state.

``SyncResult`` is now a Pydantic ``BaseModel`` (see
:mod:`ember_code.core.code_index.sync.schemas`) so the views
below wrap it directly. The :class:`ResolvedRepository`
dataclass and the ``CodeIndexItem`` / ``CodeIndexResult``
Pydantic models still need ``arbitrary_types_allowed=True``
because they carry non-``BaseModel`` fields.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ember_code.backend.command_result import CommandResult
from ember_code.core.code_index.sync.schemas import SyncResult

if TYPE_CHECKING:
    from ember_code.core.code_index.manifest import ManifestState
    from ember_code.core.code_index.resolver import ResolvedRepository
    from ember_code.core.code_index.schema.items import CodeIndexItem, CodeIndexResult


class CodeIndexHelpView(BaseModel):
    """Static ``/codeindex`` help block.

    Rendered when a subcommand is missing or unknown. Kept as a
    zero-field Pydantic model so the coordinator invokes it the
    same way as every other view (``.to_command_result()``).
    """

    @classmethod
    def to_command_result(cls) -> CommandResult:
        return CommandResult.markdown(
            "## CodeIndex\n"
            "Run `/codeindex` with no args to open the interactive status "
            "panel (current-commit indexed state + sync/clean/install "
            "actions, with a 2s live poll).\n"
            "- `/codeindex search <query>` — semantic search the head commit (chat output)\n"
            "- `/codeindex item <id>` — show full item details in chat\n"
            "- `/codeindex commits` — list indexed commits as markdown\n"
            "- `/codeindex clean` — drop stale, non-branch commits\n"
            "- `/codeindex sync [sha]` — pull and apply a changeset (defaults to HEAD)\n"
            "- `/codeindex resync [sha]` — wipe local state and pull a fresh snapshot\n"
            "- `/codeindex install` — open the GitHub App install page for this repo\n"
            "- `/codeindex status` — show sync state and install progress\n"
        )


class CodeIndexSearchView(BaseModel):
    """Wraps a ``list[CodeIndexResult]`` from ``code_index.search``.

    ``results`` is ``list[Any]`` on the model — we validate each
    entry's shape at the caller (it's a Pydantic ``CodeIndexResult``
    already) and only need attribute access here. Empty list is a
    valid caller state; the render method converts it to an
    ``info`` result rather than an empty markdown card.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ``CodeIndexResult`` is imported lazily via ``TYPE_CHECKING`` so
    # the schema module doesn't drag in ``core/code_index/schema/*``
    # (which pulls in Chroma) at import time.
    results: list[CodeIndexResult] = Field(default_factory=list)

    def to_command_result(self) -> CommandResult:
        if not self.results:
            return CommandResult.info("No results.")
        lines = f"## CodeIndex Search ({len(self.results)} results)\n"
        for i, r in enumerate(self.results, 1):
            score_str = f"{r.score:.3f}" if r.score is not None else "n/a"
            lines += (
                f"\n**{i}. {r.name}** (`{r.item_id}`)"
                f" — {r.path} (score {score_str})\n"
                f"{r.chunk_preview or ''}\n"
            )
        return CommandResult.markdown(lines)


class CodeIndexItemView(BaseModel):
    """Wraps a resolved :class:`CodeIndexItem` plus a preview cap.

    The 1500-char preview cap that the old ``_item`` free function
    applied inline is now the model's constant — one place to tune
    if the chat UI grows a bigger preview budget.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    PREVIEW_LIMIT: ClassVar[int] = 1500

    item: CodeIndexItem

    def to_command_result(self) -> CommandResult:
        preview = self.item.content
        if len(preview) > self.PREVIEW_LIMIT:
            preview = preview[: self.PREVIEW_LIMIT] + "..."
        return CommandResult.markdown(
            f"## {self.item.name}\n"
            f"- **id:** `{self.item.item_id}`\n"
            f"- **path:** {self.item.path}\n"
            f"- **type:** {self.item.type}\n"
            f"- **commit:** {self.item.commit}\n\n"
            f"```\n{preview}\n```"
        )


class CodeIndexCommitsView(BaseModel):
    """Wraps the manifest state for the ``/codeindex commits`` listing.

    ``state`` is the ``ManifestState`` dataclass returned by
    :meth:`CodeIndex.manifest.load` — kept as an opaque field
    because we only need attribute access here.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    state: ManifestState

    def to_command_result(self) -> CommandResult:
        if not self.state.commits:
            return CommandResult.info("No commits indexed.")
        lines = f"## Indexed Commits (head: `{self.state.head or 'none'}`)\n"
        for sha, info in sorted(
            self.state.commits.items(),
            key=lambda kv: kv[1].last_used_at,
            reverse=True,
        ):
            head_marker = " (HEAD)" if sha == self.state.head else ""
            branch = f" branches: {', '.join(info.branch_refs)}" if info.branch_refs else ""
            lines += f"\n- `{sha}`{head_marker} — last used {info.last_used_at}{branch}"
        return CommandResult.markdown(lines)


class CodeIndexStatusView(BaseModel):
    """Wraps the four SHAs + resolver state that ``/codeindex status``
    renders.

    All fields are optional strings so the "not a git repo" and
    "never synced" paths render as ``None`` sentinels — the old
    inline template used the same defaults.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    local_sha: str | None = None
    remote_url: str | None = None
    last_synced: str | None = None
    index_head: str | None = None
    resolved: ResolvedRepository | None = None

    def to_command_result(self) -> CommandResult:
        lines = "## CodeIndex Status\n"
        lines += f"- local HEAD: `{self.local_sha or 'not a git repo'}`\n"
        lines += f"- git remote: `{self.remote_url or 'not a git repo'}`\n"
        lines += f"- last synced: `{self.last_synced or 'never'}`\n"
        lines += f"- index head: `{self.index_head or 'none'}`\n"
        if self.resolved is None:
            lines += "- discovered: `not yet (run /codeindex sync)`\n"
        elif self.resolved.needs_install:
            lines += "- discovered: `install required`\n"
            lines += f"- install URL: `{self.resolved.install_url or 'unavailable'}`\n"
        else:
            lines += f"- discovered: `{self.resolved.repository_id}`\n"
        return CommandResult.markdown(lines)


class SyncCommandView(BaseModel):
    """Wraps a :class:`SyncResult` for the ``/codeindex sync`` output.

    The four-way branch (needs-link / skipped / error / success)
    that the old ``_sync`` free function inlined lives here, so
    :class:`ResyncCommandView` can subclass and reuse it with a
    single-line delta ("Wiped local index" prefix).

    ``open_browser`` is passed as a *call argument*, never stored,
    because Pydantic can't serialize a ``Callable`` field and we
    don't need to; the coordinator supplies its own callback.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    result: SyncResult

    @property
    def short_sha(self) -> str:
        return self.result.commit_sha[:8] if self.result.commit_sha else "?"

    def _wiped_prefix(self, _wiped: bool) -> str:
        # Overridden by :class:`ResyncCommandView`; the plain sync
        # variant never emits a wipe prefix.
        return ""

    def to_command_result(
        self, *, open_browser: Callable[[str], None], wiped: bool = False
    ) -> CommandResult:
        r = self.result
        if r.link_start_url:
            open_browser(r.link_start_url)
            return CommandResult.markdown(
                f"### CodeIndex needs setup\n"
                f"{r.reason}\n\n"
                f"Opening your browser to:\n"
                f"`{r.link_start_url}`\n\n"
                f"After the GitHub UI finishes, run `/codeindex sync` again."
            )
        if r.skipped:
            prefix = self._wiped_prefix(wiped)
            body = f"sync skipped: {r.reason}" if prefix else f"Sync skipped: {r.reason}"
            return CommandResult.info(f"{prefix}{body}")
        if r.error:
            return CommandResult.error(
                f"{self._error_verb()} of {self.short_sha} failed: {r.error}"
            )
        return self._render_success(wiped=wiped)

    def _error_verb(self) -> str:
        return "Sync"

    def _render_success(self, *, wiped: bool) -> CommandResult:
        stats = self.result.stats
        assert stats is not None  # succeeded ⇒ stats is set
        return CommandResult.info(
            f"Synced {self.short_sha}: "
            f"{stats.items_upserted} upserts, {stats.items_deleted} deletes, "
            f"{stats.references_upserted} refs."
        )


class ResyncCommandView(SyncCommandView):
    """Wraps a :class:`SyncResult` for the ``/codeindex resync`` output.

    Differs from :class:`SyncCommandView` in exactly three places:
    the success line reads "Resynced ... via snapshot", the error
    verb is "Resync", and skipped/success both prepend a "Wiped
    local index" phrase when the pre-sync ``forget_commit`` call
    actually dropped a commit.

    ``target_sha`` is the caller-resolved sha that seeded the
    snapshot — used to build ``short_sha`` when the server didn't
    echo back a ``commit_sha`` (e.g. skipped preflights).
    """

    target_sha: str = ""

    @property
    def short_sha(self) -> str:
        return (self.result.commit_sha or self.target_sha)[:8]

    def _wiped_prefix(self, wiped: bool) -> str:
        return "Wiped local index; " if wiped else ""

    def _error_verb(self) -> str:
        return "Resync"

    def _render_success(self, *, wiped: bool) -> CommandResult:
        stats = self.result.stats
        assert stats is not None  # succeeded ⇒ stats is set
        prefix = "Wiped local index. " if wiped else ""
        return CommandResult.info(
            f"{prefix}Resynced {self.short_sha} via snapshot: "
            f"{stats.items_upserted} upserts, "
            f"{stats.references_upserted} refs."
        )


__all__ = [
    "CodeIndexHelpView",
    "CodeIndexSearchView",
    "CodeIndexItemView",
    "CodeIndexCommitsView",
    "CodeIndexStatusView",
    "SyncCommandView",
    "ResyncCommandView",
]
