import { useCallback, useEffect, useState } from "react";
import type { EmberClient } from "../protocol/client";

export interface GroupAgentConflict {
  entry_name: string;
  /** What it is: agents, skills, commands, rules, output-styles, workflows. */
  entry_kind: string;
  /** What happened on the server: changed | removed. */
  kind: string;
  question: string;
}

export interface GroupPolicy {
  group_id: string | null;
  group_name: string | null;
  fetched_at: string | null;
  entry_count: number;
  default_model: string | null;
  pending_conflicts: GroupAgentConflict[];
}

type Tone = "muted" | "good" | "warn";

export interface GroupBadge {
  label: string;
  tone: Tone;
  detail: string;
  /** Whether clicking should open anything. */
  actionable: boolean;
}

/**
 * What the footer pill says.
 *
 * Somebody whose agents changed under them should be able to see why
 * without asking an admin, so the group's name is the label. A pending
 * question outranks it: that one is waiting on the person, and a count
 * they can act on beats a name they cannot.
 */
export function classify(policy: GroupPolicy | null): GroupBadge {
  if (!policy || !policy.group_name) {
    return {
      label: "none",
      tone: "muted",
      detail: "You are not in a group — agents come from this machine",
      actionable: false,
    };
  }

  const pending = policy.pending_conflicts.length;
  if (pending > 0) {
    return {
      label: `${pending} to review`,
      tone: "warn",
      detail:
        pending === 1
          ? policy.pending_conflicts[0].question
          : `${pending} things ${policy.group_name} ships changed where you have local edits`,
      actionable: true,
    };
  }

  const parts = [`${policy.entry_count} entries`];
  if (policy.default_model) parts.push(policy.default_model);
  return {
    label: policy.group_name,
    tone: "good",
    detail: parts.join(" · "),
    actionable: false,
  };
}

/**
 * The group this person is in, and anything it is waiting on.
 *
 * Their agents are the org's now, which is only reasonable if they can
 * see whose they are — and if an admin's edit landing on top of theirs
 * is a question rather than a surprise.
 */
export function GroupIndicator({ client }: { client: EmberClient }) {
  const [policy, setPolicy] = useState<GroupPolicy | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setPolicy(await client.rpc<GroupPolicy>("get_group_policy"));
    } catch {
      setPolicy(null);
    }
  }, [client]);

  useEffect(() => {
    void refresh();
    // The pack is refetched on a five-minute TTL, so a slower poll than
    // the index pill's; nothing here changes second to second.
    const interval = setInterval(() => void refresh(), 60_000);
    return () => clearInterval(interval);
  }, [refresh]);

  const resolve = async (conflict: GroupAgentConflict, acceptIncoming: boolean) => {
    const id = `${conflict.entry_kind}/${conflict.entry_name}`;
    setBusy(id);
    try {
      await client.rpc("resolve_group_agent_conflict", {
        entry_name: conflict.entry_name,
        entry_kind: conflict.entry_kind,
        accept_incoming: acceptIncoming,
      });
      await refresh();
    } finally {
      setBusy(null);
    }
  };

  const badge = classify(policy);
  const conflicts = policy?.pending_conflicts ?? [];

  return (
    <div className="group-pill-wrap">
      <button
        className={`codeindex-pill tone-${badge.tone}`}
        title={badge.detail}
        onClick={() => badge.actionable && setOpen((v) => !v)}
        aria-expanded={badge.actionable ? open : undefined}
      >
        <span className="codeindex-dot" />
        Group <span className="codeindex-label">{badge.label}</span>
      </button>

      {open && conflicts.length > 0 && (
        <div className="group-conflicts" role="dialog" aria-label="Agent changes to review">
          {conflicts.map((conflict) => {
            const id = `${conflict.entry_kind}/${conflict.entry_name}`;
            return (
              <div className="group-conflict" key={id}>
                <div className="group-conflict-question">{conflict.question}</div>
                <div className="group-conflict-note">
                  {conflict.kind === "removed"
                    ? "Keeping yours leaves it in place, and your group will not ship it again."
                    : "Taking theirs replaces your copy — your edits to this one are lost."}
                </div>
                <div className="group-conflict-actions">
                  <button disabled={busy === id} onClick={() => void resolve(conflict, true)}>
                    {conflict.kind === "removed" ? "Remove it" : "Take the group's"}
                  </button>
                  <button disabled={busy === id} onClick={() => void resolve(conflict, false)}>
                    Keep mine
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
