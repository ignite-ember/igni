/**
 * WorkflowRun — live-progress card for a CC-style workflow run.
 *
 * Renders the typed :class:`WorkflowRunState` the FE reducer
 * builds from ``workflow_event`` pushes. Composition mirrors
 * the team-progress / tool-card chrome: bordered surface with a
 * 3px status-colored left accent, a single header row carrying
 * the run's identity + live counters, and a list of phase rows
 * whose agents render inline as compact pills.
 *
 * NOT a :class:`JsonRenderView` catalog entry — workflow UI is
 * trusted application chrome with timers, parallel lanes, and
 * cancellation, not agent-generated UI.
 */

import { useState } from "react";
import type {
  WorkflowAgentRun,
  WorkflowPhase,
  WorkflowRunState,
} from "../chat/model";

function fmtDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rem = s - m * 60;
  return `${m}m${rem.toFixed(0)}s`;
}

function elapsed(startedAtMs: number, endedAtMs?: number): string {
  const end = endedAtMs ?? Date.now();
  return fmtDuration(end - startedAtMs);
}

function statusTone(
  status: WorkflowRunState["status"] | WorkflowPhase["status"] | WorkflowAgentRun["status"],
): "running" | "good" | "warn" | "bad" {
  switch (status) {
    case "running":
      return "running";
    case "completed":
    case "good":
      return "good";
    case "cancelled":
      return "warn";
    case "failed":
    case "timeout":
      return "bad";
    default:
      return "running";
  }
}

function statusGlyph(s: string): string {
  switch (s) {
    case "running":
      return "●";
    case "completed":
      return "✓";
    case "failed":
      return "✗";
    case "cancelled":
      return "⊘";
    case "timeout":
      return "◷";
    default:
      return "·";
  }
}

export function WorkflowRun({ run }: { run: WorkflowRunState }) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const status = run.status;
  const totalAgents = run.phases.reduce((n, p) => n + p.agents.length, 0);
  const completedAgents = run.phases.reduce(
    (n, p) => n + p.agents.filter((a) => a.status !== "running").length,
    0,
  );
  const totalDuration =
    run.endedAtMs !== undefined ? run.endedAtMs - run.startedAtMs : undefined;
  const resultSummary = formatResultSummary(run.result);

  return (
    <div className="workflow-run" data-status={status}>
      <header className="workflow-run-header">
        <span className="workflow-run-glyph" aria-hidden>
          {statusGlyph(status)}
        </span>
        <div className="workflow-run-title">
          <span className="workflow-run-name">{run.name}</span>
          <span className={`workflow-run-pill tone-${statusTone(status)}`}>
            {status}
          </span>
        </div>
        <div className="workflow-run-meta">
          {totalAgents > 0 && (
            <span className="workflow-run-progress">
              {completedAgents} / {totalAgents}
            </span>
          )}
          {totalDuration !== undefined && (
            <span className="workflow-run-duration">
              {fmtDuration(totalDuration)}
            </span>
          )}
        </div>
      </header>

      {run.error !== undefined && (
        <div className="workflow-run-error">
          <strong>Error:</strong> {run.error}
        </div>
      )}

      <ol className="workflow-run-phases">
        {run.phases.map((phase, i) => (
          <PhaseRow
            key={phase.phaseId}
            phase={phase}
            index={i + 1}
            totalPhases={run.phases.length}
            collapsed={collapsed[phase.phaseId] ?? false}
            onToggle={() =>
              setCollapsed((c) => ({ ...c, [phase.phaseId]: !c[phase.phaseId] }))
            }
          />
        ))}
        {run.phases.length === 0 && status === "running" && (
          <li className="workflow-run-empty">starting…</li>
        )}
      </ol>

      {run.result !== undefined && (
        <details className="workflow-run-result">
          <summary>
            <span className="workflow-run-result-label">Result</span>
            {resultSummary !== null && (
              <span className="workflow-run-result-summary">
                {resultSummary}
              </span>
            )}
          </summary>
          <pre>{JSON.stringify(run.result, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

function formatResultSummary(result: unknown): string | null {
  if (result === null || result === undefined) return null;
  if (typeof result !== "object") return String(result);
  const r = result as Record<string, unknown>;
  const parts: string[] = [];
  if (typeof r.status === "string") parts.push(r.status);
  if (typeof r.chosen_design === "string")
    parts.push(`design: ${r.chosen_design}`);
  const v = r.verification;
  if (v && typeof v === "object") {
    const ver = v as Record<string, unknown>;
    if (typeof ver.tests_passed === "number") {
      const t = ver.tests_passed;
      const f = typeof ver.tests_failed === "number" ? ver.tests_failed : 0;
      parts.push(
        f === 0 ? `${t} tests passed` : `${t} passed, ${f} failed`,
      );
    }
  }
  if (parts.length === 0) return null;
  return parts.join(" · ");
}

function PhaseRow({
  phase,
  index,
  totalPhases,
  collapsed,
  onToggle,
}: {
  phase: WorkflowPhase;
  index: number;
  totalPhases: number;
  collapsed: boolean;
  onToggle: () => void;
}) {
  return (
    <li
      className="workflow-phase"
      data-status={phase.status}
      data-parallel={phase.agents.some((a) => a.parallelGroupId !== undefined)}
    >
      <button
        type="button"
        className="workflow-phase-header"
        onClick={onToggle}
        aria-expanded={!collapsed}
      >
        <span className="workflow-phase-caret" aria-hidden>
          {collapsed ? "▸" : "▾"}
        </span>
        <span className="workflow-phase-index">{index}/{totalPhases}</span>
        <span className="workflow-phase-glyph" aria-hidden>
          {statusGlyph(phase.status)}
        </span>
        <span className="workflow-phase-title">{phase.title}</span>
        <span className={`workflow-phase-pill tone-${statusTone(phase.status)}`}>
          {phase.status}
        </span>
        <span className="workflow-phase-elapsed">
          {elapsed(phase.startedAtMs, phase.endedAtMs)}
        </span>
      </button>
      {!collapsed && <AgentList agents={phase.agents} />}
    </li>
  );
}

function AgentList({ agents }: { agents: WorkflowAgentRun[] }) {
  if (agents.length === 0) {
    return <div className="workflow-phase-empty">no agents yet</div>;
  }
  // Group consecutive agents that share a ``parallelGroupId``
  // — those are the lanes of a single ``parallel()`` call. The
  // CSS grid renders the lanes side-by-side; the row gets
  // ``data-parallel-count`` so the cell widths tune to the lane
  // count (3 lanes = 3 equal columns; 2 = 2).
  const groups: Array<{
    kind: "serial" | "parallel";
    agents: WorkflowAgentRun[];
  }> = [];
  for (const agent of agents) {
    const last = groups[groups.length - 1];
    if (
      agent.parallelGroupId !== undefined &&
      last?.kind === "parallel" &&
      last.agents[0].parallelGroupId === agent.parallelGroupId
    ) {
      last.agents.push(agent);
    } else {
      groups.push({
        kind: agent.parallelGroupId !== undefined ? "parallel" : "serial",
        agents: [agent],
      });
    }
  }
  return (
    <ul className="workflow-phase-agents">
      {groups.map((group, gi) =>
        group.kind === "serial" ? (
          group.agents.map((a) => (
            <AgentRow key={a.agentId} agent={a} />
          ))
        ) : (
          <li
            key={`par-${gi}-${group.agents[0].parallelGroupId}`}
            className="workflow-parallel"
            data-parallel-count={group.agents.length}
          >
            <span className="workflow-parallel-marker">
              <span className="workflow-parallel-glyph" aria-hidden>⇉</span>
              {group.agents.length} parallel lanes
            </span>
            <ol className="workflow-parallel-lanes">
              {group.agents.map((a) => (
                <AgentRow key={a.agentId} agent={a} />
              ))}
            </ol>
          </li>
        ),
      )}
    </ul>
  );
}

function AgentRow({ agent }: { agent: WorkflowAgentRun }) {
  return (
    <li className="workflow-agent" data-status={agent.status}>
      <span className="workflow-agent-glyph" aria-hidden>
        {statusGlyph(agent.status)}
      </span>
      <span className="workflow-agent-label" title={agent.label}>
        {agent.label}
      </span>
      <span className={`workflow-agent-pill tone-${statusTone(agent.status)}`}>
        {agent.status}
      </span>
      <span className="workflow-agent-elapsed">
        {elapsed(agent.startedAtMs, agent.endedAtMs)}
      </span>
      {agent.error !== undefined && (
        <span className="workflow-agent-error" title={agent.error}>
          {agent.error}
        </span>
      )}
    </li>
  );
}
