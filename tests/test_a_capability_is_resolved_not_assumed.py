"""``requires`` stops being a field nobody reads.

It has been in the manifest and the schema since they were written
and nothing consumed it — the fourth declared-and-ignored field this
system has had. An extension could ask for a graph database and get
installed on a machine with none.

The design, in one sentence: **a resolver returns a connection,
never a process.** Two resolvers differ only in what they do before
returning — a laptop's provisions one first, a deployment's binds to
one the infrastructure already runs. The extension cannot tell which
it got, which is what lets the same bundle, the same signature and
the same manifest install in both places.
"""

from __future__ import annotations

import pytest

from ember_code.extensions.capabilities import (
    BindingResolver,
    ProvisioningResolver,
    Resolved,
    Tenant,
    UnresolvedRequirement,
    check_dimensions_agree,
    dimension_of,
)
from ember_code.extensions.manifest import Capability, Requirement, ResolveMode


class FakeProvisioner:
    """A provisioner that records rather than downloads Neo4j."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def provision(self, requirement: Requirement, *, extension_id: str) -> Resolved:
        self.calls.append(extension_id)
        return Resolved(
            capability=requirement.capability,
            uri="bolt://127.0.0.1:7687",
            tenant=Tenant.sole(),
        )


def graph(mode: ResolveMode = ResolveMode.SHARED) -> Requirement:
    return Requirement(capability=Capability.GRAPH_DB, constraint=">=5", mode=mode)


DEPLOYMENT_GRAPH = Resolved(
    capability=Capability.GRAPH_DB,
    uri="bolt://graph.internal:7687",
    secret="graph-credentials",
    tenant=Tenant(database="ext_acme"),
)


# ── The rule both resolvers obey ───────────────────────────────────


@pytest.mark.asyncio
async def test_both_resolvers_answer_with_a_connection():
    """The one sentence. An extension cannot tell them apart."""
    local = ProvisioningResolver({Capability.GRAPH_DB: FakeProvisioner()})
    cloud = BindingResolver({Capability.GRAPH_DB: DEPLOYMENT_GRAPH})

    from_local = await local.resolve(graph(), extension_id="acme.one")
    from_cloud = await cloud.resolve(graph(), extension_id="acme.one")

    for answer in (from_local, from_cloud):
        assert answer.capability is Capability.GRAPH_DB
        assert answer.uri.startswith("bolt://")
        assert isinstance(answer.tenant, Tenant)


@pytest.mark.asyncio
async def test_each_says_how_it_was_satisfied():
    """For an administrator reading a log, "provisioned" and "bound"
    are different events."""
    local = ProvisioningResolver({Capability.GRAPH_DB: FakeProvisioner()})
    cloud = BindingResolver({Capability.GRAPH_DB: DEPLOYMENT_GRAPH})

    assert (await local.resolve(graph(), extension_id="a.b")).how == "provisioned"
    assert (await cloud.resolve(graph(), extension_id="a.b")).how == "bound to the deployment's"


@pytest.mark.asyncio
async def test_tenancy_is_present_even_where_there_is_only_one():
    """A field that appeared only in the deployment case would make
    the deployment case a contract change rather than a resolver
    swap."""
    local = ProvisioningResolver({Capability.GRAPH_DB: FakeProvisioner()})
    answer = await local.resolve(graph(), extension_id="a.b")
    assert answer.tenant == Tenant.sole()


@pytest.mark.asyncio
async def test_a_deployment_resolver_carries_the_slice():
    cloud = BindingResolver({Capability.GRAPH_DB: DEPLOYMENT_GRAPH})
    answer = await cloud.resolve(graph(), extension_id="a.b")
    assert answer.tenant.database == "ext_acme"
    # The *name* of a grant, never the credential.
    assert answer.secret == "graph-credentials"


# ── What each refuses ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_deployment_resolver_builds_nothing():
    """A missing database is an operator's problem, and a backend
    that papered over it by starting one would be the surprise."""
    cloud = BindingResolver({})
    with pytest.raises(UnresolvedRequirement, match="does not start any"):
        await cloud.resolve(graph(), extension_id="acme.one")


@pytest.mark.asyncio
async def test_a_deployment_resolver_refuses_dedicated_rather_than_downgrading():
    """The extension asked for isolation. Quietly giving it a shared
    instance is the one answer worse than refusing."""
    cloud = BindingResolver({Capability.GRAPH_DB: DEPLOYMENT_GRAPH})
    with pytest.raises(UnresolvedRequirement, match="dedicated"):
        await cloud.resolve(graph(ResolveMode.DEDICATED), extension_id="acme.one")


@pytest.mark.asyncio
async def test_a_laptop_resolver_refuses_bind_only():
    """`bind-only` is what a customer's change control requires —
    an existing instance and no new one. Provisioning anyway is
    exactly what it forbids."""
    local = ProvisioningResolver({Capability.GRAPH_DB: FakeProvisioner()})
    with pytest.raises(UnresolvedRequirement, match="bind-only"):
        await local.resolve(graph(ResolveMode.BIND_ONLY), extension_id="acme.one")


@pytest.mark.asyncio
async def test_an_unprovisionable_kind_says_what_is_available():
    local = ProvisioningResolver({Capability.GRAPH_DB: FakeProvisioner()})
    embedder = Requirement(capability=Capability.EMBEDDER, constraint="dim=384")
    with pytest.raises(UnresolvedRequirement, match="graph-db"):
        await local.resolve(embedder, extension_id="acme.one")


# ── The embedder's constraint is a width, not a version ────────────


def test_an_embedder_constraint_is_a_dimension():
    """Two extensions sharing an embedder must agree on the width or
    their vectors are not comparable. A newer model of the same width
    is a safe substitution; an older one of a different width is not,
    and a version range cannot say that."""
    assert dimension_of(Requirement(capability=Capability.EMBEDDER, constraint="dim=384")) == 384


def test_a_malformed_embedder_constraint_is_not_guessed_at():
    assert dimension_of(Requirement(capability=Capability.EMBEDDER, constraint=">=2")) is None


def test_a_version_range_on_a_store_is_not_read_as_a_dimension():
    assert dimension_of(graph()) is None


# ── One instance, several kinds — and the rule that spans them ─────


def vectors(width: int) -> Requirement:
    return Requirement(capability=Capability.VECTOR_STORE, constraint=f"dim={width}")


def embedder(width: int) -> Requirement:
    return Requirement(capability=Capability.EMBEDDER, constraint=f"dim={width}")


@pytest.mark.asyncio
async def test_two_kinds_may_resolve_to_one_instance():
    """`graph-db` and `vector-store` are usually the same Neo4j —
    it holds the graph and a native vector index on it, which is how
    both of igni's own capabilities use it.

    That is the resolver's business. Merging them into one kind
    would make an extension that wants vectors and no graph declare
    a graph database it never opens.
    """
    one = Resolved(capability=Capability.GRAPH_DB, uri="bolt://127.0.0.1:7687")
    cloud = BindingResolver(
        {
            Capability.GRAPH_DB: one,
            Capability.VECTOR_STORE: one.model_copy(update={"capability": Capability.VECTOR_STORE}),
        }
    )
    graph_answer = await cloud.resolve(graph(), extension_id="a.b")
    vector_answer = await cloud.resolve(vectors(384), extension_id="a.b")
    assert graph_answer.uri == vector_answer.uri


def test_an_embedder_and_a_vector_store_must_share_a_width():
    """Vectors written at one width and queried at another do not
    fail — they return nonsense, slowly, which is the worst failure
    available."""
    with pytest.raises(UnresolvedRequirement, match="different widths"):
        check_dimensions_agree([embedder(384), vectors(768)], extension_id="acme.one")


def test_agreeing_widths_are_accepted():
    check_dimensions_agree([embedder(384), vectors(384)], extension_id="acme.one")


def test_a_sized_kind_with_a_version_range_is_refused():
    """`>=5` on a vector store says nothing about which vectors are
    comparable."""
    bad = Requirement(capability=Capability.VECTOR_STORE, constraint=">=5")
    with pytest.raises(UnresolvedRequirement, match="needs a width"):
        check_dimensions_agree([bad], extension_id="acme.one")


def test_unsized_kinds_are_not_subject_to_the_rule():
    check_dimensions_agree([graph()], extension_id="acme.one")
