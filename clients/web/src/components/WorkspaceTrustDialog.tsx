/**
 * "Trust this folder?" — igni's own dialog (`ui.dialog`, kind
 * `workspace_trust`). See `workspaceTrust/model.ts`.
 *
 * Trust / Don't trust / This time only. When the organisation's policy
 * does not allow this folder to be trusted, it says why and offers only
 * OK. Dismissing it (Escape, the backdrop, OK) answers nothing: the folder
 * keeps running untrusted, and the dialog comes back next time.
 */

import { useEffect, useState } from "react";

import type { IgniClient } from "../protocol/client";
import type { TrustAnswer, WorkspaceTrustStatus } from "../workspaceTrust/model";

export function WorkspaceTrustDialog({
  client,
  status,
  onClose,
}: {
  client: Pick<IgniClient, "rpc">;
  status: WorkspaceTrustStatus;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || busy) return;
      e.stopPropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose, busy]);

  const answer = async (decision: TrustAnswer) => {
    setBusy(true);
    setError(null);
    try {
      await client.rpc("set_workspace_trust", { decision });
      onClose();
    } catch (e) {
      setBusy(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div
      className="update-prompt-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        className="update-prompt"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-trust-title"
      >
        <div className="update-prompt-title" id="workspace-trust-title">
          Trust this folder?
        </div>
        <div className="update-prompt-body">
          It can run code: tools, hooks, workflows, language servers… Until
          you trust it, igni leaves off its {status.gated}.
        </div>
        <div className="update-prompt-body">
          <div>
            Folder: <code>{status.path}</code>
          </div>
          <div>
            Git remote: <code>{status.remote ?? "none"}</code>
          </div>
        </div>
        {!status.can_trust && (
          <div className="update-prompt-body" role="note">
            {status.reason}
          </div>
        )}
        {error && <div className="update-prompt-error">{error}</div>}
        <div className="update-prompt-actions">
          {status.can_trust ? (
            <>
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={() => void answer("distrust")}
              >
                Don't trust
              </button>
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={() => void answer("once")}
              >
                This time only
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy}
                onClick={() => void answer("trust")}
              >
                Trust
              </button>
            </>
          ) : (
            <button type="button" className="btn btn-primary" onClick={onClose}>
              OK
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
