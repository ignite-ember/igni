"""Infrastructure an extension needs, and who makes it exist.

An extension declares a **kind** — a graph database, an embedder —
and never a product. A regulated customer often has a sanctioned
Postgres you must use, and a manifest naming ``postgres`` cannot
express "or whatever they have".

``requires`` has been in the manifest and the schema since they were
written and nothing read it, which is the fourth time that has been
true of a declared field here. This is the thing that reads it.

## The one sentence that matters

**A resolver returns a connection, never a process.**

Everything else follows from it. Two resolvers differ only in what
they do *before* returning:

* :class:`ProvisioningResolver` — on a laptop. Downloads Neo4j,
  spawns the JVM, loads the model, *then* returns a connection.
* :class:`BindingResolver` — in a deployment. Returns a connection
  to something the infrastructure already runs. Builds nothing,
  downloads nothing, and fails if the thing is not there — because
  in a deployment, a missing database is an operator's problem and
  not something a backend should paper over by starting one.

The extension cannot tell which it got, and that is the point: the
same bundle, the same signature and the same manifest install in
both places.

## Tenancy is in the result from the start

A shared instance has to say *which slice* is this extension's — a
database name, a collection prefix. On a laptop that is trivially
"the only one", and it is still filled in, because a field that
appears only in the deployment case makes the deployment case a
contract change instead of a resolver swap.

## What `constraint` means

It is **not** always a version range — that is what it said, and it
was wrong for embedders before embedders existed. Its meaning is
defined by the kind:

| Kind | `constraint` means | Resolved `uri` looks like |
|---|---|---|
| ``graph-db`` | semver range over the major version | ``bolt://host:7687`` |
| ``relational-db`` | semver range over the major version | ``postgresql://host:5432`` |
| ``object-store`` | semver range over the major version | ``s3://`` or a base URL |
| ``vector-store`` | ``dim=<n>`` — the vector width | depends on the product |
| ``embedder`` | ``dim=<n>`` — the vector width | ``http://host/embed`` |

The two vector kinds take a **dimension**, not a version, because
width is the axis on which they are compatible or not. A newer model
of the same width is a safe substitution; an older one of a different
width is not, and a version range cannot express that distinction.

## One instance may satisfy several kinds

``graph-db`` and ``vector-store`` are separate kinds and are usually
the same instance — Neo4j holds the graph *and* a native vector index
on it, which is how both of igni's own capabilities use it.

That is a resolver's business, not a manifest's. A resolver maps each
kind to a connection and two kinds may map to the same one. Merging
them into a single kind would be the wrong fix: an extension that
wants semantic search over its own documents and no graph would have
to declare a graph database it never opens, and a deployment that
runs Qdrant beside Neo4j could not express that either.

So: **a kind is what the extension needs; an instance is what
satisfies it.** The mapping is the deployment's.

## The one rule that spans two requirements

An extension declaring both an ``embedder`` and a ``vector-store``
must give them the **same** dimension. Vectors written at one width
and queried at another do not fail — they return nonsense, slowly —
which is the worst failure available and the reason this is checked
at install rather than discovered in a search result.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ember_code.extensions.manifest import Capability, Requirement, ResolveMode

logger = logging.getLogger(__name__)

#: ``dim=384``. The only constraint form an embedder takes.
DIMENSION = re.compile(r"^dim=(\d+)$")


class UnresolvedRequirement(Exception):
    """A requirement this deployment cannot satisfy.

    Raised at install, never later: an extension that starts and
    then discovers it has no database is an extension that fails in
    front of a user instead of in front of the administrator who
    installed it.
    """


class Tenant(BaseModel):
    """Which slice of an instance belongs to one extension.

    Always populated, including on a laptop where there is only one
    of everything — see the module docstring.
    """

    #: For ``graph-db`` and ``relational-db``.
    database: str = ""
    #: For ``vector-store``: the collection or index prefix.
    namespace: str = ""

    @classmethod
    def sole(cls) -> Tenant:
        """The whole instance, which is what a laptop hands back."""
        return cls(database="", namespace="")


class Resolved(BaseModel):
    """A connection to something that exists.

    Never a process, never a handle to one. A resolver that wanted
    to return "the thing I started" would be making its caller
    responsible for stopping it, and the caller is an installer.
    """

    capability: Capability
    #: How to reach it. The scheme is defined by the kind — see the
    #: table in the module docstring.
    uri: str
    #: The *name* of a secret grant that resolves to the credential,
    #: never the credential. Empty when the instance needs none.
    secret: str = ""
    tenant: Tenant = Field(default_factory=Tenant.sole)
    #: What actually satisfied this, for an administrator reading a
    #: log: ``"provisioned"`` or ``"bound to the deployment's"``.
    how: str = ""


@runtime_checkable
class CapabilityResolver(Protocol):
    """Turns a declared requirement into a connection."""

    async def resolve(self, requirement: Requirement, *, extension_id: str) -> Resolved:
        """Satisfy one requirement, or raise
        :class:`UnresolvedRequirement` naming the fix."""
        ...


class BindingResolver:
    """A deployment's resolver. Binds, never builds.

    Constructed with what the infrastructure provides — one entry
    per kind, set up when the deployment was. A kind that is not in
    that table is not available here, and saying so at install is
    the whole job: the alternative is an extension starting and
    failing later against something that was never there.
    """

    def __init__(self, provided: dict[Capability, Resolved]) -> None:
        self._provided = dict(provided)

    async def resolve(self, requirement: Requirement, *, extension_id: str) -> Resolved:
        found = self._provided.get(requirement.capability)
        if found is None:
            raise UnresolvedRequirement(
                f"{extension_id} needs {requirement.capability.value} and this "
                f"deployment provides none. Available: "
                f"{sorted(c.value for c in self._provided) or 'nothing'}. "
                "A deployment resolver binds to infrastructure it was given; it "
                "does not start any."
            )
        # ``dedicated`` asks for an instance nobody else is on, which
        # a binding resolver cannot manufacture. Refused rather than
        # quietly downgraded to shared — the extension asked for
        # isolation and would not get it.
        if requirement.mode is ResolveMode.DEDICATED:
            raise UnresolvedRequirement(
                f"{extension_id} asks for a dedicated "
                f"{requirement.capability.value}, and this deployment binds to "
                "shared infrastructure rather than starting instances. Either "
                "provision a dedicated one and provide it, or change the "
                "manifest to 'shared'."
            )
        return found.model_copy(update={"how": "bound to the deployment's"})


class ProvisioningResolver:
    """A laptop's resolver. Builds what is not there.

    Delegates the building to one provisioner per kind, so that the
    rule — resolve returns a connection — stays in one place and the
    knowledge of how to download Neo4j stays in another.
    """

    def __init__(self, provisioners: dict[Capability, Provisioner]) -> None:
        self._provisioners = dict(provisioners)

    async def resolve(self, requirement: Requirement, *, extension_id: str) -> Resolved:
        if requirement.mode is ResolveMode.BIND_ONLY:
            # The one mode a provisioning resolver must refuse to
            # provision for. ``bind-only`` is what a customer's
            # change control requires, and starting an instance
            # anyway is exactly the thing it forbids.
            raise UnresolvedRequirement(
                f"{extension_id} asks for a bind-only "
                f"{requirement.capability.value}, meaning an existing instance "
                "and no new one. There is none here to bind to."
            )

        provisioner = self._provisioners.get(requirement.capability)
        if provisioner is None:
            raise UnresolvedRequirement(
                f"{extension_id} needs {requirement.capability.value} and igni "
                f"cannot provision one. Provisionable here: "
                f"{sorted(c.value for c in self._provisioners) or 'nothing'}."
            )
        resolved = await provisioner.provision(requirement, extension_id=extension_id)
        return resolved.model_copy(update={"how": "provisioned"})


@runtime_checkable
class Provisioner(Protocol):
    """Makes one kind of thing exist on this machine."""

    async def provision(self, requirement: Requirement, *, extension_id: str) -> Resolved:
        """Start or reuse an instance and return a connection."""
        ...


#: The kinds whose constraint is a width rather than a version.
SIZED = frozenset({Capability.EMBEDDER, Capability.VECTOR_STORE})


def dimension_of(requirement: Requirement) -> int | None:
    """The width this requirement asks for.

    ``None`` when the kind does not take one, and also when a sized
    kind's constraint is malformed — which is a manifest bug and is
    reported by :func:`check_dimensions_agree` rather than guessed
    at here.
    """
    if requirement.capability not in SIZED:
        return None
    match = DIMENSION.match(requirement.constraint.strip())
    return int(match.group(1)) if match else None


def check_dimensions_agree(requirements: list[Requirement], *, extension_id: str) -> None:
    """An extension's vector kinds must share one width.

    Vectors written at one width and queried at another do not fail.
    They return nonsense, slowly, which is the worst failure
    available — so this is an install-time refusal rather than
    something found in a search result weeks later.
    """
    widths: dict[str, int] = {}
    for requirement in requirements:
        if requirement.capability not in SIZED:
            continue
        width = dimension_of(requirement)
        if width is None:
            raise UnresolvedRequirement(
                f"{extension_id}: {requirement.capability.value} needs a width, "
                f"written 'dim=<n>', and its constraint is "
                f"{requirement.constraint!r}. A version range cannot say which "
                "vectors are comparable."
            )
        widths[requirement.capability.value] = width

    if len(set(widths.values())) > 1:
        pairs = ", ".join(f"{kind}={width}" for kind, width in sorted(widths.items()))
        raise UnresolvedRequirement(
            f"{extension_id} asks for vector kinds of different widths ({pairs}). "
            "Vectors written at one width and queried at another return "
            "nonsense rather than failing, so they have to agree."
        )
