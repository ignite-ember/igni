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

**One agent holds this toolkit**, reads included — the archivist, see
:data:`ARCHIVIST`. No other agent has any artifact tool at all.

Two reasons, and the second is the one that decided it.

*Provenance.* A write has to declare what it was built on, and a
declaration is only worth having if somebody weighed it. An agent
midway through a refactor, asked to "note that down", writes
something true and cites nothing — not carelessly, but because citing
is not the task it is doing. Reading belongs with writing because
they share one model: versions, pins, what "head" means, why a
captured source is not a confirmed one.

*Attributable traces.* A capability spread across every agent makes
every agent's manifest claim it, and a trace that calls the tool can
then belong to any of them. That is not hypothetical: the corpus
already carries 1,370 specialist traces whose manifests had to be
rebuilt, and 823 that had to be relabelled to the one role that
really held the tool they called. ``CodeIndexTools`` is the shape
that works — one function, one agent, one grant. This matches it.

The cost is a delegation round-trip on every lookup, and it is real.
What makes it affordable is that the ledger and the reads now live in
the same agent: the archivist cites what the archivist read, with no
cross-agent hand-off to get right.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from ember_code.core.artifacts.errors import DeleteRefused, SourcesRequired
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

    Built for the archivist and nobody else, so there is no
    read-only variant: an agent that can follow lineage but cannot act
    on it is half-informed, and the split put the two halves of one
    model in two places.
    """

    def __init__(self, artifact_mgr: SessionArtifactManager):
        super().__init__(name="igni_artifacts")
        self._mgr = artifact_mgr

        self.register(self.artifact_search)
        self.register(self.artifact_read)
        self.register(self.artifact_list)
        self.register(self.artifact_versions)
        self.register(self.artifact_lineage)
        self.register(self.artifact_ask)
        self.register(self.artifact_status)
        self.register(self.artifact_cypher)
        self.register(self.artifact_create)
        self.register(self.artifact_edit_draft)
        self.register(self.artifact_publish_draft)
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
        """Search the project's documents for relevant passages.

        Each result is one **passage**, with the id you cite if you
        write from it. Pass that id in ``sources`` when you create or
        revise a document: it records which paragraph you drew on, so
        a reader can go and check your reasoning, and so you can be
        told later if that exact paragraph changes.

        Citing the document id alone still works and is weaker — it
        says this was built on that, which nobody can verify.

        Args:
            query: Natural-language search query.
            limit: Max results to return (default 5).
        """
        response = await self._mgr.search(query, limit=limit)
        if not response.results:
            return f"No artifacts found for: {query}"
        lines = [f"Found {response.total} passage(s):"]
        for i, r in enumerate(response.results, 1):
            body = _preview(r.content)
            where = r.metadata.get("line_from", "")
            at = f" lines {where}-{r.metadata.get('line_to', '')}" if where else ""
            # The citable id on its own line and labelled, because the
            # agent has to copy it exactly and a ref buried in a
            # heading gets paraphrased.
            cite = f"\n   cite as: {r.chunk_id}" if r.chunk_id else ""
            lines.append(f"\n{i}. {r.name or 'untitled'} [{r.ref}]{at}{cite}\n{body}")
        return "\n".join(lines)

    async def artifact_read(self, ref: str, version: int = 0) -> str:
        """Read one artifact in full, at a specific version.

        The current version comes marked with the id of each passage,
        on a ``cite as:`` line where the passage starts — the same ids
        search shows. Cite the passage you drew on, as from search.
        Passages overlap, so a line may sit in two; either id is right.
        Older versions keep their text but not their passages: cite one
        as ``id@n``.

        Args:
            ref: Artifact id, ``id@n``, or name.
            version: Version number; 0 (default) reads the current one.
        """
        found = await self._mgr.version(ref, version, record=True)
        if found is None:
            return f"No artifact matches '{ref}'."
        header = f"{found.ref} (v{found.n}{'  — current' if found.head else ''})"
        if found.head:
            marked = await self._with_passage_ids(ref, found.n)
            if marked is not None:
                return f"{header}\n\n{marked}"
        else:
            header += f" — an older version: cite it as {found.ref}"
        return f"{header}\n\n{found.content}"

    async def _with_passage_ids(self, ref: str, n: int) -> str | None:
        """The server's text of version `n`, a ``cite as:`` line before each passage.

        The server's text, not this client's copy: the spans are lines
        of what the server chunked, and marking another text with them
        would put an id beside the wrong paragraph. None — the plain
        read — when the server has no passages for exactly this
        version, so an id is never shown for text it does not match.
        """
        body = await self._mgr.passages(ref)
        if not body or int(body.get("n") or 0) != n or not body.get("passages"):
            return None
        lines = str(body.get("content") or "").split("\n")
        starts: dict[int, list[str]] = {}
        for passage in body["passages"]:
            starts.setdefault(int(passage["line_from"]), []).append(str(passage["chunk_id"]))
        out: list[str] = []
        for number, line in enumerate(lines, start=1):
            for chunk_id in starts.get(number, []):
                out.append(f"[cite as: {chunk_id}]")
            out.append(line)
        return "\n".join(out)

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

        ``sources`` is required. List every document and external
        page this content actually draws on — that list becomes the
        provenance record, and it is how anyone later finds out that
        this document needs revisiting when one of its sources
        changes.

        **Prefer the passage id.** ``artifact_search`` prints a
        ``cite as:`` line per result; passing that cites the exact
        paragraph you used, which a reader can render and judge. A
        bare document id is accepted and says much less — that this
        was built on that, with nothing to check.

        **Mark the text too.** Where a sentence draws on a passage,
        write ``[cite:<passage id>]`` at the end of it. That records
        which paragraph of *yours* came from which of theirs — the
        ``sources`` list cannot, because it is about the document as a
        whole. Readers never see the markers; they are stripped when
        the document is shown.

        Put a marker on the same line as the text it marks. One split
        across a line break is not a citation and will be ignored.

        Args:
            name: Human-readable title. Also the artifact's identity —
                reuse it with artifact_revise rather than creating a
                near-duplicate name.
            content: The document body. May carry ``[cite:<passage
                id>]`` markers where a sentence draws on a passage —
                see above.
            sources: Passage ids from ``artifact_search`` (preferred),
                artifact refs (``id`` or ``id@n``), and full URLs this
                was built on. Call artifact_pending_sources if you are
                unsure what you read.
            kind: Free-form label, e.g. ``note``, ``design``,
                ``decision``, ``summary``. Default ``note``.
            no_sources_reason: Required only when ``sources`` is empty
                — say why this document has no antecedent (e.g. "first
                observation, nothing read").
        """
        try:
            result = await self._mgr.create(
                name=name,
                content=_without_read_markers(content),
                sources=sources,
                kind=kind,
                no_sources_reason=no_sources_reason,
            )
        except SourcesRequired as exc:
            return _sources_required_message(exc)
        return result.message if result.success else f"Error: {result.error}"

    async def artifact_edit_draft(self, ref: str, content: str) -> str:
        """Change a document without making a version of it.

        **This is how to change a document somebody asked you to
        change.** The edit goes to the draft — one per document,
        shared by everybody who can read it — and the person who
        asked decides when it becomes a version. Use it for anything
        that came from a comment or a request: they are still
        thinking about the wording, and a version published under
        them is one they did not agree to.

        Writing the draft again replaces it. There is only ever one.

        Args:
            ref: Artifact id or name to edit.
            content: The full new body (not a patch).
        """
        result = await self._mgr.write_draft(ref=ref, content=_without_read_markers(content))
        return result.message if result.success else f"Error: {result.error}"

    async def artifact_publish_draft(self, ref: str) -> str:
        """Turn a document's draft into a new version.

        Only when you were asked to publish it. Writing the draft and
        publishing it are two acts on purpose: the second one is a
        decision about what everybody reads, and it belongs to
        whoever asked for the change.

        Args:
            ref: Artifact id or name whose draft to publish.
        """
        result = await self._mgr.publish_draft(ref=ref)
        return result.message if result.success else f"Error: {result.error}"

    async def artifact_revise(
        self,
        ref: str,
        content: str,
        sources: list[str],
        no_sources_reason: str = "",
    ) -> str:
        """Add a new version of an existing artifact, immediately.

        For your own records — a note you keep, a document you are
        revising because you learned something, with nobody waiting
        on the wording. **If somebody asked for the change, use
        ``artifact_edit_draft`` instead**, so they publish it rather
        than finding it published.

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
                content=_without_read_markers(content),
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
        result = await self._mgr.accept_reconcile(
            plan=plan, content=_without_read_markers(updated_content)
        )
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
        try:
            deleted = await self._mgr.delete(ref)
        except DeleteRefused as exc:
            return f"Error: {exc}"
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


#: The passage labels `artifact_read` puts in what it shows.
_READ_MARKER = re.compile(r"^\[cite as: [^\]\n]+\]\n?", re.MULTILINE)


def _without_read_markers(content: str) -> str:
    """`content` without the ``[cite as: …]`` lines a read adds.

    They label what the agent was shown and are not part of any
    document. An agent revising a page often starts from the text it
    read, and without this every revision would store the labels as
    prose — where the next read would show them as the page's own.
    """
    return _READ_MARKER.sub("", content)


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
