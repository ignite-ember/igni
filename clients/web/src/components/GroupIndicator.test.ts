/**
 * Tests for ``classify`` — what the group pill says.
 *
 * Somebody whose agents changed under them should be able to see whose
 * they are without asking an admin, and a question waiting on *them*
 * should outrank the name. That priority is the whole of the pill's
 * user-facing behaviour, so it is what is tested here; the polling
 * effect and the popover are not, in line with the CodeIndex pill.
 */

import { describe, expect, it } from "vitest";
import { classify, type GroupPolicy } from "./GroupIndicator";

function policy(over: Partial<GroupPolicy> = {}): GroupPolicy {
  return {
    group_id: "g-1",
    group_name: "Legal",
    fetched_at: "2026-08-30T00:00:00Z",
    entry_count: 4,
    default_model: null,
    pending_conflicts: [],
    ...over,
  };
}

describe("classify", () => {
  it("names the group when there is nothing to answer", () => {
    expect(classify(policy()).label).toBe("Legal");
  });

  it("says so when there is no group", () => {
    const badge = classify(policy({ group_name: null }));
    expect(badge.label).toBe("none");
    expect(badge.detail).toContain("this machine");
  });

  it("treats an unreachable backend as no group rather than an error", () => {
    expect(classify(null).label).toBe("none");
  });

  it("a pending question outranks the name", () => {
    // It is waiting on the person; the name is not.
    const badge = classify(
      policy({
        pending_conflicts: [
          { entry_name: "reviewer", entry_kind: "agents", kind: "changed", question: "reviewer: take theirs?" },
        ],
      }),
    );
    expect(badge.label).toBe("1 to review");
    expect(badge.tone).toBe("warn");
  });

  it("shows the one question verbatim when there is only one", () => {
    const badge = classify(
      policy({
        pending_conflicts: [
          { entry_name: "reviewer", entry_kind: "agents", kind: "changed", question: "reviewer: take theirs?" },
        ],
      }),
    );
    expect(badge.detail).toBe("reviewer: take theirs?");
  });

  it("counts them when there is more than one", () => {
    const badge = classify(
      policy({
        pending_conflicts: [
          { entry_name: "a", entry_kind: "agents", kind: "changed", question: "a?" },
          { entry_name: "b", entry_kind: "agents", kind: "removed", question: "b?" },
        ],
      }),
    );
    expect(badge.label).toBe("2 to review");
    expect(badge.detail).toContain("Legal");
  });

  it("is only clickable when there is something to click", () => {
    expect(classify(policy()).actionable).toBe(false);
    expect(
      classify(policy({ pending_conflicts: [{ entry_name: "a", entry_kind: "agents", kind: "changed", question: "a?" }] }))
        .actionable,
    ).toBe(true);
  });

  it("puts the entry count and the model in the tooltip", () => {
    // A group is exactly what its members get, so the count is the
    // whole story rather than one half of a merge.
    const badge = classify(policy({ default_model: "legal-reviewer" }));
    expect(badge.detail).toContain("4 entries");
    expect(badge.detail).toContain("legal-reviewer");
  });
});
