"""Eval setup for the archivist suite.

Seeds a small store with a situation that has something wrong in it,
then points the agent's session at it.

The scenario is chosen so several cases can be graded against the same
state, and so each case has a *wrong* answer that looks plausible:

* **Auth model** is at v3. It was a session cookie at v1 and is a
  signed JWT now — a change of substance, not wording.
* **Login flow** is pinned to ``Auth model@1``. It is stale, and its
  text still describes cookies, so reconciling it genuinely requires
  an edit.
* **Release checklist** is pinned to ``Auth model@2``. It is stale
  too, but only mentions *that* auth exists — so the right answer is
  to re-pin, not to invent an edit. An agent that edits both is doing
  the thing the reconcile flow exists to prevent.
* **Rate limiting notes** sits on Login flow as an unconfirmed
  *candidate* — captured because it was read, never adjudicated.
* **Error taxonomy** is a confirmed source of Login flow that is
  up to date, so "confirm everything" and "re-pin everything" both
  produce visible damage.

The driver is stubbed at ``ArtifactCypherService._execute``, the one
method that touches Neo4j. Everything above it runs for real — the
read-only guard, parameter binding, row capping, the graph projection
— so a case can grade the Cypher the agent actually wrote.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec
from ember_code.core.artifacts.models import EdgeState, SourceDeclaration, VersionPin
from evals.archivist.store import EvalEmbedder, FakeArtifactStore

logger = logging.getLogger(__name__)

CODEC = ArtifactMetadataCodec()

AUTH_V1 = """Auth is a session cookie, set on sign-in and read on every
request. It expires after 24 hours of inactivity.

Revocation is a denylist check against the sessions table."""

AUTH_V2 = """Auth is a session cookie, set on sign-in and read on every
request. It expires after 8 hours of inactivity.

Revocation is a denylist check against the sessions table."""

AUTH_V3 = """Auth is a signed JWT, not a session cookie. It expires in
15 minutes and is refreshed by an httpOnly cookie with SameSite=Lax.

Revocation is checked on refresh only — the access token is not
consulted against a denylist, because that read was what fell over
first under load."""

LOGIN_FLOW = """The login flow posts credentials, then stores the
session cookie the auth model issues. The cookie is good for 24 hours,
so a returning user inside that window skips the form entirely.

On failure the flow surfaces the error taxonomy's `auth.rejected`."""

CHECKLIST = """Before cutting a tag:

- confirm migrations have run on staging
- confirm auth is reachable from the edge nodes
- confirm the error taxonomy has no unmapped codes"""

TAXONOMY = """Every user-visible failure maps to a code. `auth.rejected`
covers a credential that was read and refused."""

RATE_LIMITS = """Rate limiting is per-IP at the edge and per-account at
the API. Neither is aware of the other."""


def build_index(work_dir: Path) -> ArtifactIndex:
    """An unseeded index over a fresh fake store.

    Split out so the live driver can build one before the runner's
    hook fires — it needs a manager to construct the agent with, and
    the agent is built before any case runs.
    """
    store = FakeArtifactStore()
    index = ArtifactIndex(
        project=work_dir,
        neo4j_client=store,  # type: ignore[arg-type]
        embedder=EvalEmbedder(),
    )
    _patch_cypher_driver(store)
    return index


async def build_seeded_index(work_dir: Path) -> ArtifactIndex:
    """A seeded index, for callers that build the agent themselves."""
    index = build_index(work_dir)
    await _seed(index)
    return index


async def setup(work_dir: Path, project_dir: Path) -> None:
    """Build the seeded store and hand it to every session built here."""
    index = build_index(work_dir)
    await _seed(index)
    _patch_session(index)
    logger.info("archivist eval: seeded %d artifacts", await index.count())


async def _seed(index: ArtifactIndex) -> None:
    """Write the scenario through the real index.

    Seeded via the public API rather than by building nodes by hand,
    so the fixture cannot encode a state the product could not
    actually produce.

    The ledger is cleared between writes. Capture is doing its job
    here — each seeding write is a "read" of everything seeded before
    it — but a fixture meant to contain *one* unadjudicated candidate
    arrived with ten, and the case that asks the agent to review them
    would have been grading it on the fixture's own noise.
    """
    none = SourceDeclaration()

    await index.create(name="Auth model", content=AUTH_V1, sources=none, kind="design")
    index.ledger.clear()
    await index.create(name="Error taxonomy", content=TAXONOMY, sources=none, kind="design")
    index.ledger.clear()
    await index.create(name="Rate limiting notes", content=RATE_LIMITS, sources=none)
    index.ledger.clear()

    auth = CODEC.artifact_id("Auth model")
    taxonomy = CODEC.artifact_id("Error taxonomy")
    rates = CODEC.artifact_id("Rate limiting notes")

    # Login flow: two confirmed sources, pinned where they were read.
    await index.create(
        name="Login flow",
        content=LOGIN_FLOW,
        kind="note",
        sources=SourceDeclaration(
            pins=[
                VersionPin(artifact_id=auth, n=1, state=EdgeState.CONFIRMED),
                VersionPin(artifact_id=taxonomy, n=1, state=EdgeState.CONFIRMED),
            ]
        ),
    )
    index.ledger.clear()
    login = CODEC.artifact_id("Login flow")

    # Auth model moves twice. Login flow's pin stays at v1.
    await index.revise(ref=auth, content=AUTH_V2, sources=none)
    index.ledger.clear()

    # Release checklist reads auth at v2 — mentions it, does not
    # describe it. The right reconcile here is a re-pin.
    await index.create(
        name="Release checklist",
        content=CHECKLIST,
        sources=SourceDeclaration(
            pins=[
                VersionPin(artifact_id=auth, n=2, state=EdgeState.CONFIRMED),
                VersionPin(artifact_id=taxonomy, n=1, state=EdgeState.CONFIRMED),
            ]
        ),
    )

    index.ledger.clear()
    await index.revise(ref=auth, content=AUTH_V3, sources=none)
    index.ledger.clear()

    # One unadjudicated capture on Login flow, as retrieval would
    # have left it.
    head = await index.version(login, 0)
    if head is not None:
        await index.client.record_candidates(
            version_id=head.version_id,
            pins=[VersionPin(artifact_id=rates, n=1)],
        )

    # Each case starts from what the *agent* has read, not from what
    # seeding touched.
    index.ledger.clear()


def _patch_session(index: ArtifactIndex) -> None:
    """Give every session built in this process the seeded index.

    The runner constructs the Session, so there is no ``pre_artifacts``
    to pass. Patching the one symbol ``core.py`` imports is the
    narrowest interception available — the same move
    ``codeindex_architect_cypher`` makes on ``CodeIndex._neo4j_client``.
    """
    import ember_code.core.session.core as core_mod
    from ember_code.core.session.artifact_ops import SessionArtifactManager

    real = SessionArtifactManager

    def seeded(artifacts, settings, project_dir, **kwargs):
        return real(index, settings, project_dir, **kwargs)

    core_mod.SessionArtifactManager = seeded  # type: ignore[assignment]


def _patch_cypher_driver(store: FakeArtifactStore) -> None:
    """Answer Cypher without a database, below the guard.

    ``_execute`` is the only method that touches the driver, so
    everything the eval cares about grading — the read-only guard,
    ``$proj`` binding, the row cap, the graph projection — still runs
    on the query the agent actually wrote.

    Responses are keyed on intent, like the codeindex suite's
    dispatcher. A query that matches nothing returns no rows, which is
    the honest answer: the agent asked something this fixture does not
    answer.
    """
    from ember_code.core.artifacts.cypher import ArtifactCypherService

    async def _execute(self, cypher: str, params: dict):  # noqa: ANN202
        rows = await _dispatch(store, cypher)
        columns = list(rows[0].keys()) if rows else []
        return rows, columns

    ArtifactCypherService._execute = _execute  # type: ignore[assignment]


async def _dispatch(store: FakeArtifactStore, cypher: str) -> list[dict]:
    lowered = cypher.lower()
    if "derived_from" in lowered and ("pinned_n <" in lowered or "head_n" in lowered):
        return [
            {
                "dependent": row.dependent_name,
                "source": row.source_name,
                "pinned_to": row.pinned_n,
                "now_at": row.source_head_n,
                "artifact_id": row.dependent_artifact_id,
            }
            for row in await store.stale_dependents()
        ]
    if "candidate" in lowered:
        out = []
        for summary in await store.list_artifacts():
            lineage = await store.lineage(summary.artifact_id, 0)
            out.extend(
                {
                    "document": summary.name,
                    "possible_source": edge.to_name,
                    "artifact_id": summary.artifact_id,
                }
                for edge in lineage.sources
                if edge.state is EdgeState.CANDIDATE
            )
        return out
    if "count(" in lowered:
        return [{"total": await store.version_count()}]
    if ":artifact" in lowered:
        return [
            {
                "name": s.name,
                "kind": s.kind,
                "version": s.head_n,
                "artifact_id": s.artifact_id,
            }
            for s in await store.list_artifacts()
        ]
    return []
