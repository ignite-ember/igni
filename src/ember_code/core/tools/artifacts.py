"""ArtifactTools — agent-facing operations on the project's artifacts.

An artifact is a document the agent authors and revises: a design
note, an understanding of a subsystem, a decision record. Every write
declares what it was built on, which is what makes "the source moved,
these three documents are now suspect" a question the store can
answer.

``sources`` is a required argument on both write tools and has no
default. That is deliberate and it is the point of the subsystem: the
moment an agent finishes writing is the only moment it still knows
which documents it drew on, and a parameter it can skip is a parameter
it will skip. When the declaration is missing, the error names the
exact refs this session read so the agent confirms a real list rather
than inventing one.

**Only one agent may write.** The toolkit is read-only unless built
with ``writable=True``, which only the archivist gets — see
:data:`ARCHIVIST`. Every other agent can search, read and follow
lineage, and has no tool that changes anything.

The reason is not safety, it is provenance. A write has to declare
what it was built on, and a declaration is only worth having if
somebody weighed it. An agent midway through a refactor, asked to
"note that down", will write something true and cite nothing —
because citing is not what it is doing. Routing writes through an
agent whose whole job is the document makes that judgement the
foreground task rather than an interruption to one.

This costs a delegation round-trip on every write, and that is the
trade being made. It is affordable because the capture ledger is
per-session and shared: whatever the main agent read is still offered
to the archivist as candidate sources, so delegating does not lose
the provenance that made the write worth recording.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from ember_code.core.artifacts.errors import SourcesRequired
from ember_code.core.artifacts.models import EdgeState

if TYPE_CHECKING:
    from ember_code.core.session.artifact_ops import SessionArtifactManager

logger = logging.getLogger(__name__)

# Long documents make the tool response the dominant cost of a turn.
# The agent asks for a version by ref when it needs the whole thing.
PREVIEW_CHARS: int = 1200


#: The one agent allowed to change anything.
#:
#: Named here rather than read from a config file, because "who may
#: write" is not a preference. A frontmatter key would let any agent
#: definition grant itself the capability, which is the same as not
#: having the rule.
ARCHIVIST: str = "archivist"


class ArtifactTools(Toolkit):
    """Agent-facing tools for the project's versioned artifacts.

    Read-only by default. ``writable=True`` adds the tools that
    change something — writing, reviewing captured sources,
    reconciling, publishing, deleting — and only the archivist is
    built that way.
    """

    def __init__(self, artifact_mgr: SessionArtifactManager, *, writable: bool = False):
        super().__init__(name="igni_artifacts")
        self._mgr = artifact_mgr
        self.writable = writable

        # Reading is for everyone. An agent that cannot look things up
        # writes worse code, and none of this can change a document.
        self.register(self.artifact_search)
        self.register(self.artifact_read)
        self.register(self.artifact_list)
        self.register(self.artifact_versions)
        self.register(self.artifact_lineage)
        self.register(self.artifact_ask)
        self.register(self.artifact_status)

        if not writable:
            return

        self.register(self.artifact_cypher)
        self.register(self.artifact_create)
        self.register(self.artifact_revise)
        self.register(self.artifact_pending_sources)
        self.register(self.artifact_confirm_source)
        self.register(self.artifact_dismiss_source)
        self.register(self.artifact_stale)
        self.register(self.artifact_reconcile)
        self.register(self.artifact_repin)
        self.register(self.artifact_delete)

    # ── Reads ─────────────────────────────────────────────────────────

    async def artifact_search(self, query: str, limit: int = 5) -> str:
        """Search the project's artifacts for relevant documents.

        Returns the current version of each match. Every result is
        recorded as something you read, so it can be offered back to
        you as a candidate source next time you write.

        Args:
            query: Natural-language search query.
            limit: Max results to return (default 5).
        """
        response = await self._mgr.search(query, limit=limit)
        if not response.results:
            return f"No artifacts found for: {query}"
        lines = [f"Found {response.total} artifact(s):"]
        for i, r in enumerate(response.results, 1):
            body = _preview(r.content)
            lines.append(f"\n{i}. {r.name or 'untitled'} [{r.ref}]\n{body}")
        return "\n".join(lines)

    async def artifact_read(self, ref: str, version: int = 0) -> str:
        """Read one artifact in full, at a specific version.

        Args:
            ref: Artifact id, ``id@n``, or name.
            version: Version number; 0 (default) reads the current one.
        """
        found = await self._mgr.version(ref, version, record=True)
        if found is None:
            return f"No artifact matches '{ref}'."
        header = f"{found.ref} (v{found.n}{'  — current' if found.head else ''})"
        return f"{header}\n\n{found.content}"

    async def artifact_list(self, limit: int = 50) -> str:
        """List the project's artifacts with their current version.

        Args:
            limit: Max artifacts to list (default 50).
        """
        artifacts = await self._mgr.list_artifacts(limit=limit)
        if not artifacts:
            return "No artifacts yet."
        lines = [f"{len(artifacts)} artifact(s):"]
        for a in artifacts:
            imported = " (imported)" if a.is_imported else ""
            lines.append(
                f"- {a.name or a.artifact_id} [{a.artifact_id}] "
                f"v{a.head_n}, {a.version_count} version(s){imported}"
            )
        return "\n".join(lines)

    async def artifact_versions(self, ref: str) -> str:
        """Show the revision history of one artifact.

        Args:
            ref: Artifact id or name.
        """
        versions = await self._mgr.versions(ref)
        if not versions:
            return f"No artifact matches '{ref}'."
        lines = [f"{len(versions)} version(s) of {ref}:"]
        for v in versions:
            marker = " (current)" if v.head else ""
            when = v.created_at or "unknown date"
            lines.append(f"- v{v.n}{marker} — {when}, {len(v.content)} chars")
        return "\n".join(lines)

    async def artifact_lineage(self, ref: str, version: int = 0) -> str:
        """Show what an artifact was built on, and what sits on top of it.

        Args:
            ref: Artifact id or name.
            version: Version to inspect; 0 (default) is the current one.
        """
        lineage = await self._mgr.lineage(ref, version)
        if lineage is None:
            return f"No artifact matches '{ref}'."
        lines = [f"Lineage of {ref} v{lineage.n}:"]
        confirmed = [e for e in lineage.sources if e.state is EdgeState.CONFIRMED]
        lines.append("\nBuilt on:")
        if not confirmed:
            lines.append("  (nothing declared)")
        for e in confirmed:
            flag = f"  ⚠ source has moved to v{e.source_head_n}" if e.is_stale else ""
            lines.append(f"  - {e.to_name or e.to_artifact_id}@{e.pinned_n}{flag}")
        if lineage.citations:
            lines.append("\nExternal sources:")
            lines.extend(f"  - {c.url}" for c in lineage.citations)
        if lineage.candidates:
            lines.append("\nCaptured but unconfirmed (read while this was written):")
            lines.extend(
                f"  - {e.to_name or e.to_artifact_id}@{e.pinned_n}" for e in lineage.candidates
            )
        if lineage.dependents:
            lines.append("\nDepended on by:")
            lines.extend(
                f"  - {e.from_name or e.from_artifact_id}@{e.from_n}" for e in lineage.dependents
            )
        return "\n".join(lines)

    async def artifact_ask(self, question: str) -> str:
        """Ask a structural question about the artifact graph.

        Use this when the question is about how documents relate
        rather than what they say — artifact_search finds documents
        *about* a topic, this answers questions *about the documents*:
        what was built on what, what would be affected by a change,
        what nobody has reviewed, which version said what.

        A query is written for the question and run read-only against
        the local graph. The answer includes the query, so a wrong
        result can be diagnosed rather than just disbelieved.

        Args:
            question: Plain English, e.g. "what would be affected if
                the auth model changed" or "which documents have
                sources nobody confirmed".
        """
        return await self._mgr.ask_graph(question)

    async def artifact_cypher(self, cypher: str, semantic_query: str = "") -> str:
        """Run a Cypher query you wrote yourself against the graph.

        Use this when you know the shape of the answer and
        artifact_ask's paraphrase keeps missing it. The query runs
        read-only; a write is refused before it reaches the database.

        The graph schema is in your instructions. Two rules that
        account for most wrong answers: filter `v.head` unless the
        question is about history, and return named columns rather
        than whole nodes.

        Args:
            cypher: The query. Must include a LIMIT.
            semantic_query: Text to embed and bind as `$query_vector`,
                for queries that call the vector index. You cannot
                write a 384-dimension literal, so this is how that
                index is reached.
        """
        result = await self._mgr.run_cypher(cypher, semantic_query=semantic_query)
        if not result.ok:
            return f"Query refused or failed: {result.error}"
        if not result.rows:
            return "No rows. The query ran; nothing matched."
        lines = [f"{len(result.rows)} row(s):"]
        for row in result.rows[:40]:
            lines.append("  " + ", ".join(f"{k}={v}" for k, v in row.items()))
        if len(result.rows) > 40:
            lines.append(f"  … and {len(result.rows) - 40} more")
        if result.truncated:
            lines.append("  (result was capped)")
        return "\n".join(lines)

    async def artifact_pending_sources(self) -> str:
        """List the artifacts you have read this session.

        Call this before writing when you are unsure what to declare
        as sources — it is the exact set that will otherwise be
        captured as unconfirmed candidates.
        """
        pending = self._mgr.pending_sources()
        if not pending:
            return "You have not read any artifacts in this session yet."
        lines = ["Read this session (use these refs as sources where they apply):"]
        lines.extend(f"  - {ref}" for ref in pending)
        return "\n".join(lines)

    # ── Writes ────────────────────────────────────────────────────────

    async def artifact_create(
        self,
        name: str,
        content: str,
        sources: list[str],
        kind: str = "note",
        no_sources_reason: str = "",
    ) -> str:
        """Create a new artifact, declaring what it was built on.

        ``sources`` is required. List every artifact and external
        document this content actually draws on — that list becomes
        the provenance graph, and it is how anyone later finds out
        that this document needs revisiting when one of its sources
        changes.

        Args:
            name: Human-readable title. Also the artifact's identity —
                reuse it with artifact_revise rather than creating a
                near-duplicate name.
            content: The document body.
            sources: Artifact refs (``id`` or ``id@n``) and full URLs
                this was built on. Call artifact_pending_sources if
                you are unsure what you read.
            kind: Free-form label, e.g. ``note``, ``design``,
                ``decision``, ``summary``. Default ``note``.
            no_sources_reason: Required only when ``sources`` is empty
                — say why this document has no antecedent (e.g. "first
                observation, nothing read").
        """
        try:
            result = await self._mgr.create(
                name=name,
                content=content,
                sources=sources,
                kind=kind,
                no_sources_reason=no_sources_reason,
            )
        except SourcesRequired as exc:
            return _sources_required_message(exc)
        return result.message if result.success else f"Error: {result.error}"

    async def artifact_revise(
        self,
        ref: str,
        content: str,
        sources: list[str],
        no_sources_reason: str = "",
    ) -> str:
        """Add a new version of an existing artifact.

        The previous version is kept and stays addressable — this
        appends, it does not overwrite. Declare the sources *this
        revision* drew on, which may differ from the ones v1 used.

        Args:
            ref: Artifact id or name to revise.
            content: The full new body (not a patch).
            sources: Artifact refs and URLs this revision was built on.
            no_sources_reason: Required only when ``sources`` is empty.
        """
        try:
            result = await self._mgr.revise(
                ref=ref,
                content=content,
                sources=sources,
                no_sources_reason=no_sources_reason,
            )
        except SourcesRequired as exc:
            return _sources_required_message(exc)
        return result.message if result.success else f"Error: {result.error}"

    # ── Lineage maintenance ───────────────────────────────────────────

    async def artifact_confirm_source(self, ref: str, source_ref: str) -> str:
        """Confirm that an artifact really was built on a source.

        Promotes a captured candidate to a real derivation. Only
        confirmed sources raise a staleness warning when they change.

        Args:
            ref: The dependent artifact.
            source_ref: The source it was built on.
        """
        ok = await self._mgr.confirm_source(ref=ref, source_ref=source_ref)
        if not ok:
            return f"No captured link from '{ref}' to '{source_ref}'."
        return f"Confirmed: {ref} is built on {source_ref}."

    async def artifact_dismiss_source(self, ref: str, source_ref: str) -> str:
        """Record that a captured read was *not* a real source.

        Args:
            ref: The artifact that was written.
            source_ref: The candidate source to dismiss.
        """
        ok = await self._mgr.dismiss_source(ref=ref, source_ref=source_ref)
        if not ok:
            return f"No captured link from '{ref}' to '{source_ref}'."
        return f"Dismissed: {ref} is not built on {source_ref}."

    async def artifact_stale(self, limit: int = 20) -> str:
        """List artifacts whose sources have changed since they were written.

        Args:
            limit: Max rows to return (default 20).
        """
        stale = await self._mgr.stale(limit=limit)
        if not stale:
            return "Nothing is out of date — every confirmed source is at its pinned version."
        lines = [f"{len(stale)} artifact(s) built on a source that has since changed:"]
        lines.extend(f"  - {s.summary}" for s in stale)
        lines.append("\nCall artifact_reconcile with the dependent's ref to see the diff.")
        return "\n".join(lines)

    async def artifact_reconcile(
        self,
        ref: str,
        source_ref: str = "",
        updated_content: str = "",
    ) -> str:
        """Inspect or resolve an out-of-date artifact.

        Without ``updated_content`` this returns the diff of what
        changed in the source since this artifact was pinned to it —
        read that first. With ``updated_content`` it stores a new
        version pinned to the source's current version. If the change
        turns out not to affect this document, use artifact_repin
        instead of inventing an edit.

        Args:
            ref: The out-of-date artifact.
            source_ref: Which source to reconcile against. Optional
                when the artifact has only one stale source.
            updated_content: The revised body. Leave empty to inspect.
        """
        stale = await self._mgr.stale(limit=200)
        target = _match_stale(stale, ref=ref, source_ref=source_ref)
        if target is None:
            return f"'{ref}' has no out-of-date sources" + (
                f" matching '{source_ref}'." if source_ref else "."
            )
        plan = await self._mgr.reconcile_plan(target)
        if plan is None:
            return f"Could not load the versions needed to reconcile '{ref}'."
        if not updated_content.strip():
            return (
                f"{plan.dependent_name or plan.dependent_artifact_id} is pinned to "
                f"{plan.source_name or plan.source_artifact_id}@{plan.pinned_n}; "
                f"that source is now at v{plan.source_head_n}.\n\n"
                f"What changed in the source:\n{plan.source_diff}\n\n"
                f"Current content of {plan.dependent_name or plan.dependent_artifact_id}:\n"
                f"{plan.dependent_content}\n\n"
                "Call artifact_reconcile again with updated_content to store a "
                "corrected version. If the source change does not affect this "
                "document, call artifact_repin to move the pin forward and "
                "leave the content alone."
            )
        result = await self._mgr.accept_reconcile(plan=plan, content=updated_content)
        return result.message if result.success else f"Error: {result.error}"

    async def artifact_repin(self, ref: str, source_ref: str = "") -> str:
        """Record that a source change does not affect this artifact.

        Moves the pin to the source's current version without writing
        a new document version. Use it only after reading the diff via
        artifact_reconcile — the point of the pin is that someone
        looked.

        Args:
            ref: The out-of-date artifact.
            source_ref: Which source to re-pin against. Optional when
                the artifact has only one stale source.
        """
        stale = await self._mgr.stale(limit=200)
        target = _match_stale(stale, ref=ref, source_ref=source_ref)
        if target is None:
            return f"'{ref}' has no out-of-date sources to re-pin."
        plan = await self._mgr.reconcile_plan(target)
        if plan is None:
            return f"Could not load the versions needed to re-pin '{ref}'."
        result = await self._mgr.accept_reconcile(plan=plan)
        return result.message if result.success else f"Error: {result.error}"

    # ── Admin ─────────────────────────────────────────────────────────

    async def artifact_delete(self, ref: str, confirm: bool = False) -> str:
        """Delete an artifact and all of its versions.

        Args:
            ref: Artifact id or name.
            confirm: Must be True to actually delete.
        """
        if not confirm:
            return (
                f"Preview mode: to delete '{ref}' and every version of it, "
                f"call artifact_delete again with confirm=True."
            )
        deleted = await self._mgr.delete(ref)
        if not deleted:
            return f"No artifact matches '{ref}'."
        return f"Deleted '{ref}' and its version history."

    async def artifact_status(self) -> str:
        """Check the state of the project's artifact store."""
        status = await self._mgr.status()
        if not status.enabled:
            return "Artifacts are disabled."
        lines = [
            f"Artifacts: {status.document_count}",
            f"Versions: {status.version_count}",
            f"Embedder: {status.embedder}",
        ]
        if status.stale_count:
            lines.append(f"Out of date: {status.stale_count} (see artifact_stale)")
        if status.pending_candidates:
            lines.append(
                f"Unconfirmed captured sources: {status.pending_candidates} (see artifact_lineage)"
            )
        return "\n".join(lines)


def _preview(content: str) -> str:
    if len(content) <= PREVIEW_CHARS:
        return content
    return content[:PREVIEW_CHARS] + f"\n… ({len(content) - PREVIEW_CHARS} more chars)"


def _sources_required_message(exc: SourcesRequired) -> str:
    """Turn a refused write into an answerable question.

    Listing the session's reads is the difference between the agent
    confirming a real corpus and the agent guessing at one.
    """
    lines = [
        "Error: this write needs a source declaration.",
        "",
        "Pass `sources` listing every artifact ref and URL this content was "
        "built on. If it genuinely has no antecedent, pass sources=[] together "
        "with no_sources_reason explaining why.",
    ]
    if exc.candidates:
        lines.append("")
        lines.append("You have read these artifacts this session:")
        lines.extend(f"  - {ref}" for ref in exc.candidates)
        lines.append("")
        lines.append(
            "Include the ones this document actually draws on. The rest will be "
            "recorded as unconfirmed candidates, not as real sources."
        )
    else:
        lines.append("")
        lines.append(
            "You have not read any artifacts this session, so sources=[] with a "
            "no_sources_reason is likely correct here."
        )
    return "\n".join(lines)


def _match_stale(stale: list, *, ref: str, source_ref: str):
    """Find the stale row a reconcile call is talking about.

    Matching on id *or* name in both positions because the agent
    refers to artifacts by whatever it last saw them called.
    """
    needle = (ref or "").strip().casefold()
    source_needle = (source_ref or "").strip().casefold()
    for row in stale:
        dependent_match = needle in {
            row.dependent_artifact_id.casefold(),
            (row.dependent_name or "").casefold(),
        }
        if not dependent_match:
            continue
        if not source_needle:
            return row
        if source_needle in {
            row.source_artifact_id.casefold(),
            (row.source_name or "").casefold(),
        }:
            return row
    return None
