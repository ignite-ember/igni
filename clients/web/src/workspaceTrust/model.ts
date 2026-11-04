/**
 * Workspace trust — "Trust this folder?" for the desktop and web.
 *
 * Opening a folder never runs its code until the person trusts it
 * (`core/workspace_trust` in the backend): its tools, hooks, workflows,
 * language servers and its permission/safety settings wait. There is no
 * terminal here to prompt on, so the window asks: it reads the folder's
 * decision with `get_workspace_trust` when it connects and whenever the
 * folder changes, and opens igni's own dialog (`ui.dialog`, kind
 * `workspace_trust`) while the state is `ask`. The answer goes back with
 * `set_workspace_trust`; the backend pushes `workspace_trust_required`
 * with the new status, so every window showing the dialog for that folder
 * closes it. Until someone answers, the folder runs untrusted.
 */

import { useEffect, useRef } from "react";

import type { Dialog, WindowModel } from "../clientState";
import type { IgniClient } from "../protocol/client";

export const TRUST_CHANNEL = "workspace_trust_required";
export const TRUST_DIALOG_KIND = "workspace_trust";

export type TrustAnswer = "trust" | "distrust" | "once";

/** `get_workspace_trust`'s answer and the push's payload. */
export interface WorkspaceTrustStatus {
  state: "trusted" | "untrusted" | "ask";
  can_trust: boolean;
  reason: string;
  path: string;
  remote: string | null;
  policy_mode: string;
  gated: string;
  source?: string;
}

/** Checked, not cast: this decides whether a folder may run code. */
export function isTrustStatus(value: unknown): value is WorkspaceTrustStatus {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    (v.state === "trusted" || v.state === "untrusted" || v.state === "ask") &&
    typeof v.can_trust === "boolean" &&
    typeof v.path === "string"
  );
}

/** The status `workspace_trust_required` carries, or `null` for anything else. */
export function trustFromPush(message: unknown): WorkspaceTrustStatus | null {
  const push = message as { channel?: unknown; payload?: unknown };
  if (push?.channel !== TRUST_CHANNEL) return null;
  return isTrustStatus(push.payload) ? push.payload : null;
}

/** Whether the open dialog is the trust dialog for `path`. */
export function isTrustDialogFor(dialog: Dialog | null, path: string): boolean {
  return dialog?.spec.kind === TRUST_DIALOG_KIND && dialog.spec.path === path;
}

/**
 * Open the trust dialog while the folder's state is `ask` (and once, to say
 * why, when the organisation's policy forbids trusting it); close it when
 * someone — here or in another window — has answered.
 */
export function useWorkspaceTrust(
  client: IgniClient,
  connected: boolean,
  projectDir: string | null | undefined,
  windowModel: WindowModel,
  dialog: Dialog | null,
): void {
  // The latest dialog, for the push handler registered once per connection.
  const dialogRef = useRef<Dialog | null>(dialog);
  dialogRef.current = dialog;
  // Folders whose policy refusal this window has already explained: said
  // once, not on every reconnect.
  const explained = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!connected) return;
    let alive = true;

    const apply = (status: WorkspaceTrustStatus) => {
      const open = dialogRef.current;
      const refused =
        status.state === "untrusted" && !status.can_trust && !explained.current.has(status.path);
      if (status.state === "ask" || refused) {
        if (refused) explained.current.add(status.path);
        if (open?.spec.kind !== TRUST_DIALOG_KIND) {
          windowModel.openDialog({ kind: TRUST_DIALOG_KIND, ...status });
        }
      } else if (open && isTrustDialogFor(open, status.path)) {
        windowModel.answerDialog(open.id, null);
      }
    };

    void client
      .rpc<unknown>("get_workspace_trust")
      .then((answer) => {
        if (alive && isTrustStatus(answer)) apply(answer);
      })
      .catch(() => {
        // A backend without the RPC (older) or a failure: the folder stays
        // untrusted on the backend side, which is the safe direction.
      });
    const off = client.onEvent((message) => {
      const status = trustFromPush(message);
      if (status) apply(status);
    });
    return () => {
      alive = false;
      off();
    };
  }, [client, connected, projectDir, windowModel]);
}
