/**
 * WorkflowRun — live-progress card for a CC-style workflow run.
 *
 * Renders the typed :class:`WorkflowRunState` the FE reducer
 * builds from ``workflow_event`` pushes. Each phase is a
 * collapsible card with the agents inside; agents get a status
 * pill + elapsed-time label.
 *
 * Layout:
 *   <WorkflowRun>
 *     <status banner — running | completed | failed | cancelled>
 *     <phase card> Assess
 *       <agent card> assess — running
 *       <agent card> assess — completed (2.3s)
 *     </phase card>
 *     <phase card> Design
 *       <agent card> design:oop-minimal
 *       <agent card> design:oop-architectural
 *       <agent card> design:oop-consistency
 *     </phase card>
 *     ...
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

  return (
    <div className="workflow-run" data-status={status}>
      <header className="workflow-run-header">
        <div className="workflow-run-title">
          <span className="workflow-run-icon" aria-hidden>⚙</span>
          <span className="workflow-run-name">{run.name}</span>
          <span className={`workflow-run-pill tone-${statusTone(status)}`}>
            {status}
          </span>
        </div>
        <div className="workflow-run-meta">
          <span className="workflow-run-id">{run.workflowRunId}</span>
          {totalAgents > 0 && (
            <span className="workflow-run-progress">
              {completedAgents} / {totalAgents} agents
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
        {run.phases.map((phase) => (
          <PhaseCard
            key={phase.phaseId}
            phase={phase}
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
          <summary>Result</summary>
          <pre>{JSON.stringify(run.result, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

function PhaseCard({
  phase,
  collapsed,
  onToggle,
}: {
  phase: WorkflowPhase;
  collapsed: boolean;
  onToggle: () => void;
}) {
  return (
    <li className="workflow-phase" data-status={phase.status}>
      <button
        type="button"
        className="workflow-phase-header"
        onClick={onToggle}
        aria-expanded={!collapsed}
      >
        <span className="workflow-phase-caret" aria-hidden>
          {collapsed ? "▸" : "▾"}
        </span>
        <span className="workflow-phase-title">{phase.title}</span>
        <span className={`workflow-phase-pill tone-${statusTone(phase.status)}`}>
          {phase.status}
        </span>
        <span className="workflow-phase-elapsed">
          {elapsed(phase.startedAtMs, phase.endedAtMs)}
        </span>
      </button>
      {!collapsed && (
        <ul className="workflow-phase-agents">
          {phase.agents.map((agent) => (
            <AgentCard key={agent.agentId} agent={agent} />
          ))}
          {phase.agents.length === 0 && (
            <li className="workflow-phase-empty">no agents yet</li>
          )}
        </ul>
      )}
    </li>
  );
}

function AgentCard({ agent }: { agent: WorkflowAgentRun }) {
  return (
    <li className="workflow-agent" data-status={agent.status}>
      <div className="workflow-agent-row">
        <span className="workflow-agent-label">{agent.label}</span>
        <span className={`workflow-agent-pill tone-${statusTone(agent.status)}`}>
          {agent.status}
        </span>
        <span className="workflow-agent-elapsed">
          {elapsed(agent.startedAtMs, agent.endedAtMs)}
        </span>
      </div>
      {agent.error !== undefined && (
        <div className="workflow-agent-error">{agent.error}</div>
      )}
      {agent.result !== undefined && agent.status !== "running" && (
        <details className="workflow-agent-result">
          <summary>Result</summary>
          <pre>{JSON.stringify(agent.result, null, 2)}</pre>
        </details>
      )}
    </li>
  );
}
