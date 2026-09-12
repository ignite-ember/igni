import { useCallback, useEffect, useState } from "react";

import { Drawer } from "../panels/Drawer";
import type { EmberClient } from "../../protocol/client";

export interface WorkflowPhase {
  title?: string;
  detail?: string;
  model?: string;
}

export interface WorkflowMeta {
  name?: string;
  description?: string;
  whenToUse?: string;
  phases?: WorkflowPhase[];
  path?: string;
}

/**
 * The workflows this project defines.
 *
 * `/workflows` with no name used to append its listing to the
 * transcript. That is how browsing what exists destroyed the chat's
 * start screen: the welcome only renders while `items.length === 0`,
 * so one listing replaced it permanently — and the start screen's own
 * card was the only place `/workflows` was advertised, so the
 * suggestion consumed the screen that made it.
 *
 * A listing is a place, not a message. Running a workflow still
 * streams into the chat, because a run *is* transcript: it has a
 * beginning, output and an end, and belongs in the conversation that
 * asked for it.
 */
export function WorkflowsPage({
  client,
  onRun,
  onClose,
}: {
  client: EmberClient;
  /** Seeds the composer with `/workflows <name>` rather than firing
   *  it. Several take a JSON argument object, and the phase list is
   *  what tells you whether you want this one at all. */
  onRun: (command: string) => void;
  onClose: () => void;
}) {
  const [workflows, setWorkflows] = useState<WorkflowMeta[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setWorkflows((await client.rpc<WorkflowMeta[]>("list_workflows")) ?? []);
    } catch (e) {
      setError(String(e));
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Drawer title="Workflows" onClose={onClose}>
      {error && <div className="ctx-error">Could not list workflows: {error}</div>}

      {workflows === null && !error && <div className="ctx-empty">Reading workflows…</div>}

      {workflows?.length === 0 && (
        <div className="codeindex-empty">
          <p className="codeindex-empty-lead">
            No workflows in <code>.claude/workflows/</code>. A workflow is a script that
            drives several agents through named phases — useful when one prompt would have
            to do too many things at once.
          </p>
        </div>
      )}

      {workflows?.map((wf) => (
        <button
          className="workflow-card"
          key={wf.path || wf.name}
          onClick={() => onRun(`/workflows ${wf.name ?? ""}`)}
        >
          <span className="workflow-head">
            <code className="workflow-name">{wf.name ?? "(unnamed)"}</code>
            <span className="workflow-phases">
              {(wf.phases ?? []).length} {(wf.phases ?? []).length === 1 ? "phase" : "phases"}
            </span>
          </span>
          {wf.description && <span className="workflow-desc">{wf.description}</span>}
          {wf.whenToUse && <span className="workflow-when">{wf.whenToUse}</span>}
          {(wf.phases ?? []).length > 0 && (
            <span className="workflow-phase-list">
              {(wf.phases ?? []).map((p, i) => (
                <span className="workflow-phase" key={`${p.title ?? i}-${i}`}>
                  {p.title ?? `Phase ${i + 1}`}
                </span>
              ))}
            </span>
          )}
        </button>
      ))}
    </Drawer>
  );
}
