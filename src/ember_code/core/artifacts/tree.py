"""The navigation tree, assembled from what people arranged.

A tree and the lineage graph answer different questions and must not
be the same structure.

``DERIVED_FROM`` says *this was built on that*. It is a citation: a
document has several sources, gains more as it is revised, and one
written from scratch has none. Rendered as a tree it would file a
document under whatever it happened to cite, give it two parents when
it cites two things, re-shape itself whenever somebody confirmed a
source, and leave every unsourced document with nowhere to live.

``CHILD_OF`` says *this belongs under that*. One parent, chosen by a
person, staying where they put it. That is navigation, and it is the
only thing this module builds.

**Two node kinds, one tree.** A document may hold other documents, the
way a page holds sub-pages, and a folder holds things without being
one. Folders are a *separate label* in the store rather than an
artifact with a flag: search, lineage, drift, provenance and the vector
index all key on ``:Artifact``, and a folder that satisfied that label
would turn up in every one of them as a document with no text. The
tree is the one place the two kinds meet, so it is the one place that
has to know about both.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: How deep the tree may go before it stops being navigation.
#:
#: Not a storage limit — the store refuses cycles on its own. This
#: bounds what is *rendered*, so a pathological arrangement degrades
#: into a flat tail rather than an unbounded indent.
MAX_DEPTH: int = 12


@dataclass
class TreeNode:
    """One entry in the tree, and what sits under it."""

    node_id: str
    name: str
    #: ``folder`` or the artifact's own kind (``design``, ``note``…).
    kind: str = "note"
    is_folder: bool = False
    head_n: int = 0
    updated_at: str = ""
    depth: int = 0
    children: list[TreeNode] = field(default_factory=list)
    #: True when this stands in for a parent that is missing or that
    #: the reader cannot open. It holds a place; there is nothing
    #: behind it to open.
    placeholder: bool = False

    @property
    def descendants(self) -> int:
        return len(self.children) + sum(c.descendants for c in self.children)

    @property
    def is_empty_folder(self) -> bool:
        return self.is_folder and not self.children


def _sort_key(node: TreeNode) -> tuple[int, str]:
    """Folders first, then by name.

    Folders first is a filesystem habit and it is right *here* for the
    reason it is right there: a folder is a place to go looking, and
    the places should not be scattered among the things. Documents that
    happen to have children are still documents and stay in name order
    with the rest.

    Name order rather than recency. The list sorts by most-recently-
    updated, which is right for a queue and wrong for navigation, where
    a thing has to stay where it was last time.
    """
    return (0 if node.is_folder else 1, node.name.casefold() or node.node_id)


def build(rows: list[dict], *, visible: set[str] | None = None) -> list[TreeNode]:
    """Assemble the forest from folder and document rows.

    Each row needs ``node_id``, ``name`` and ``parent_id``; folders say
    so with ``is_folder``. ``visible`` optionally restricts what the
    reader may see.

    **Nothing is ever dropped.** A node whose parent is missing —
    deleted, not yet synced, or not visible — surfaces under a
    placeholder rather than disappearing. A navigation tree that
    silently omits something is worse than one with an odd-looking
    branch: the document still exists and nobody can find it.
    """
    known = {r["node_id"]: r for r in rows}
    nodes: dict[str, TreeNode] = {
        r["node_id"]: TreeNode(
            node_id=r["node_id"],
            name=r.get("name") or r["node_id"],
            kind="folder" if r.get("is_folder") else (r.get("kind") or "note"),
            is_folder=bool(r.get("is_folder")),
            head_n=int(r.get("head_n") or 0),
            updated_at=r.get("updated_at") or "",
        )
        for r in rows
        if visible is None or r["node_id"] in visible
    }

    roots: list[TreeNode] = []
    placeholders: dict[str, TreeNode] = {}

    for row in rows:
        node = nodes.get(row["node_id"])
        if node is None:
            continue
        parent_id = (row.get("parent_id") or "").strip()
        if not parent_id:
            roots.append(node)
            continue

        parent = nodes.get(parent_id)
        if parent is not None:
            parent.children.append(node)
            continue

        if parent_id not in placeholders:
            missing = known.get(parent_id)
            placeholders[parent_id] = TreeNode(
                node_id=parent_id,
                name=(missing or {}).get("name") or "A document you cannot open",
                is_folder=bool((missing or {}).get("is_folder")),
                placeholder=True,
            )
            roots.append(placeholders[parent_id])
        placeholders[parent_id].children.append(node)

    def order(level: list[TreeNode], depth: int) -> list[TreeNode]:
        level.sort(key=_sort_key)
        for node in level:
            node.depth = depth
            node.children = order(node.children, depth + 1) if depth < MAX_DEPTH else []
        return level

    return order(roots, 0)


def flatten(nodes: list[TreeNode], *, expanded: set[str] | None = None) -> list[TreeNode]:
    """The tree as rows, honouring which branches are open.

    Kept here rather than in the component so the order shown and the
    order reasoned about are the same one.
    """
    out: list[TreeNode] = []
    for node in nodes:
        out.append(node)
        if expanded is None or node.node_id in expanded:
            out.extend(flatten(node.children, expanded=expanded))
    return out


def path_to(nodes: list[TreeNode], node_id: str) -> list[TreeNode]:
    """Every ancestor of one node, root first, including itself.

    What a breadcrumb needs, and what "show me where this lives" needs
    in order to open the right branches.
    """
    for node in nodes:
        if node.node_id == node_id:
            return [node]
        below = path_to(node.children, node_id)
        if below:
            return [node, *below]
    return []


def would_cycle(rows: list[dict], *, node_id: str, parent_id: str) -> bool:
    """Whether putting ``node_id`` under ``parent_id`` closes a loop.

    The one way a tree stops being one. A cycle does not error and
    does not look broken — the branch simply detaches from every root,
    so everything on it vanishes from navigation while still existing.
    That is the worst shape a bug can take here.

    Checked against rows rather than in the store so the answer is the
    same before the write is attempted, which is what lets the UI
    refuse a move rather than report a failure.
    """
    if not parent_id:
        return False
    if node_id == parent_id:
        return True
    parents = {r["node_id"]: (r.get("parent_id") or "") for r in rows}
    seen: set[str] = set()
    at = parent_id
    while at and at not in seen:
        if at == node_id:
            return True
        seen.add(at)
        at = parents.get(at, "")
    return False
