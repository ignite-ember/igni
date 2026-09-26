"""The agent-facing artifact toolkit.

The behaviour under the most scrutiny here is the one the subsystem
exists for: a write **cannot** be made without declaring its sources,
and the refusal has to be answerable — it names the refs the session
actually read, so the agent confirms a real corpus instead of
inventing a plausible one.
"""

from __future__ import annotations

from ember_code.core.artifacts.cypher import CypherResult
from ember_code.core.artifacts.errors import SourcesRequired
from ember_code.core.artifacts.models import (
    ArtifactLineage,
    ArtifactSearchHit,
    ArtifactSearchResponse,
    ArtifactStatus,
    ArtifactSummary,
    ArtifactVersion,
    ArtifactWriteResult,
    Citation,
    EdgeState,
    LineageEdge,
    ReconcilePlan,
    StaleDependent,
)
from ember_code.core.tools.artifacts import ArtifactTools


class FakeManager:
    """Stands in for :class:`SessionArtifactManager`.

    Records calls so a test can assert what the toolkit forwarded,
    which is the toolkit's entire job.
    """

    def __init__(self, **overrides):
        self.calls: list[tuple] = []
        self.pending: list[str] = overrides.get("pending", [])
        self.require_sources = overrides.get("require_sources", True)
        self.search_response = overrides.get("search_response", ArtifactSearchResponse(query=""))
        self.artifacts_list = overrides.get("artifacts_list", [])
        self.version_result = overrides.get("version_result")
        self.versions_result = overrides.get("versions_result", [])
        self.lineage_result = overrides.get("lineage_result")
        self.stale_result = overrides.get("stale_result", [])
        self.plan_result = overrides.get("plan_result")
        self.status_result = overrides.get("status_result", ArtifactStatus(enabled=True))
        self.write_result = overrides.get(
            "write_result", ArtifactWriteResult.ok("stored", artifact_id="a1", n=1)
        )
        self.graph_answer = overrides.get("graph_answer", "2 row(s):")
        self.cypher_result = overrides.get("cypher_result")
        self.confirm_result = overrides.get("confirm_result", True)
        self.delete_result = overrides.get("delete_result", True)

    def pending_sources(self):
        return self.pending

    async def ask_graph(self, question):
        self.calls.append(("ask_graph", question))
        return self.graph_answer

    def _maybe_require(self, sources, no_sources_reason):
        declared = [s for s in (sources or []) if s.strip()]
        if self.require_sources and not declared and not no_sources_reason.strip():
            raise SourcesRequired("needs sources", candidates=self.pending)

    async def create(
        self, *, name, content, sources, kind="note", no_sources_reason="", metadata=None
    ):
        self._maybe_require(sources, no_sources_reason)
        self.calls.append(("create", name, content, tuple(sources or ()), kind))
        return self.write_result

    async def revise(self, *, ref, content, sources, no_sources_reason="", metadata=None):
        self._maybe_require(sources, no_sources_reason)
        self.calls.append(("revise", ref, content, tuple(sources or ())))
        return self.write_result

    async def search(self, query, limit=5, *, record=True):
        self.calls.append(("search", query, limit, record))
        return self.search_response

    async def list_artifacts(self, *, limit=1000):
        return self.artifacts_list

    async def version(self, ref, n=0, *, record=False):
        self.calls.append(("version", ref, n, record))
        return self.version_result

    async def run_cypher(self, cypher, *, semantic_query=""):
        self.calls.append(("run_cypher", cypher, semantic_query))
        return self.cypher_result

    async def versions(self, ref):
        return self.versions_result

    async def lineage(self, ref, n=0):
        return self.lineage_result

    async def stale(self, *, limit=100):
        return self.stale_result

    async def reconcile_plan(self, stale):
        self.calls.append(("plan", stale.dependent_artifact_id))
        return self.plan_result

    async def accept_reconcile(self, *, plan, content=None):
        self.calls.append(("accept", plan.dependent_artifact_id, content))
        return self.write_result

    async def confirm_source(self, *, ref, source_ref):
        self.calls.append(("confirm", ref, source_ref))
        return self.confirm_result

    async def dismiss_source(self, *, ref, source_ref):
        self.calls.append(("dismiss", ref, source_ref))
        return self.confirm_result

    async def delete(self, ref):
        self.calls.append(("delete", ref))
        return self.delete_result

    async def status(self):
        return self.status_result


def tools(**overrides) -> tuple[ArtifactTools, FakeManager]:
    mgr = FakeManager(**overrides)
    return ArtifactTools(mgr), mgr


class TestArtifactCreateDemandsSources:
    async def test_a_missing_declaration_is_refused(self):
        kit, _ = tools()
        out = await kit.artifact_create("Design", "body", [])
        assert "needs a source declaration" in out
        assert "Error" in out

    async def test_the_refusal_names_what_was_read(self):
        """A refusal without the candidate list produces a guess."""
        kit, _ = tools(pending=["auth@2", "schema@1"])
        out = await kit.artifact_create("Design", "body", [])
        assert "auth@2" in out
        assert "schema@1" in out

    async def test_with_nothing_read_it_says_empty_is_probably_right(self):
        kit, _ = tools(pending=[])
        out = await kit.artifact_create("Design", "body", [])
        assert "no_sources_reason" in out
        assert "likely correct" in out

    async def test_an_explicit_reason_lets_an_empty_list_through(self):
        kit, mgr = tools()
        out = await kit.artifact_create("Design", "body", [], no_sources_reason="first observation")
        assert "stored" in out
        assert mgr.calls[0][0] == "create"

    async def test_declared_sources_reach_the_manager(self):
        kit, mgr = tools()
        await kit.artifact_create("Design", "body", ["auth@2", "https://x.test/spec"])
        assert mgr.calls[0][3] == ("auth@2", "https://x.test/spec")

    async def test_a_store_failure_is_reported_not_raised(self):
        kit, _ = tools(write_result=ArtifactWriteResult.fail("name taken"))
        out = await kit.artifact_create("Design", "body", ["a@1"])
        assert "Error: name taken" in out


class TestArtifactRevise:
    async def test_it_also_demands_sources(self):
        kit, _ = tools(pending=["auth@2"])
        out = await kit.artifact_revise("Design", "new body", [])
        assert "source declaration" in out
        assert "auth@2" in out

    async def test_a_revision_forwards_its_own_sources(self):
        kit, mgr = tools()
        await kit.artifact_revise("Design", "new body", ["auth@3"])
        assert mgr.calls[0] == ("revise", "Design", "new body", ("auth@3",))


class TestReads:
    async def test_search_reports_nothing_found_with_the_query(self):
        kit, _ = tools(search_response=ArtifactSearchResponse(query="pricing"))
        assert "pricing" in await kit.artifact_search("pricing")

    async def test_search_hits_carry_their_ref(self):
        response = ArtifactSearchResponse(
            query="q",
            results=[ArtifactSearchHit(artifact_id="a1", name="Auth", n=3, content="body")],
            total=1,
        )
        kit, _ = tools(search_response=response)
        out = await kit.artifact_search("q")
        assert "Auth" in out and "a1@3" in out

    async def test_artifact_read_returns_the_full_version(self):
        kit, _ = tools(
            version_result=ArtifactVersion(
                artifact_id="a1", version_id="a1::2", n=2, content="full text", head=True
            )
        )
        out = await kit.artifact_read("a1")
        assert "full text" in out and "v2" in out

    async def test_artifact_read_of_a_missing_artifact_says_so(self):
        kit, _ = tools(version_result=None)
        assert "No artifact matches" in await kit.artifact_read("nope")

    async def test_artifact_list_shows_version_counts(self):
        kit, _ = tools(
            artifacts_list=[
                ArtifactSummary(artifact_id="a1", name="Auth", head_n=3, version_count=3)
            ]
        )
        out = await kit.artifact_list()
        assert "Auth" in out and "v3" in out and "3 version(s)" in out

    async def test_artifact_list_on_an_empty_store(self):
        kit, _ = tools(artifacts_list=[])
        assert await kit.artifact_list() == "No artifacts yet."

    async def test_artifact_versions_marks_the_current_one(self):
        kit, _ = tools(
            versions_result=[
                ArtifactVersion(artifact_id="a1", n=2, content="new", head=True),
                ArtifactVersion(artifact_id="a1", n=1, content="old"),
            ]
        )
        out = await kit.artifact_versions("a1")
        assert "v2 (current)" in out and "v1" in out

    async def test_artifact_ask_forwards_the_question(self):
        """The structural counterpart to artifact_search: search finds
        documents *about* a topic, ask answers questions *about the
        documents*."""
        kit, mgr = tools(graph_answer="1 row(s):\n  dependent=Login")
        out = await kit.artifact_ask("what depends on the auth model")

        assert ("ask_graph", "what depends on the auth model") in mgr.calls
        assert "Login" in out

    async def test_artifact_pending_sources_lists_the_session_reads(self):
        kit, _ = tools(pending=["auth@2"])
        assert "auth@2" in await kit.artifact_pending_sources()

    async def test_artifact_pending_sources_when_nothing_was_read(self):
        kit, _ = tools(pending=[])
        assert "not read any artifacts" in await kit.artifact_pending_sources()


class TestLineage:
    def _lineage(self, **kw) -> ArtifactLineage:
        return ArtifactLineage(artifact_id="a1", n=2, **kw)

    async def test_it_separates_confirmed_from_captured(self):
        lineage = self._lineage(
            sources=[
                LineageEdge(
                    from_artifact_id="a1",
                    from_n=2,
                    to_artifact_id="s1",
                    to_name="Schema",
                    to_n=1,
                    state=EdgeState.CONFIRMED,
                    pinned_n=1,
                    source_head_n=1,
                ),
                LineageEdge(
                    from_artifact_id="a1",
                    from_n=2,
                    to_artifact_id="s2",
                    to_name="Notes",
                    to_n=1,
                    state=EdgeState.CANDIDATE,
                    pinned_n=1,
                    source_head_n=1,
                ),
            ]
        )
        kit, _ = tools(lineage_result=lineage)
        out = await kit.artifact_lineage("a1")
        assert "Built on:" in out and "Schema" in out
        assert "Captured but unconfirmed" in out and "Notes" in out

    async def test_a_moved_source_is_flagged_inline(self):
        lineage = self._lineage(
            sources=[
                LineageEdge(
                    from_artifact_id="a1",
                    from_n=2,
                    to_artifact_id="s1",
                    to_name="Schema",
                    to_n=1,
                    state=EdgeState.CONFIRMED,
                    pinned_n=1,
                    source_head_n=4,
                )
            ]
        )
        kit, _ = tools(lineage_result=lineage)
        out = await kit.artifact_lineage("a1")
        assert "moved to v4" in out

    async def test_nothing_declared_says_so_rather_than_printing_nothing(self):
        kit, _ = tools(lineage_result=self._lineage())
        assert "(nothing declared)" in await kit.artifact_lineage("a1")

    async def test_citations_are_listed(self):
        kit, _ = tools(
            lineage_result=self._lineage(citations=[Citation(url="https://x.test/spec")])
        )
        assert "https://x.test/spec" in await kit.artifact_lineage("a1")

    async def test_confirm_and_dismiss_forward_both_refs(self):
        kit, mgr = tools()
        await kit.artifact_confirm_source("a1", "s1")
        await kit.artifact_dismiss_source("a1", "s2")
        assert ("confirm", "a1", "s1") in mgr.calls
        assert ("dismiss", "a1", "s2") in mgr.calls

    async def test_confirming_a_link_that_was_not_captured_says_so(self):
        kit, _ = tools(confirm_result=False)
        assert "No captured link" in await kit.artifact_confirm_source("a1", "s1")


class TestStaleAndReconcile:
    def _stale(self) -> StaleDependent:
        return StaleDependent(
            dependent_artifact_id="a1",
            dependent_name="Design",
            dependent_n=2,
            source_artifact_id="s1",
            source_name="Schema",
            pinned_n=1,
            source_head_n=4,
        )

    async def test_a_clean_store_reports_nothing_out_of_date(self):
        kit, _ = tools(stale_result=[])
        assert "Nothing is out of date" in await kit.artifact_stale()

    async def test_stale_rows_say_how_far_behind_they_are(self):
        kit, _ = tools(stale_result=[self._stale()])
        out = await kit.artifact_stale()
        assert "Design" in out and "3 version(s) behind" in out

    async def test_reconcile_without_content_returns_the_diff(self):
        plan = ReconcilePlan(
            dependent_artifact_id="a1",
            dependent_name="Design",
            dependent_n=2,
            dependent_content="current body",
            source_artifact_id="s1",
            source_name="Schema",
            pinned_n=1,
            source_head_n=4,
            source_diff="-old\n+new",
        )
        kit, mgr = tools(stale_result=[self._stale()], plan_result=plan)
        out = await kit.artifact_reconcile("Design")
        assert "-old" in out and "+new" in out
        assert "current body" in out
        assert not any(c[0] == "accept" for c in mgr.calls)

    async def test_reconcile_with_content_stores_a_revision(self):
        plan = ReconcilePlan(
            dependent_artifact_id="a1",
            dependent_name="Design",
            dependent_n=2,
            source_artifact_id="s1",
            source_name="Schema",
            pinned_n=1,
            source_head_n=4,
        )
        kit, mgr = tools(stale_result=[self._stale()], plan_result=plan)
        await kit.artifact_reconcile("Design", updated_content="fixed body")
        assert ("accept", "a1", "fixed body") in mgr.calls

    async def test_repin_accepts_without_changing_content(self):
        plan = ReconcilePlan(
            dependent_artifact_id="a1",
            dependent_name="Design",
            dependent_n=2,
            source_artifact_id="s1",
            source_name="Schema",
            pinned_n=1,
            source_head_n=4,
        )
        kit, mgr = tools(stale_result=[self._stale()], plan_result=plan)
        await kit.artifact_repin("Design")
        assert ("accept", "a1", None) in mgr.calls

    async def test_reconciling_something_that_is_not_stale_says_so(self):
        kit, _ = tools(stale_result=[])
        assert "no out-of-date sources" in await kit.artifact_reconcile("Design")

    async def test_a_ref_is_matched_by_id_or_name(self):
        plan = ReconcilePlan(
            dependent_artifact_id="a1",
            dependent_name="Design",
            dependent_n=2,
            source_artifact_id="s1",
            source_name="Schema",
            pinned_n=1,
            source_head_n=4,
            source_diff="d",
        )
        kit, mgr = tools(stale_result=[self._stale()], plan_result=plan)
        await kit.artifact_reconcile("a1")
        assert ("plan", "a1") in mgr.calls


class TestAdmin:
    async def test_delete_previews_before_it_deletes(self):
        kit, mgr = tools()
        out = await kit.artifact_delete("a1")
        assert "Preview mode" in out
        assert not any(c[0] == "delete" for c in mgr.calls)

    async def test_a_confirmed_delete_goes_through(self):
        kit, mgr = tools()
        out = await kit.artifact_delete("a1", confirm=True)
        assert "Deleted" in out
        assert ("delete", "a1") in mgr.calls

    async def test_deleting_something_missing_says_so(self):
        kit, _ = tools(delete_result=False)
        assert "No artifact matches" in await kit.artifact_delete("nope", confirm=True)

    async def test_status_on_a_disabled_store(self):
        kit, _ = tools(status_result=ArtifactStatus(enabled=False))
        assert await kit.artifact_status() == "Artifacts are disabled."

    async def test_status_surfaces_stale_and_candidate_counts(self):
        kit, _ = tools(
            status_result=ArtifactStatus(
                enabled=True,
                document_count=4,
                version_count=11,
                stale_count=2,
                pending_candidates=5,
                embedder="hash",
            )
        )
        out = await kit.artifact_status()
        assert "Artifacts: 4" in out
        assert "Versions: 11" in out
        assert "Out of date: 2" in out
        assert "Unconfirmed captured sources: 5" in out


class TestOneToolkitOneAgent:
    """The whole toolkit belongs to the archivist — reads included.

    Two reasons, and the second decided it. Provenance: reading and
    writing share one model, and an agent that can follow lineage but
    not act on it holds half of it. Attribution: a capability spread
    across every agent makes every agent's manifest claim it, and the
    corpus already carries 1,370 specialist traces whose manifests had
    to be rebuilt over exactly that.
    """

    def test_the_toolkit_carries_reads_and_writes_together(self):
        names = set(ArtifactTools(FakeManager()).async_functions)
        assert {"artifact_search", "artifact_read"} <= names
        assert {"artifact_create", "artifact_revise", "artifact_delete"} <= names

    def test_it_takes_no_capability_flag(self):
        """A flag with one value is a flag nobody can use and somebody
        will eventually set wrong."""
        import inspect

        params = inspect.signature(ArtifactTools.__init__).parameters
        assert set(params) == {"self", "artifact_mgr"}

    def test_the_archivist_is_named_in_code(self):
        """Not read from frontmatter: an agent that could grant itself
        the capability is the same as having no rule."""
        from ember_code.core.tools.artifacts import ARCHIVIST

        assert ARCHIVIST == "archivist"


class TestArtifactCypher:
    def _kit(self, result):
        mgr = FakeManager(cypher_result=result)
        return ArtifactTools(mgr), mgr

    async def test_it_forwards_the_query_verbatim(self):
        kit, mgr = self._kit(
            CypherResult(
                cypher="MATCH (a:Artifact) RETURN a.name AS name LIMIT 5",
                rows=[{"name": "Auth model"}],
            )
        )
        out = await kit.artifact_cypher("MATCH (a:Artifact) RETURN a.name AS name LIMIT 5")

        assert ("run_cypher", "MATCH (a:Artifact) RETURN a.name AS name LIMIT 5", "") in mgr.calls
        assert "Auth model" in out

    async def test_a_refused_query_says_why(self):
        kit, _ = self._kit(
            CypherResult(error="Cypher token 'DELETE' is forbidden: this tool is read-only.")
        )
        out = await kit.artifact_cypher("MATCH (a) DELETE a")
        assert "read-only" in out

    async def test_no_rows_is_distinguished_from_a_failure(self):
        """ "Nothing matched" and "the query was wrong" are different
        answers and lead to different next steps."""
        kit, _ = self._kit(CypherResult(cypher="…", rows=[]))
        out = await kit.artifact_cypher("MATCH (a:Artifact) WHERE false RETURN a.name LIMIT 1")
        assert "No rows" in out
        assert "refused" not in out

    async def test_semantic_text_is_passed_through_for_the_vector_index(self):
        kit, mgr = self._kit(CypherResult(cypher="…", rows=[{"name": "x"}]))
        await kit.artifact_cypher("CALL db.index.vector.queryNodes(...)", semantic_query="jwt")
        assert ("run_cypher", "CALL db.index.vector.queryNodes(...)", "jwt") in mgr.calls

    async def test_a_long_result_is_capped_with_the_remainder_named(self):
        rows = [{"name": f"doc-{i}"} for i in range(60)]
        kit, _ = self._kit(CypherResult(cypher="…", rows=rows))
        out = await kit.artifact_cypher("MATCH (a:Artifact) RETURN a.name AS name LIMIT 60")
        assert "and 20 more" in out
