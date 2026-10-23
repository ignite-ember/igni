import { useCallback, useEffect, useMemo, useState } from "react";

import type { IgniClient } from "../../protocol/client";
import { MenuIcon } from "@igni/ui-react";
import { Tooltip } from "@igni/ui-react";
import { TreeNotice } from "./TreeNotice";

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
  archived?: boolean;
}

interface Tree {
  nodes: TreeNode[];
  error: string;
}

interface Dependent {
  artifact_id: string;
  name: string;
  state: string;
  /** ``dependent`` — built on this one. ``source`` — this was built on it. */
  direction: string;
}

interface Changed {
  ok: boolean;
  node_id: string;
  promoted: number;
  message: string;
  error: string;
  blocked_by?: Dependent[];
  suggest_archive?: boolean;
}

/** The open menu: which row, which of the two, and where to draw it. */
interface Menu {
  id: string;
  kind: "create" | "more";
  top: number;
  right: number;
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
  reloadKey,
  onOpen,
  onCreateInside,
  onChanged,
}: {
  client: IgniClient;
  selectedId?: string | null;
  staleIds?: Set<string>;
  /** Changes when the documents changed elsewhere on the page. */
  reloadKey?: number;
  onOpen: (id: string) => void;
  /** Start a new document under this row. The panel collects the
   *  text — the tree only says where it goes, and what that place is
   *  called, so the form can say so rather than filing it silently. */
  onCreateInside: (parentId: string, parentName: string) => void;
  /** Pull in what other machines have shared through git. A verb
   *  about the collection, which is what this bar is for — it used
   *  to be a button on the search field, where the page opened with
   *  a row of controls instead of with the documents. */
  /**
   * The set of documents changed from in here.
   *
   * Archiving takes one out of the list as well as out of the tree,
   * and a list still showing it is a list the reader will click —
   * and then wonder why the document they just put away is open.
   */
  onChanged?: () => void;
}) {
  const [nodes, setNodes] = useState<TreeNode[] | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  /**
   * The open menu, if any — which row, which of the two, and where.
   *
   * Where, because the menu cannot live inside the row it belongs to.
   * The rows scroll, and an absolutely positioned menu inside a
   * scrolling box is clipped by it: the first version came out with
   * its right edge and its shadow sheared off along the scroller's
   * boundary. So it is rendered once, outside, against the viewport,
   * anchored to the button that opened it.
   */
  const [menu, setMenu] = useState<Menu | null>(null);
  /**
   * Whether archived documents are shown, dimmed, in place.
   *
   * In the same tree rather than on a screen of their own. Restoring
   * one is a question about where it goes back to, and that question
   * is only answerable next to the rest of the tree.
   */
  const [showArchived, setShowArchived] = useState(false);
  /** A delete that was refused, and what refused it. */
  const [blocked, setBlocked] = useState<{
    id: string;
    name: string;
    by: Dependent[];
  } | null>(null);

  const load = useCallback(async () => {
    try {
      const tree = await client.rpc<Tree>("artifact_tree", {
        include_archived: showArchived,
      });
      setNodes(tree?.nodes ?? []);
      setError(tree?.error ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    // ``reloadKey`` is a dependency the body does not read. It is the
    // page saying the set of documents changed underneath this — an
    // add or a delete from the centre column — and re-reading is the
    // whole point of being told.
  }, [client, showArchived, reloadKey]);

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
        setBlocked(null);
        // Said out loud: removing a folder moves its contents rather
        // than deleting them, and a reader needs to be told which.
        setNote(r?.message ?? "");
        await load();
        onChanged?.();
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return false;
      }
    },
    [client, load],
  );

  const rows = useMemo(() => visibleRows(nodes ?? [], collapsed), [nodes, collapsed]);

  /** The row the open menu belongs to, or nothing if it has gone. */
  const menuNode = useMemo(
    () => (menu ? (nodes ?? []).find((n) => n.node_id === menu.id) : undefined),
    [menu, nodes],
  );

  /** The branch being dragged — never a valid destination for itself. */
  const forbidden = useMemo(() => {
    if (!dragging || !nodes) return new Set<string>();
    return branchOf(
      nodes,
      nodes.findIndex((n) => n.node_id === dragging),
    );
  }, [dragging, nodes]);

  /**
   * A folder, straight into rename.
   *
   * "New folder" then find it then rename it is three steps for one
   * intention. It is created, opened, and its name selected, so the
   * next thing typed is the thing it is called.
   */
  const addFolder = useCallback(
    async (parentId: string) => {
      const r = await client
        .rpc<Changed>("artifact_create_folder", { name: "New folder", parent_id: parentId })
        .catch((e) => ({ error: e instanceof Error ? e.message : String(e) }) as Changed);
      if (r?.error) {
        setError(r.error);
        return;
      }
      if (parentId) {
        setCollapsed((prev) => {
          const next = new Set(prev);
          next.delete(parentId);
          return next;
        });
      }
      await load();
      if (r?.node_id) {
        setDraft("New folder");
        setRenaming(r.node_id);
      }
    },
    [client, load],
  );

  /**
   * Open a menu against the button that asked for it.
   *
   * The rect is read now and kept, rather than the menu being placed
   * inside the row: the rows scroll, and anything absolutely
   * positioned inside a scrolling box is clipped by it — the first
   * version came out with its right edge and its shadow sheared off
   * along the scroller's boundary.
   */
  const openMenu = (
    e: React.MouseEvent<HTMLButtonElement>,
    node: TreeNode,
    kind: "create" | "more",
  ) => {
    const box = e.currentTarget.getBoundingClientRect();
    setMenu((on) =>
      on?.id === node.node_id && on.kind === kind
        ? null
        : {
            id: node.node_id,
            kind,
            top: box.bottom + 4,
            right: Math.max(8, window.innerWidth - box.right),
          },
    );
  };

  /**
   * Delete, or say why not.
   *
   * The refusal is the point. Deleting takes the whole version chain
   * with it, so anything built on any version of it breaks — and it
   * breaks somewhere else, later, on a document whose sources
   * silently stop saying what they were.
   */
  const removeDocument = async (node: TreeNode) => {
    setError("");
    setNote("");
    setBlocked(null);
    try {
      const r = await client.rpc<Changed>("artifact_delete_node", { node_id: node.node_id });
      if (r?.blocked_by?.length) {
        setBlocked({ id: node.node_id, name: node.name, by: r.blocked_by });
        return;
      }
      if (r?.error) {
        setError(r.error);
        return;
      }
      setNote(r?.message ?? "");
      await load();
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const hasChildren = (node: TreeNode, index: number) =>
    index + 1 < rows.length ? rows[index + 1].depth > node.depth : node.descendants > 0;

  if (error && nodes === null) return <div className="msg-error">{error}</div>;
  if (nodes === null) return <p className="kb-hint">Reading the tree…</p>;

  return (
    <div className="kb-tree">
      {/* No filter here. The centre column already searches the
          documents, and a second box over the same names is two
          places to look for one answer. */}
      {/* Three glyphs where three sentences used to be. The bar sat
          above the tree in two wrapped rows — two labelled buttons
          and a toggle whose wording changed under the pointer — and
          it cost more vertical room than several folders. These are
          the verbs a file tree has; they do not need explaining
          every time the column is looked at, and the tooltip is
          there for the once they do. */}
      <div className="kb-tree-bar">
        <Tooltip text="New document">
          <button
            type="button"
            className="kb-tree-act"
            aria-label="New document"
            onClick={() => onCreateInside("", "")}
          >
            <MenuIcon name="document-add" />
          </button>
        </Tooltip>
        <Tooltip text="New folder">
          <button
            type="button"
            className="kb-tree-act"
            aria-label="New folder"
            onClick={() => void addFolder("")}
          >
            <MenuIcon name="folder-add" />
          </button>
        </Tooltip>
        <span className="kb-tree-bar-gap" />
        <Tooltip
          text={
            showArchived
              ? "Hide archived documents"
              : "Show archived documents. Archived keeps every version, and anything built on one still resolves — only its content is frozen."
          }
        >
        <button
          type="button"
          className={showArchived ? "kb-tree-act is-on" : "kb-tree-act"}
          // A toggle, said as one. ``aria-pressed`` is what makes the
          // state readable without the label having to change from
          // "Show archived" to "Hide archived" — wording that moves
          // under the pointer is wording you have to read twice to
          // work out which way it is about to go.
          aria-pressed={showArchived}
          aria-label="Show archived documents"
          onClick={() => setShowArchived((on) => !on)}
        >
          <MenuIcon name="archive" />
        </button>
        </Tooltip>
      </div>

      {/* Both raised over the page rather than printed here. A
          sentence about what just happened used to push every folder
          down by two lines to announce that one of them had gone. */}
      <TreeNotice text={error} tone="refused" onDismiss={() => setError("")} />
      <TreeNotice text={note} onDismiss={() => setNote("")} />

      {/* Named, not counted. "3 documents depend on this" tells a
          reader they are stuck; naming them tells them what to go and
          change, which is the only version of this that leads
          anywhere. */}
      {blocked && (
        <div className="kb-tree-blocked">
          <p>
            <strong>{blocked.name}</strong> was not deleted. It is part of the
            lineage:
          </p>
          <ul>
            {blocked.by.map((d) => (
              <li key={d.artifact_id}>
                <button
                  type="button"
                  className="kb-tree-blocked-link"
                  onClick={() => onOpen(d.artifact_id)}
                >
                  {d.name || d.artifact_id}
                </button>{" "}
                {/* Which way the link runs, per row. Without it the
                    reader cannot tell whether to go and change that
                    document or this one. */}
                <span className="kb-tree-blocked-way">
                  {d.direction === "source" ? "— this is built on it" : "— built on this"}
                </span>
              </li>
            ))}
          </ul>
          <p className="kb-hint">
            Archiving keeps every version and every one of those links resolving.
            An archived document is frozen: it stays readable and stops changing.
          </p>
          <div className="kb-tree-blocked-do">
            <button
              type="button"
              className="btn btn-xs"
              onClick={() => {
                const id = blocked.id;
                setBlocked(null);
                void act("artifact_archive_node", { node_id: id, archived: true });
              }}
            >
              Archive instead
            </button>
            <button type="button" className="btn btn-xs" onClick={() => setBlocked(null)}>
              Leave it
            </button>
          </div>
        </div>
      )}

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
                node.archived ? "is-archived" : "",
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
              {node.archived && <span className="kb-tree-badge">archived</span>}
              {staleIds?.has(node.node_id) && (
                <span className="kb-tree-stale">out of date</span>
              )}

              {/* Hidden until the row is pointed at or focused. Shown
                  always, two controls on every row is more furniture
                  than tree. */}
              {!node.placeholder && (
                <span className="kb-tree-actions">
                  <Tooltip text={`New document or folder in ${node.name}`}>
                  <button
                    type="button"
                    className={
                      menu?.id === node.node_id && menu.kind === "create"
                        ? "kb-tree-act is-open"
                        : "kb-tree-act"
                    }
                    aria-label={`Create inside ${node.name}`}
                    aria-expanded={menu?.id === node.node_id && menu.kind === "create"}
                    onClick={(e) => openMenu(e, node, "create")}
                  >
                    +
                  </button>
                  </Tooltip>
                  <Tooltip text={`More actions for ${node.name}`}>
                  <button
                    type="button"
                    className={
                      menu?.id === node.node_id && menu.kind === "more"
                        ? "kb-tree-act is-open"
                        : "kb-tree-act"
                    }
                    aria-label={`More actions for ${node.name}`}
                    aria-expanded={menu?.id === node.node_id && menu.kind === "more"}
                    onClick={(e) => openMenu(e, node, "more")}
                  >
                    ⋯
                  </button>
                  </Tooltip>
                </span>
              )}
            </li>
          );
        })}
      </ul>

      {/* Drawn here rather than inside the row, and against the
          viewport, so the scrolling list cannot clip it. */}
      {menu && menuNode && (
        <>
          <div className="kb-tree-backdrop" onClick={() => setMenu(null)} />
          <ul
            className="kb-tree-menu"
            role="menu"
            style={{ top: menu.top, right: menu.right }}
          >
            {menu.kind === "create" ? (
              <>
                <li>
                  <button
                    type="button"
                    role="menuitem"
                    className="popup-item overflow-item"
                    onClick={() => {
                      const id = menuNode.node_id;
                      setMenu(null);
                      setCollapsed((prev) => {
                        const next = new Set(prev);
                        next.delete(id);
                        return next;
                      });
                      onCreateInside(id, menuNode.name);
                    }}
                  >
                    <MenuIcon name="document" />
                    Document
                  </button>
                </li>
                <li>
                  <button
                    type="button"
                    role="menuitem"
                    className="popup-item overflow-item"
                    onClick={() => {
                      const id = menuNode.node_id;
                      setMenu(null);
                      void addFolder(id);
                    }}
                  >
                    <MenuIcon name="folder" />
                    Folder
                  </button>
                </li>
              </>
            ) : (
              <>
                {!menuNode.is_folder && (
                  <li>
                    <button
                      type="button"
                      role="menuitem"
                      className="popup-item overflow-item"
                      onClick={() => {
                        const id = menuNode.node_id;
                        setMenu(null);
                        onOpen(id);
                      }}
                    >
                      <MenuIcon name="open" />
                      Open
                    </button>
                  </li>
                )}
                {menuNode.is_folder && (
                  <li>
                    <button
                      type="button"
                      role="menuitem"
                      className="popup-item overflow-item"
                      onClick={() => {
                        setMenu(null);
                        setDraft(menuNode.name);
                        setRenaming(menuNode.node_id);
                      }}
                    >
                      <MenuIcon name="rename" />
                      Rename
                    </button>
                  </li>
                )}
                {menuNode.depth > 0 && (
                  <li>
                    <button
                      type="button"
                      role="menuitem"
                      className="popup-item overflow-item"
                      onClick={() => {
                        const id = menuNode.node_id;
                        setMenu(null);
                        void act("artifact_move_node", { node_id: id, parent_id: "" });
                      }}
                    >
                      <MenuIcon name="outdent" />
                      Move to top level
                    </button>
                  </li>
                )}

                {/* Archiving and deleting are different answers to the
                    same wish, and the difference is what happens to the
                    documents built on this one. Archive is listed
                    first because it is the one that is always safe. */}
                {!menuNode.is_folder && (
                  <li className="kb-tree-menu-sep">
                    <button
                      type="button"
                      role="menuitem"
                      className="popup-item overflow-item"
                      title={
                        menuNode.archived
                          ? "Put it back in the tree"
                          : "Out of the tree and the list; anything built on it still resolves"
                      }
                      onClick={() => {
                        const id = menuNode.node_id;
                        const archived = !menuNode.archived;
                        setMenu(null);
                        void act("artifact_archive_node", { node_id: id, archived });
                      }}
                    >
                      <MenuIcon name={menuNode.archived ? "restore" : "archive"} />
                      {menuNode.archived ? "Restore" : "Archive"}
                    </button>
                  </li>
                )}
                {!menuNode.is_folder && (
                  <li>
                    <button
                      type="button"
                      role="menuitem"
                      className="popup-item overflow-item is-destructive"
                      title="Refused while another document is built on this one"
                      onClick={() => {
                        const node = menuNode;
                        setMenu(null);
                        void removeDocument(node);
                      }}
                    >
                      <MenuIcon name="remove" />
                      Delete
                    </button>
                  </li>
                )}
                {menuNode.is_folder && (
                  <li className="kb-tree-menu-sep">
                    <button
                      type="button"
                      role="menuitem"
                      className="popup-item overflow-item"
                      /* Not "delete": a folder is an arrangement, and
                         what is inside it survives. Saying "delete"
                         beside a folder full of documents is a
                         sentence people read twice. */
                      title="Whatever is inside moves up a level"
                      onClick={() => {
                        const id = menuNode.node_id;
                        setMenu(null);
                        void act("artifact_delete_folder", { folder_id: id });
                      }}
                    >
                      <MenuIcon name="remove" />
                      Remove folder
                    </button>
                  </li>
                )}
              </>
            )}
          </ul>
        </>
      )}

      {/* The way back to the top level, and only while something is
          being carried there. Without it a document dragged into a
          folder can only ever be moved to another folder — but shown
          the rest of the time it is a labelled box under the tree
          that does nothing and names no destination, which reads as a
          control nobody can explain. */}
      {dragging !== null && (
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
        Move to the top level
      </div>
      )}
    </div>
  );
}
