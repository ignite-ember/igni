"""Typed wire schemas for the artifacts panel.

Consolidates every artifact wire type in one place, matching the
existing ``schemas_run.py`` / ``schemas_pause.py`` convention so the
controller module (``server_artifacts.py``) stays focused on
behaviour.

These types are the privacy boundary for the panel the same way
``ExportBundle.to_payload`` is for the share path: they are
hand-declared, so a field added to the store does not reach a client
until someone decides it should.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ArtifactStatus(BaseModel):
    """Panel header. ``embedder`` carries the active embedding
    provider (empty when artifacts are disabled)."""

    enabled: bool
    collection_name: str = ""
    document_count: int = 0
    version_count: int = 0
    stale_count: int = 0
    pending_candidates: int = 0
    embedder: str = ""


class ArtifactHit(BaseModel):
    """One search hit. ``metadata`` values are stringified at wire
    time so the FE never has to guess types."""

    artifact_id: str = ""
    name: str = ""
    kind: str = ""
    n: int = 0
    content: str = ""
    score: float | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class ArtifactListEntry(BaseModel):
    """One row of the Browse tab — ``preview`` renders the collapsed
    row and the rest is pulled on click."""

    id: str
    name: str
    source: str = ""
    kind: str = ""
    n: int = 0
    version_count: int = 0
    size: int = 0
    preview: str = ""
    added_at: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class ArtifactGetResult(BaseModel):
    """One artifact's full detail at one version.

    Error paths (disabled, not found, no such version) populate
    ``error`` and leave the content fields empty so the panel renders
    a clear message instead of a blank document.
    """

    id: str = ""
    name: str = ""
    source: str = ""
    kind: str = ""
    n: int = 0
    head_n: int = 0
    is_head: bool = False
    #: Archived, and therefore frozen. The page has to say so: finding
    #: out that a document cannot change by pressing something and
    #: being refused is one round trip too many.
    archived: bool = False
    content: str = ""
    created_at: str = ""
    author: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    error: str = ""


class ArtifactVersionRow(BaseModel):
    """One entry in the version switcher."""

    n: int
    created_at: str = ""
    author: str = ""
    size: int = 0
    is_head: bool = False


class ArtifactLineageRow(BaseModel):
    """One derivation edge, in whichever direction it was asked for.

    ``pinned_n`` and ``head_n`` are both sent so the panel can render
    "pinned to v1, now at v3" without a second round trip, which is
    the whole question a reader has about a source.
    """

    artifact_id: str
    name: str = ""
    n: int = 0
    pinned_n: int = 0
    head_n: int = 0
    state: str = "candidate"
    is_stale: bool = False


class ArtifactLineageView(BaseModel):
    """Both directions around one version."""

    artifact_id: str
    n: int = 0
    sources: list[ArtifactLineageRow] = Field(default_factory=list)
    dependents: list[ArtifactLineageRow] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class ArtifactStaleRow(BaseModel):
    """One out-of-date dependency for the review list."""

    artifact_id: str
    name: str = ""
    n: int = 0
    source_artifact_id: str = ""
    source_name: str = ""
    pinned_n: int = 0
    source_head_n: int = 0
    versions_behind: int = 0


class ProvenanceMatchRow(BaseModel):
    """One source passage a passage of this document resembles."""

    source_artifact_id: str = ""
    source_name: str = ""
    source_n: int = 0
    source_line_from: int = 0
    source_line_to: int = 0
    source_text: str = ""
    #: Cosine similarity. Surfaced so the UI can say how sure this is
    #: rather than drawing every line with the same weight — and shown
    #: outright beside the label, because "close" is an opinion and
    #: 0.78 is the thing the opinion was formed from.
    similarity: float = 0.0
    #: That same number in words. Banded here rather than in the
    #: browser: the cuts are an argument about what the measurements
    #: in :mod:`ember_code.core.artifacts.provenance` support, and two
    #: copies of an argument is one copy that goes stale.
    strength: str = ""
    #: The source text this resembles has changed since it was pinned.
    changed: bool = False


class ProvenancePassageRow(BaseModel):
    """One passage of the document, and what it traces to."""

    chunk_id: str = ""
    line_from: int = 0
    line_to: int = 0
    text: str = ""
    matches: list[ProvenanceMatchRow] = Field(default_factory=list)
    affected: bool = False


class ArtifactProvenance(BaseModel):
    """Passage-level provenance for one version of one document.

    ``inferred`` is always true and is sent anyway: the UI has to say
    so. Nothing records which passage came from which, so these are
    resemblances between passages, and a line drawn with the
    confidence of a recorded fact would be a lie about what is known.
    """

    artifact_id: str = ""
    n: int = 0
    passages: list[ProvenancePassageRow] = Field(default_factory=list)
    #: Sources considered, so the UI can colour by source.
    sources: list[str] = Field(default_factory=list)
    #: Passages whose source text has since changed.
    affected_count: int = 0
    inferred: bool = True
    #: Chunks that carry no vector yet, so cannot be placed. Non-zero
    #: means the answer is incomplete and says which way.
    unplaced: int = 0
    error: str = ""


class CandidateOverlapRow(BaseModel):
    """One place this document and a captured read say the same thing."""

    here_text: str = ""
    here_line_from: int = 0
    here_line_to: int = 0
    there_text: str = ""
    there_line_from: int = 0
    there_line_to: int = 0
    similarity: float = 0.0
    strength: str = ""


class ArtifactCandidateEvidence(BaseModel):
    """What there is to go on when deciding whether a read was a source.

    A captured read is a name and a version number, which is not
    enough to accept or reject it — the reviewer is being asked
    whether this document was built on that one and given nothing
    about either. This is the evidence: where the two texts say
    similar things, how similar, and how much of this document is
    involved at all.

    It is the same passage matching the trace view uses, pointed at an
    *unconfirmed* source on purpose. Refusing to guess there was right
    — a guess refining a guess produces two stacked and one line on
    screen — but here the guess is the question being asked, and
    "nothing in this document resembles it" is the most useful answer
    a reviewer can get.
    """

    artifact_id: str = ""
    source_artifact_id: str = ""
    source_name: str = ""
    #: The version that was read, which is what the decision is about.
    source_n: int = 0
    #: When the read was captured.
    captured_at: str = ""
    overlaps: list[CandidateOverlapRow] = Field(default_factory=list)
    #: Passages of this document that resemble anything there, out of
    #: how many. "2 of 14" is a different answer from "11 of 14".
    passages_touched: int = 0
    total_passages: int = 0
    #: Chunks with no vector yet. Non-zero means the answer is
    #: incomplete and says which way.
    unplaced: int = 0
    error: str = ""


class ArtifactCandidateVerdict(BaseModel):
    """The agent's read on whether a captured read was a source.

    Advisory, and sent as such. Nothing here writes an edge — the two
    buttons still do, and they still mean what they meant. What
    changes is that the reviewer is now accepting or rejecting a
    recommendation with reasons, instead of adjudicating a similarity
    score on their own.
    """

    #: ``source`` | ``not-a-source`` | ``unclear``.
    recommendation: str = "unclear"
    #: Three sentences at most, naming what carries the resemblance.
    reason: str = ""
    error: str = ""


class DriftAffectedRow(BaseModel):
    """A passage of the dependent that leans on something that moved."""

    text: str = ""
    line_from: int = 0
    line_to: int = 0
    source_text: str = ""
    strength: str = ""


class ArtifactDrift(BaseModel):
    """How far a document has drifted from what it was built on.

    Named for the branch situation it describes rather than for
    staleness: the version chain of a document is not a dependency,
    and a revision is a commit on the branch you are working on. This
    is about the *other* branch having moved.
    """

    dependent_artifact_id: str = ""
    dependent_name: str = ""
    dependent_n: int = 0
    source_artifact_id: str = ""
    source_name: str = ""
    #: The merge base — the version this was written against.
    pinned_n: int = 0
    #: Where the source is now.
    source_head_n: int = 0
    versions_behind: int = 0
    #: One line, in the terms the reader already has.
    summary: str = ""
    #: Passages of the dependent that resemble the changed parts.
    affected: list[DriftAffectedRow] = Field(default_factory=list)
    #: Whether the two have actually parted company. False means the
    #: document is pinned to the source's current version and there is
    #: nothing to decide.
    has_diverged: bool = False
    error: str = ""


class ArtifactDriftApplied(BaseModel):
    """The outcome of bringing a document up to date with its source.

    Two halves, and the second is the one that is easy to forget: a
    version is written *and* the ``DERIVED_FROM`` edge is re-pinned.
    ``pinned_to`` is reported so the caller can say which, rather than
    leaving the reader to check whether the warning will return.
    """

    ok: bool = False
    #: The version that was written, when one was.
    n: int = 0
    #: Where the edge now points.
    pinned_to: int = 0
    #: True when the text was already correct and only the pin moved.
    #: A real outcome, not a failure — and not worth writing an
    #: identical version to record.
    text_unchanged: bool = False
    message: str = ""
    error: str = ""


class ArtifactCommentPosted(BaseModel):
    """A comment written here, and what to ask the agent.

    ``prompt`` is handed back rather than sent from here so the client
    can post it as an ordinary chat turn. That keeps what the agent
    was told on screen next to what it did — an instruction assembled
    out of sight is one nobody can check.
    """

    ok: bool = False
    comment_id: str = ""
    thread_id: str = ""
    #: The turn text. Empty when ``ok`` is false.
    prompt: str = ""
    #: How many earlier comments were folded into ``prompt`` as
    #: context, so the UI can say "answering with 4 earlier comments
    #: in view" rather than leaving it a mystery.
    history_count: int = 0
    message: str = ""


class ArtifactCommentRow(BaseModel):
    """One comment, as the review screen needs it.

    Carries ``document_name`` alongside the id because the review
    screen is cross-document by nature — it answers "what is waiting
    on me", and a list of bodies with no document beside them is a
    list nobody can triage.
    """

    comment_id: str
    artifact_id: str
    document_name: str = ""
    n: int = 0
    thread_id: str = ""
    #: Empty for the comment that opened a thread, the root's id for a
    #: reply. Sent because a thread read back out of the store is a
    #: conversation, and a flat list of bodies in ``at`` order is not
    #: one — the answer has to sit under the question.
    parent_id: str = ""
    #: ``web`` — a person, elsewhere. ``local`` — a person, here.
    #: ``agent`` — igni's own reply, which is a transcript and never
    #: an instruction.
    origin: str = "web"
    quote: str = ""
    #: Where the passage sat when the comment was written, or -1.
    #:
    #: The only way to tell two identical sentences apart. Without it
    #: a comment on the second "Limits" heading in a document
    #: highlights the first one and files itself beside it.
    quote_start: int = -1
    body: str = ""
    suggested_text: str = ""
    status: str = "open"
    resolved_in_n: int = 0
    author: str = ""
    at: str = ""
    #: True when the comment quotes a version that is no longer the
    #: head. The passage may have been rewritten since, so the
    #: reviewer is told rather than left to notice.
    behind_head: bool = False
    #: Where the document is now.
    #:
    #: Sent because accepting a suggestion appends after *head*, not
    #: after the version commented on, and the button that does it has
    #: to be able to say which version it will write. Without this the
    #: screen could only guess from ``n`` — and on a comment left on
    #: v2 of a document since revised to v5, it promised v3 and wrote
    #: v6.
    head_n: int = 0


class ArtifactTreeNode(BaseModel):
    """One entry in the navigation tree."""

    node_id: str = ""
    name: str = ""
    kind: str = "note"
    is_folder: bool = False
    head_n: int = 0
    depth: int = 0
    #: How many things sit below this one, at any depth.
    descendants: int = 0
    #: Stands in for a parent that is missing or cannot be opened.
    placeholder: bool = False
    #: Put away. Only ever present when the reader asked to see them.
    archived: bool = False


class ArtifactTree(BaseModel):
    """The tree, already flattened and ordered.

    Flattened on this side because the order shown and the order
    reasoned about should be the same one, and depth is what the UI
    indents by.
    """

    nodes: list[ArtifactTreeNode] = Field(default_factory=list)
    error: str = ""


class ArtifactDependent(BaseModel):
    """One document standing in the way of a delete."""

    artifact_id: str = ""
    name: str = ""
    #: ``confirmed`` — somebody agreed this was a source.
    #: ``candidate`` — the agent noticed a read and nobody has ruled.
    state: str = ""
    #: ``dependent`` — that document was built on this one.
    #: ``source`` — this one was built on that document.
    #:
    #: Which way the edge runs is what tells a reader where to go to
    #: change it, so it travels with the name rather than being
    #: flattened into "related".
    direction: str = ""


class ArtifactRevised(BaseModel):
    """The outcome of editing a document by hand."""

    ok: bool = False
    #: The version that was written. The caller shows it, and it is
    #: how somebody knows their edit became a thing rather than
    #: overwriting what was there.
    n: int = 0
    message: str = ""
    error: str = ""


class ArtifactTreeChanged(BaseModel):
    """The outcome of creating, moving, renaming or removing."""

    ok: bool = False
    node_id: str = ""
    #: Set when a folder was removed and its contents moved up.
    promoted: int = 0
    message: str = ""
    error: str = ""
    #: Why a delete was refused, named rather than counted.
    #:
    #: "3 documents depend on this" tells a reader they are stuck.
    #: Naming them tells them what to go and change, which is the
    #: only version of this message that leads anywhere.
    blocked_by: list[ArtifactDependent] = Field(default_factory=list)
    #: True when archiving would do what the reader was after.
    suggest_archive: bool = False


class ArtifactCommentReplied(BaseModel):
    """The outcome of answering a comment.

    Two replies can come back from one send. The person's reply is
    always posted; the agent's is posted only when the message
    addressed ``@igni``, and only as a *suggestion* — the thread is
    shared, other people are watching it, and an agent that appended a
    version because somebody typed its name would be writing on their
    behalf in public.
    """

    ok: bool = False
    comment_id: str = ""
    #: Who the message reached, for the UI to confirm.
    mentioned: list[str] = Field(default_factory=list)
    #: Handles that matched nobody in the thread.
    unknown: list[str] = Field(default_factory=list)
    #: True when ``@igni`` was addressed and the agent answered.
    agent_replied: bool = False
    agent_body: str = ""
    #: Replacement text the agent proposed, if any. Applying it is
    #: still the existing Accept button's job.
    agent_suggestion: str = ""
    message: str = ""
    error: str = ""


class ArtifactCommentAnchor(BaseModel):
    """What became of the passage a comment was left on.

    Deterministic and opinion-free. The review queue's "the document
    has moved on since" is true of every row and separates none of
    them — a passage still present word for word and a passage deleted
    outright read identically. This says which, and carries what the
    passage says now so the change can be read rather than reported.
    """

    #: ``intact`` | ``edited`` | ``gone`` | ``no-anchor``.
    state: str = "no-anchor"
    #: The quote as the comment recorded it.
    then: str = ""
    #: What replaced it, when something did.
    now: str = ""
    #: How alike the two are, 0-1.
    similarity: float = 0.0
    commented_n: int = 0
    head_n: int = 0
    #: The passage in its paragraph at head, so the reviewer can see
    #: whether the fix landed in the sentence after.
    context: str = ""
    error: str = ""


class ArtifactCommentRelevance(BaseModel):
    """Whether the comment survived what happened to its passage.

    Asked *after* the anchor is tracked and never instead of it. A
    changed anchor is not an answered comment and an intact one is not
    an unanswered comment, which is the whole reason a string
    comparison cannot finish this job.
    """

    #: ``still-applies`` | ``overtaken`` | ``unclear``.
    relevance: str = "unclear"
    reason: str = ""
    error: str = ""


class ArtifactCommentResolved(BaseModel):
    """The outcome of adjudicating one comment."""

    ok: bool = False
    status: str = ""
    #: The version an accepted suggestion was applied in, if any.
    resolved_in_n: int = 0
    message: str = ""


class MentionCandidate(BaseModel):
    """One person a comment could name."""

    user_id: str = ""
    email: str = ""
    name: str = ""
    #: Whether they could open the document as it is shared today.
    has_access: bool = False
    #: Somebody has already asked for them to be let in. A different
    #: offer from ``has_access``: there is nothing left to press.
    pending: bool = False


class ArtifactMentions(BaseModel):
    """Who can be named on a document, and who can fix it if they cannot.

    ``error`` rather than a raised exception for the same reason every
    other result here carries one: this runs while somebody is typing,
    and a completion popup that cannot reach the server should go
    quiet, not interrupt the sentence being written.
    """

    people: list[MentionCandidate] = Field(default_factory=list)
    #: Whether the person asking may widen the audience themselves.
    can_grant: bool = False
    #: Who to ask when they may not.
    owner_name: str = ""
    owner_email: str = ""
    error: str = ""


class ArtifactViewGranted(BaseModel):
    """The outcome of letting one more person read a document."""

    ok: bool = False
    #: Who was let in, echoed back — the popup that asked has usually
    #: closed by the time this returns, so the message has to name them
    #: rather than rely on what is still on screen.
    identity: str = ""
    message: str = ""
    error: str = ""


class RosterPerson(BaseModel):
    """One person a document names, and what they may do."""

    identity: str = ""
    user_id: str = ""
    email: str = ""
    name: str = ""
    #: ``owner``, ``writer`` or ``reader``.
    role: str = "reader"


class ArtifactLocked(BaseModel):
    """What a document is, to somebody who cannot open it.

    ``open`` true means they can read it after all, in which case
    nothing else is filled in — the caller should be showing the
    document rather than the door.
    """

    open: bool = False
    title: str = ""
    owner_name: str = ""
    owner_email: str = ""
    asked: bool = False
    declined: bool = False
    error: str = ""


class ArtifactOpened(BaseModel):
    """Where a shared document ended up after following a link.

    Three outcomes, and the caller draws a different thing for each:
    it opened and is now local (``artifact_id``), it is shut and can
    be asked for (``locked``), or something went wrong (``error``).
    """

    ok: bool = False
    #: The local id, once the chain has been pulled in.
    artifact_id: str = ""
    title: str = ""
    #: Filled instead of ``artifact_id`` when the reader may not open
    #: it. The caller draws the door.
    locked: ArtifactLocked | None = None
    error: str = ""


class AccessAsk(BaseModel):
    """One person waiting to be let in."""

    id: str = ""
    identity: str = ""
    name: str = ""
    #: Who raised it, when somebody else did.
    asked_by_name: str = ""
    note: str = ""
    created_at: str = ""


class ArtifactAccessAsks(BaseModel):
    """The queue waiting on one document."""

    asks: list[AccessAsk] = Field(default_factory=list)
    error: str = ""


class ArtifactAccessAsked(BaseModel):
    """The outcome of asking for access."""

    ok: bool = False
    #: Whom it was for, echoed — the popup that asked has usually
    #: closed by the time this returns.
    identity: str = ""
    message: str = ""
    error: str = ""


class ArtifactPeople(BaseModel):
    """Who a document is shared with, and how far it reaches.

    Empty is the ordinary answer for a document that was never
    shared, which is why ``error`` is separate from "no people": one
    means nothing to draw, the other means something went wrong, and
    a panel that treated them alike would put a warning under every
    private draft.
    """

    people: list[RosterPerson] = Field(default_factory=list)
    reach: str = "private"
    domain: str = ""
    can_manage: bool = False
    #: The link to hand somebody, when the document has been shared.
    url: str = ""
    error: str = ""


class ArtifactRoom(BaseModel):
    """Whether this session is now watching a document's room.

    ``shared`` is the interesting field. A document that was never
    published has no room and nobody else can be in it, which is not
    a failure — it is what a private draft looks like — so the panel
    is told the difference rather than shown a warning under every
    document somebody has not shared yet.
    """

    ok: bool = False
    shared: bool = False
    error: str = ""


class ArtifactExportResult(BaseModel):
    """Where a local export landed, and as what."""

    path: str = ""
    #: ``html``, ``md`` or ``pdf``. Echoed back rather than assumed by
    #: the caller: the request names a format, and what actually got
    #: written is the answer to a different question.
    format: str = ""
    error: str = ""


class ArtifactOpenResult(BaseModel):
    """Whether the OS took the file off our hands."""

    opened: bool = False
    error: str = ""


class ArtifactShareResult(BaseModel):
    """The URL of a published snapshot, and which version it holds."""

    url: str = ""
    version: int = 0
    error: str = ""


class ArtifactRemoveResult(BaseModel):
    """Delete outcome — ``removed`` is False for both "not found" and
    "disabled" (``error`` differentiates)."""

    removed: bool
    error: str = ""


class ArtifactGraphNode(BaseModel):
    """One node in the picture drawn beside a result."""

    id: str
    label: str
    kind: str = "artifact"
    version: int | None = None


class ArtifactGraphEdge(BaseModel):
    source: str
    target: str
    label: str = ""
    stale: bool = False


class ArtifactGraphView(BaseModel):
    nodes: list[ArtifactGraphNode] = Field(default_factory=list)
    edges: list[ArtifactGraphEdge] = Field(default_factory=list)


class ArtifactQueryResult(BaseModel):
    """A question, the Cypher written for it, and what came back.

    ``cypher`` is returned even when the run failed — the panel puts
    it in an editable box, so a query that was nearly right is one
    correction away rather than a dead end.
    """

    question: str = ""
    cypher: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    graph: ArtifactGraphView = Field(default_factory=ArtifactGraphView)
    truncated: bool = False
    attempts: int = 0
    unanswerable: bool = False
    error: str = ""
