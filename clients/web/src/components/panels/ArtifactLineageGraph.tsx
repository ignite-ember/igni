import { useCallback, useMemo } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import dagre from "@dagrejs/dagre";
import "@xyflow/react/dist/style.css";

/**
 * The lineage of a result, drawn as the directed graph it is.
 *
 * This replaces a hand-rolled SVG that placed nodes on a circle or in
 * two rows. That was fine for three nodes and wrong for thirty: a
 * ring hides direction, and direction is the whole point — "these
 * were built on that" reads in the arrows or not at all.
 *
 * Laid out with dagre rather than a force simulation. Derivation is a
 * DAG, so rank order carries real information, and a hierarchical
 * layout is also
 * deterministic — the same query draws the same picture twice, which
 * matters when someone is comparing two runs. A force layout settles
 * somewhere slightly different every time.
 *
 * Nodes are components, not circles with a caption. That is the thing
 * a library buys here: a node can carry its name, its version and a
 * stale badge, and still be one click away from opening the document.
 */

export interface LineageNode {
  id: string;
  label: string;
  kind: string;
  version?: number | null;
}

export interface LineageEdge {
  source: string;
  target: string;
  label?: string;
  stale?: boolean;
}

type ArtifactNodeData = {
  label: string;
  version?: number | null;
  isSource: boolean;
  stale: boolean;
  onOpen: () => void;
};

// Dagre needs a box per node before it can rank them. These match the
// CSS below; a mismatch shows up as edges that stop short of a node
// or overlap it.
const NODE_W = 188;
const NODE_H = 46;

function ArtifactNode({ data }: NodeProps) {
  const d = data as unknown as ArtifactNodeData;
  return (
    <div
      className={
        "lin-node" +
        (d.isSource ? " lin-node-source" : "") +
        (d.stale ? " lin-node-stale" : "")
      }
      onClick={d.onOpen}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          d.onOpen();
        }
      }}
      title={d.label}
    >
      {/* Both handles on every node: a document can be somebody's
          source and have sources of its own, and a node that could
          only receive edges would break the middle of a chain. */}
      <Handle type="target" position={Position.Bottom} isConnectable={false} />
      <Handle type="source" position={Position.Top} isConnectable={false} />
      <span className="lin-node-label">{d.label}</span>
      {d.version ? <span className="lin-node-version">v{d.version}</span> : null}
    </div>
  );
}

const NODE_TYPES = { artifact: ArtifactNode };

/**
 * Rank the graph so the thing everything points at sits at the top.
 *
 * `BT` puts an edge's source below its target, and our edges run
 * dependent → source. So a document that three others were built on
 * ends up above them with three arrows converging on it, which is the
 * shape of the question being asked: "what would be affected if this
 * changed".
 */
function layout(nodes: Node[], edges: Edge[]): Node[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "BT", nodesep: 28, ranksep: 64, marginx: 12, marginy: 12 });

  nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }));
  edges.forEach((e) => g.setEdge(e.source, e.target));
  dagre.layout(g);

  return nodes.map((n) => {
    const placed = g.node(n.id);
    return {
      ...n,
      // dagre returns a centre; React Flow positions by top-left.
      position: { x: placed.x - NODE_W / 2, y: placed.y - NODE_H / 2 },
    };
  });
}

export function ArtifactLineageGraph({
  nodes,
  edges,
  onOpen,
  height = 320,
}: {
  nodes: LineageNode[];
  edges: LineageEdge[];
  onOpen: (id: string) => void;
  height?: number;
}) {
  const open = useCallback((id: string) => onOpen(id), [onOpen]);

  const flowNodes = useMemo<Node[]>(
    () =>
      nodes.map((n) => ({
        id: n.id,
        type: "artifact",
        position: { x: 0, y: 0 },
        draggable: true,
        data: {
          label: n.label,
          version: n.version,
          isSource: n.kind !== "artifact",
          // A node is stale when something pinned to an old version of
          // it — read off the edges rather than passed in, so the
          // badge cannot disagree with the edge beside it.
          stale: edges.some((e) => e.stale && e.target === n.id),
          onOpen: () => open(n.id),
        } satisfies ArtifactNodeData,
      })),
    [nodes, edges, open],
  );

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((e, i) => ({
        id: `${e.source}->${e.target}-${i}`,
        source: e.source,
        target: e.target,
        label: e.label || undefined,
        className: e.stale ? "lin-edge lin-edge-stale" : "lin-edge",
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
        animated: false,
      })),
    [edges],
  );

  const positioned = useMemo(
    () => layout(flowNodes, flowEdges),
    [flowNodes, flowEdges],
  );

  if (nodes.length === 0) return null;

  return (
    <div className="lin-canvas" style={{ height }}>
      <ReactFlow
        nodes={positioned}
        edges={flowEdges}
        nodeTypes={NODE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.18 }}
        // The canvas is a picture, not an editor. Panning and zooming
        // help on a large result; connecting nodes by hand would
        // assert a derivation that never happened.
        nodesConnectable={false}
        edgesFocusable={false}
        proOptions={{ hideAttribution: false }}
        minZoom={0.3}
        maxZoom={1.6}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
