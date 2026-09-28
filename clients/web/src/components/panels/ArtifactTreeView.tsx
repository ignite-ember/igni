import { useCallback, useEffect, useMemo, useState } from "react";

import type { IgniClient } from "../../protocol/client";

/**
 * Where documents live, as against what they refer to.
 *
 * The lineage graph answers "built on what". This answers "kept
 * where", and they are different facts: a document cites several
 * things and gains more over time, but it sits in exactly one place
 * and stays there until somebody moves it.
 *
 * Two node kinds share the tree. A folder holds things without being
 * one; a document may hold other documents, the way a page holds
 * sub-pages. Both are moved the same way, because to a reader
 * arranging their work the difference is not interesting.
 *
 * The server sends the tree **already flattened and ordered**, with a
 * depth per row. The order shown and the order reasoned about are
 * then the same one, and this component's job is indentation,
 * disclosure and moving — not sorting.
 */

export interface TreeNode {
  node_id: string;
  name: string;
  kind: string;
  is_folder: boolean;
  head_n: number;
  depth: number;
  descendants: number;
  placeholder: boolean;
}

interface Tree {
  nodes: TreeNode[];
  error: string;
}

interface Changed {
  ok: boolean;
  node_id: string;
  promoted: number;
  message: string;
  error: string;
}

/**
 * Everything below a row, read off the flattened list.
 *
 * Depth is enough: the rows after it that are deeper belong to it,
 * and the first one at its own depth or shallower ends the branch.
 * Computing it here means a move can be refused while the pointer is
 * still moving, rather than after a round trip.
 */
export function branchOf(nodes: TreeNode[], at: number): Set<string> {
  const out = new Set<string>();
  const root = nodes[at];
  if (!root) return out;
  out.add(root.node_id);
  for (let i = at + 1; i < nodes.length && nodes[i].depth > root.depth; i++) {
    out.add(nodes[i].node_id);
  }
  return out;
}

/**
 * Rows whose title matches, plus every ancestor of each.
 *
 * The ancestors are the point. A match shown without the branch it
 * sits on tells you a document exists and not where it is, which is
 * the one thing a tree is for — and the centre column already answers
 * "which documents mention this".
 */
export function filterRows(nodes: TreeNode[], query: string): TreeNode[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return nodes;

  const keep = new Set<number>();
  for (let i = 0; i < nodes.length; i++) {
    if (!nodes[i].name.toLowerCase().includes(needle)) continue;
    keep.add(i);
    // Walk back up: the previous row one level shallower is the
    // parent, and so on to the root.
    let depth = nodes[i].depth;
    for (let j = i - 1; j >= 0 && depth > 0; j--) {
      if (nodes[j].depth < depth) {
        keep.add(j);
        depth = nodes[j].depth;
      }
    }
  }
  return nodes.filter((_, i) => keep.has(i));
}

/** Which rows are hidden because an ancestor is collapsed. */
export function visibleRows(nodes: TreeNode[], collapsed: Set<string>): TreeNode[] {
  const out: TreeNode[] = [];
  let hiddenBelow: number | null = null;
  for (const node of nodes) {
    if (hiddenBelow !== null && node.depth > hiddenBelow) continue;
    hiddenBelow = null;
    out.push(node);
    if (collapsed.has(node.node_id)) hiddenBelow = node.depth;
  }
  return out;
}

/** A page. Outlined, so it reads at 14px without becoming a blob. */
function PageIcon() {
  return (
    <svg className="kb-tree-glyph" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <rect x="2.5" y="1.8" width="11" height="12.4" rx="2" fill="none"
        stroke="currentColor" strokeWidth="1.3" />
      <path d="M5.4 5.6h5.2M5.4 8h5.2M5.4 10.4h3.2" stroke="currentColor"
        strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}

/** A folder, open or closed — the state is worth showing twice. */
function FolderIcon({ open }: { open: boolean }) {
  return (
    <svg className="kb-tree-glyph" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      {open ? (
        <path d="M1.9 12.6V4.1a.9.9 0 0 1 .9-.9h3.1l1.5 1.7h4.8a.9.9 0 0 1 .9.9v1.1M1.9 12.6l1.7-4.6h11L12.9 12.6a.9.9 0 0 1-.85.6H2.8a.9.9 0 0 1-.9-.6Z"
          fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
      ) : (
        <path d="M2 12.4V4a.9.9 0 0 1 .9-.9h3.2l1.5 1.7h5.5a.9.9 0 0 1 .9.9v6.7a.9.9 0 0 1-.9.9H2.9a.9.9 0 0 1-.9-.9Z"
          fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
      )}
    </svg>
  );
}

export function ArtifactTreeView({
  client,
  selectedId,
  staleIds,
  onOpen,
}: {
  client: IgniClient;
  selectedId?: string | null;
  staleIds?: Set<string>;
  onOpen: (id: string) => void;
}) {
  const [nodes, setNodes] = useState<TreeNode[] | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    try {
      const tree = await client.rpc<Tree>("artifact_tree", {});
      setNodes(tree?.nodes ?? []);
      setError(tree?.error ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = useCallback(
    async (method: string, args: Record<string, unknown>) => {
      setError("");
      try {
        const r = await client.rpc<Changed>(method, args);
        if (r?.error) {
          setError(r.error);
          return false;
        }
        // Said out loud: removing a folder moves its contents rather
        // than deleting them, and a reader needs to be told which.
        setNote(r?.message ?? "");
        await load();
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return false;
      }
    },
    [client, load],
  );

  const rows = useMemo(() => {
    // Filtering ignores what is collapsed: a query is a request to be
    // shown the matches, not to be shown the matches you had already
    // opened.
    if (query.trim()) return filterRows(nodes ?? [], query);
    return visibleRows(nodes ?? [], collapsed);
  }, [nodes, collapsed, query]);

  /** The branch being dragged — never a valid destination for itself. */
  const forbidden = useMemo(() => {
    if (!dragging || !nodes) return new Set<string>();
    return branchOf(
      nodes,
      nodes.findIndex((n) => n.node_id === dragging),
    );
  }, [dragging, nodes]);

  const hasChildren = (node: TreeNode, index: number) =>
    index + 1 < rows.length ? rows[index + 1].depth > node.depth : node.descendants > 0;

  if (error && nodes === null) return <div className="msg-error">{error}</div>;
  if (nodes === null) return <p className="kb-hint">Reading the tree…</p>;

  return (
    <div className="kb-tree">
      <div className="kb-tree-bar">
        <input
          className="kb-tree-filter"
          type="search"
          placeholder="Search by title"
          aria-label="Search the tree by title"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button
          type="button"
          className="btn btn-xs kb-tree-new"
          onClick={() => void act("artifact_create_folder", { name: "New folder" })}
        >
          New folder
        </button>
      </div>

      {error && <p className="msg-error">{error}</p>}
      {note && <p className="kb-hint kb-tree-note">{note}</p>}

      <ul className="kb-tree-rows">
        {rows.map((node, index) => {
          const open = !collapsed.has(node.node_id);
          const expandable = hasChildren(node, index);
          const isTarget = over === node.node_id && !forbidden.has(node.node_id);
          return (
            <li
              key={node.node_id}
              className={[
                "kb-tree-row",
                node.is_folder ? "is-folder" : "",
                node.placeholder ? "is-placeholder" : "",
                node.node_id === selectedId ? "is-current" : "",
                isTarget ? "is-target" : "",
                dragging === node.node_id ? "is-dragging" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              style={{ "--tree-depth": node.depth } as React.CSSProperties}
              draggable={!node.placeholder}
              onDragStart={() => setDragging(node.node_id)}
              onDragEnd={() => {
                setDragging(null);
                setOver(null);
              }}
              onDragOver={(e) => {
                // Dropping a branch inside itself would detach it from
                // every root, so it is refused here rather than being
                // attempted and reported.
                if (!dragging || forbidden.has(node.node_id) || node.placeholder) return;
                e.preventDefault();
                setOver(node.node_id);
              }}
              onDragLeave={() => setOver((on) => (on === node.node_id ? null : on))}
              onDrop={(e) => {
                e.preventDefault();
                const moving = dragging;
                setDragging(null);
                setOver(null);
                if (!moving || forbidden.has(node.node_id)) return;
                void act("artifact_move_node", {
                  node_id: moving,
                  parent_id: node.node_id,
                });
              }}
            >
              <button
                type="button"
                className="kb-tree-twist"
                aria-label={open ? "Collapse" : "Expand"}
                aria-expanded={expandable ? open : undefined}
                disabled={!expandable}
                onClick={() =>
                  setCollapsed((prev) => {
                    const next = new Set(prev);
                    if (next.has(node.node_id)) next.delete(node.node_id);
                    else next.add(node.node_id);
                    return next;
                  })
                }
              >
                {expandable ? (
                  <svg viewBox="0 0 12 12" width="10" height="10" aria-hidden="true">
                    <path
                      d={open ? "M2.5 4.5 6 8l3.5-3.5" : "M4.5 2.5 8 6l-3.5 3.5"}
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.6"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                ) : (
                  /* A dot where the chevron would be. Empty space says
                     nothing; this says "nothing under here". */
                  <span className="kb-tree-leaf" aria-hidden="true" />
                )}
              </button>

              {/* Both kinds are marked, and the folder shows whether
                  it is open. The two decide what a drop will do, and
                  at a glance an outline is quicker to tell apart than
                  a word. */}
              <span className="kb-tree-icon">
                {node.is_folder ? <FolderIcon open={open} /> : <PageIcon />}
              </span>

              {renaming === node.node_id ? (
                <input
                  className="kb-tree-rename"
                  autoFocus
                  value={draft}
                  aria-label="Folder name"
                  onChange={(e) => setDraft(e.target.value)}
                  onBlur={() => setRenaming(null)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") setRenaming(null);
                    if (e.key !== "Enter") return;
                    setRenaming(null);
                    void act("artifact_rename_folder", {
                      folder_id: node.node_id,
                      name: draft,
                    });
                  }}
                />
              ) : (
                <button
                  type="button"
                  className="kb-tree-name"
                  disabled={node.placeholder}
                  onDoubleClick={() => {
                    if (!node.is_folder) return;
                    setDraft(node.name);
                    setRenaming(node.node_id);
                  }}
                  onClick={() => {
                    if (node.placeholder) return;
                    if (node.is_folder) {
                      setCollapsed((prev) => {
                        const next = new Set(prev);
                        if (next.has(node.node_id)) next.delete(node.node_id);
                        else next.add(node.node_id);
                        return next;
                      });
                      return;
                    }
                    onOpen(node.node_id);
                  }}
                >
                  {node.name}
                </button>
              )}

              {/* No version number. It is metadata about a document
                  rather than a way of telling two apart, and a column
                  of it beside the titles is what made this read as a
                  table. */}
              {staleIds?.has(node.node_id) && (
                <span className="kb-tree-stale">out of date</span>
              )}
              {node.is_folder && !node.placeholder && (
                <button
                  type="button"
                  className="kb-tree-remove"
                  title="Remove this folder — whatever is inside moves up"
                  onClick={() =>
                    void act("artifact_delete_folder", { folder_id: node.node_id })
                  }
                >
                  ×
                </button>
              )}
            </li>
          );
        })}
      </ul>

      {/* The way back to the top level. Without it a document dragged
          into a folder can only be moved to another folder, and the
          root stops being reachable once everything is nested. */}
      <div
        className={over === "" ? "kb-tree-root is-target" : "kb-tree-root"}
        onDragOver={(e) => {
          if (!dragging) return;
          e.preventDefault();
          setOver("");
        }}
        onDragLeave={() => setOver((on) => (on === "" ? null : on))}
        onDrop={(e) => {
          e.preventDefault();
          const moving = dragging;
          setDragging(null);
          setOver(null);
          if (moving) void act("artifact_move_node", { node_id: moving, parent_id: "" });
        }}
      >
        Top level
      </div>
    </div>
  );
}
