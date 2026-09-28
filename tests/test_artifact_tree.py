"""The navigation tree.

Two things it must never do, and both are silent failures rather than
errors: lose a document, and close a loop.

Losing one is the likelier of the two. A parent can be missing because
it was deleted, because it has not synced yet, or because the reader
may not open it — and in every case the child still exists while
nobody can find it. So nothing is dropped; what cannot be placed gets
a placeholder and keeps its position.

A cycle is rarer and worse. It does not error and does not look broken:
the branch simply detaches from every root, so everything on it
vanishes from navigation while still being in the store.
"""

from __future__ import annotations

from ember_code.core.artifacts.tree import (
    MAX_DEPTH,
    build,
    flatten,
    path_to,
    would_cycle,
)


def doc(node_id: str, name: str, parent: str = "", **over) -> dict:
    return {"node_id": node_id, "name": name, "parent_id": parent, "kind": "design", **over}


def folder(node_id: str, name: str, parent: str = "") -> dict:
    return {"node_id": node_id, "name": name, "parent_id": parent, "is_folder": True}


class TestShape:
    def test_a_flat_set_is_all_roots(self):
        roots = build([doc("a", "Alpha"), doc("b", "Beta")])
        assert [n.node_id for n in roots] == ["a", "b"]
        assert all(n.depth == 0 for n in roots)

    def test_a_document_may_hold_documents(self):
        # The Confluence shape: a page with sub-pages, no folder in
        # sight.
        roots = build([doc("a", "Auth model"), doc("b", "Login flow", parent="a")])
        assert [n.node_id for n in roots] == ["a"]
        assert [c.node_id for c in roots[0].children] == ["b"]
        assert roots[0].children[0].depth == 1

    def test_a_folder_may_hold_documents(self):
        roots = build([folder("f", "Design"), doc("a", "Auth model", parent="f")])
        assert roots[0].is_folder
        assert [c.name for c in roots[0].children] == ["Auth model"]

    def test_folders_and_documents_nest_either_way(self):
        rows = [
            folder("f", "Design"),
            doc("a", "Auth model", parent="f"),
            folder("g", "Details", parent="a"),
            doc("b", "Sessions", parent="g"),
        ]
        roots = build(rows)
        assert [n.name for n in flatten(roots)] == [
            "Design",
            "Auth model",
            "Details",
            "Sessions",
        ]

    def test_depth_is_recorded_for_indenting(self):
        rows = [doc("a", "A"), doc("b", "B", parent="a"), doc("c", "C", parent="b")]
        assert [n.depth for n in flatten(build(rows))] == [0, 1, 2]

    def test_it_stops_indenting_past_the_limit(self):
        # Degrades to a flat tail rather than an unbounded indent.
        rows = [doc("n0", "n0")]
        rows += [doc(f"n{i}", f"n{i}", parent=f"n{i - 1}") for i in range(1, MAX_DEPTH + 6)]
        assert max(n.depth for n in flatten(build(rows))) <= MAX_DEPTH


class TestOrder:
    def test_folders_come_first(self):
        # A folder is a place to go looking, and the places should not
        # be scattered among the things.
        roots = build([doc("a", "Alpha"), folder("z", "Zebra")])
        assert [n.name for n in roots] == ["Zebra", "Alpha"]

    def test_then_by_name_not_by_recency(self):
        # The list sorts by most-recently-updated, which is right for a
        # queue and wrong for navigation: a thing has to stay where it
        # was last time.
        rows = [
            doc("a", "Beta", updated_at="2026-09-28"),
            doc("b", "Alpha", updated_at="2026-01-01"),
        ]
        assert [n.name for n in build(rows)] == ["Alpha", "Beta"]

    def test_order_ignores_case(self):
        assert [n.name for n in build([doc("a", "beta"), doc("b", "Alpha")])] == [
            "Alpha",
            "beta",
        ]

    def test_a_document_with_children_stays_in_name_order(self):
        rows = [doc("a", "Zebra"), doc("b", "Alpha"), doc("c", "child", parent="a")]
        assert [n.name for n in build(rows)] == ["Alpha", "Zebra"]


class TestNothingIsLost:
    def test_a_child_of_a_missing_parent_keeps_its_place(self):
        roots = build([doc("b", "Orphan", parent="gone")])
        assert len(roots) == 1
        assert roots[0].placeholder
        assert [c.name for c in roots[0].children] == ["Orphan"]

    def test_a_child_of_an_invisible_parent_is_not_promoted(self):
        # Promoting it to the root would be tidier and would lie about
        # where the document lives.
        rows = [doc("p", "Secret"), doc("c", "Visible", parent="p")]
        roots = build(rows, visible={"c"})
        assert roots[0].placeholder
        assert roots[0].name == "Secret" or roots[0].name == "A document you cannot open"
        assert [c.name for c in roots[0].children] == ["Visible"]

    def test_several_orphans_share_one_placeholder(self):
        rows = [doc("a", "One", parent="gone"), doc("b", "Two", parent="gone")]
        roots = build(rows)
        assert len(roots) == 1
        assert [c.name for c in roots[0].children] == ["One", "Two"]

    def test_an_invisible_document_is_absent_not_placeheld(self):
        # It has no place in *this* reader's tree at all.
        roots = build([doc("a", "Secret"), doc("b", "Mine")], visible={"b"})
        assert [n.name for n in roots] == ["Mine"]


class TestCycles:
    ROWS = [doc("a", "A"), doc("b", "B", parent="a"), doc("c", "C", parent="b")]

    def test_a_node_cannot_go_under_itself(self):
        assert would_cycle(self.ROWS, node_id="a", parent_id="a")

    def test_a_node_cannot_go_under_its_own_descendant(self):
        # The move that detaches a branch from every root.
        assert would_cycle(self.ROWS, node_id="a", parent_id="c")

    def test_an_ordinary_move_is_allowed(self):
        assert not would_cycle(self.ROWS, node_id="c", parent_id="a")

    def test_moving_to_the_root_is_allowed(self):
        assert not would_cycle(self.ROWS, node_id="c", parent_id="")

    def test_it_terminates_on_a_tree_that_is_already_looped(self):
        looped = [doc("a", "A", parent="b"), doc("b", "B", parent="a")]
        assert would_cycle(looped, node_id="a", parent_id="b") is True


class TestFlattenAndPath:
    ROWS = [
        folder("f", "Design"),
        doc("a", "Auth model", parent="f"),
        doc("b", "Login flow", parent="a"),
    ]

    def test_collapsed_branches_are_not_listed(self):
        roots = build(self.ROWS)
        assert [n.name for n in flatten(roots, expanded=set())] == ["Design"]

    def test_expanding_one_level_shows_its_children_only(self):
        roots = build(self.ROWS)
        assert [n.name for n in flatten(roots, expanded={"f"})] == ["Design", "Auth model"]

    def test_the_path_to_a_node_is_its_ancestors(self):
        # What a breadcrumb needs, and what opening the right branches
        # needs.
        roots = build(self.ROWS)
        assert [n.name for n in path_to(roots, "b")] == ["Design", "Auth model", "Login flow"]

    def test_the_path_to_something_absent_is_empty(self):
        assert path_to(build(self.ROWS), "nope") == []


class TestCounts:
    def test_descendants_counts_the_whole_branch(self):
        rows = [doc("a", "A"), doc("b", "B", parent="a"), doc("c", "C", parent="b")]
        assert build(rows)[0].descendants == 2

    def test_an_empty_folder_says_so(self):
        roots = build([folder("f", "Empty")])
        assert roots[0].is_empty_folder
