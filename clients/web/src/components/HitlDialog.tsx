import { useState } from "react";
import type { HITLRequest } from "../protocol/messages";
import { HitlArgsView } from "./HitlArgsView";

export interface HitlDecision {
  requirement_id: string;
  action: "confirm" | "reject";
  choice: string;
}

/** Tools that mutate files — the set ``acceptEdits`` mode auto-allows.
 *  When the dialog is asking about one of these AND the session is
 *  in default mode, we show an inline shortcut to enable acceptEdits
 *  for the rest of the session. Names include the friendly catalog
 *  names (``Edit``, ``Write``, ``NotebookEdit``) AND the internal
 *  Agno function names (``edit_file`` etc.) because the BE may
 *  surface either depending on call site. Mirrors
 *  ``FILE_EDIT_TOOLS`` in ``src/ember_code/core/config/permission_eval.py``. */
const EDIT_TOOL_NAMES: ReadonlySet<string> = new Set([
  "Edit",
  "Write",
  "NotebookEdit",
  "edit_file",
  "edit_file_replace_all",
  "save_file",
  "create_file",
]);

/**
 * Permission dialog — steps through each pending requirement and
 * collects all decisions, then submits the batch in one round-trip
 * (mirrors the TUI's HITLResponseBatch flow).
 *
 * When the agent asks about a file-edit tool and the session is in
 * default mode, an extra full-width button appears in its own row:
 * *"Accept all edits during this session"*. Clicking it confirms
 * every remaining requirement in the batch AND invokes
 * ``onAcceptEditsThisRun`` so the parent can flip into acceptEdits
 * for the duration of the current run (the parent auto-reverts
 * when the agent finishes — see ``App.tsx``). Hidden when the
 * user is already in ``acceptEdits`` / ``bypassPermissions`` /
 * ``plan`` or when the tool isn't a file-edit tool.
 */
export function HitlDialog({
  requirements,
  onResolve,
  currentMode = "",
  onAcceptEditsThisRun,
}: {
  requirements: HITLRequest[];
  onResolve: (decisions: HitlDecision[]) => void;
  /** Active permission mode from the live ``StatusUpdate``. Used to
   *  gate the acceptEdits shortcut button — only shown when
   *  ``"default"`` (or empty for back-compat with older statuses). */
  currentMode?: string;
  /** Flip into acceptEdits mode for the rest of the current agent
   *  run. Parent auto-reverts on ``StreamingDone`` so the
   *  permission posture doesn't quietly persist across turns —
   *  matches the user's mental model of "accept edits FOR THIS
   *  TASK", not "permanently lower the gate". */
  onAcceptEditsThisRun?: () => void;
}) {
  const [index, setIndex] = useState(0);
  const [decisions, setDecisions] = useState<HitlDecision[]>([]);
  const req = requirements[index];
  if (!req) return null;

  const decide = (action: "confirm" | "reject", choice: string) => {
    const next = [...decisions, { requirement_id: req.requirement_id, action, choice }];
    if (index + 1 < requirements.length) {
      setDecisions(next);
      setIndex(index + 1);
    } else {
      onResolve(next);
    }
  };

  const isEditTool = EDIT_TOOL_NAMES.has(req.tool_name);
  // ``""`` covers older BE messages that didn't ship a mode; we
  // optimistically treat that as default so the shortcut appears.
  const isDefaultMode = currentMode === "" || currentMode === "default";
  const showAcceptEditsShortcut =
    isEditTool && isDefaultMode && onAcceptEditsThisRun !== undefined;

  const approveAllEditsInBatch = () => {
    // Confirm THIS req plus every remaining one in the batch with
    // ``choice="once"``. Mode flip handles the rest of the current
    // run; the parent flips it back off when the agent finishes
    // so the gate doesn't quietly persist past the task.
    const rest = requirements.slice(index).map((r) => ({
      requirement_id: r.requirement_id,
      action: "confirm" as const,
      choice: "once",
    }));
    onAcceptEditsThisRun?.();
    onResolve([...decisions, ...rest]);
  };

  return (
    <div className="hitl-inline">
      <div className="hitl-card">
        <div className="dialog-title">
          <span className="tool-status running" />
          Allow {req.friendly_name || req.tool_name}?
          {requirements.length > 1 && (
            <span style={{ color: "var(--fg-faint)", fontWeight: 400 }}>
              {index + 1}/{requirements.length}
            </span>
          )}
        </div>
        {req.agent_path && <div className="dialog-sub">{req.agent_path}</div>}
        {req.details && <div className="dialog-sub">{req.details}</div>}
        <HitlArgsView args={req.tool_args as Record<string, unknown> | undefined} />
        {/* Action rows are indented to align with col-2 of the
            ``.hitl-args`` grid above — that's where the args' value
            "rectangles" (file pill, diff old/new, command block)
            start. The grid template is
            ``minmax(72px, max-content) 1fr`` with a 14px gap, so
            col-2 lands at ~86px from the card's left edge. Buttons
            then sit visually beneath the values they're acting on,
            not floating against the card's left wall. */}
        {showAcceptEditsShortcut ? (
          <div
            style={{
              display: "inline-flex",
              flexDirection: "column",
              gap: 8,
              paddingLeft: 86,
            }}
          >
            <button
              className="btn btn-primary"
              onClick={approveAllEditsInBatch}
              title="Confirm this edit and auto-approve any further edits the agent makes for the rest of this task. Reverts to default when the agent finishes."
            >
              Accept all edits during this session
            </button>
            <div className="dialog-actions">
              <button className="btn" onClick={() => decide("confirm", "once")}>
                Allow once
              </button>
              <button className="btn" onClick={() => decide("confirm", "always")}>
                Always allow
              </button>
              <button className="btn" onClick={() => decide("confirm", "similar")}>
                Allow similar
              </button>
              <button className="btn btn-danger" onClick={() => decide("reject", "")}>
                Reject
              </button>
            </div>
          </div>
        ) : (
          <div className="dialog-actions" style={{ paddingLeft: 86 }}>
            <button className="btn btn-primary" onClick={() => decide("confirm", "once")}>
              Allow once
            </button>
            <button className="btn" onClick={() => decide("confirm", "always")}>
              Always allow
            </button>
            <button className="btn" onClick={() => decide("confirm", "similar")}>
              Allow similar
            </button>
            <button className="btn btn-danger" onClick={() => decide("reject", "")}>
              Reject
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
