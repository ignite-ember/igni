import { useCallback, useEffect, useState } from "react";
import type { IgniClient } from "../protocol/client";

export interface GroupPolicy {
  group_id: string | null;
  group_name: string | null;
  fetched_at: string | null;
  entry_count: number;
  default_model: string | null;
}

type Tone = "muted" | "good";

export interface GroupBadge {
  label: string;
  tone: Tone;
  detail: string;
}

/**
 * What the footer pill says.
 *
 * Somebody whose agents come from their organisation should be able to
 * see whose they are without asking an admin, so the group's name is the
 * label.
 *
 * There is nothing to act on here any more. The pill used to show a
 * count of pending conflicts — the group had changed something under a
 * local edit and was waiting on "yours or theirs" — and clicking opened
 * a panel to answer them. Group entries are now read from the policy
 * cache and a same-named project file simply outranks them, so nothing
 * is copied into the project and nothing can diverge. No copy, no
 * conflict, no question.
 */
export function classify(policy: GroupPolicy | null): GroupBadge {
  if (!policy || !policy.group_name) {
    return {
      label: "none",
      tone: "muted",
      detail: "You are not in a group — agents come from this machine",
    };
  }

  const parts = [`${policy.entry_count} entries`];
  if (policy.default_model) parts.push(policy.default_model);
  return {
    label: policy.group_name,
    tone: "good",
    detail: parts.join(" · "),
  };
}

/** The group this person is in. */
export function GroupIndicator({ client }: { client: IgniClient }) {
  const [policy, setPolicy] = useState<GroupPolicy | null>(null);

  const refresh = useCallback(async () => {
    try {
      setPolicy(await client.rpc<GroupPolicy>("get_group_policy"));
    } catch {
      setPolicy(null);
    }
  }, [client]);

  useEffect(() => {
    void refresh();
    // The pack is revalidated when a dialogue starts and on a
    // five-minute timer, so a slower poll than the index pill's; nothing
    // here changes second to second.
    const interval = setInterval(() => void refresh(), 60_000);
    return () => clearInterval(interval);
  }, [refresh]);

  const badge = classify(policy);

  return (
    <div className="group-pill-wrap">
      <button className={`codeindex-pill tone-${badge.tone}`} title={badge.detail} disabled>
        <span className="codeindex-dot" />
        Group <span className="codeindex-label">{badge.label}</span>
      </button>
    </div>
  );
}
