/**
 * Tests for ``classify`` — what the group pill says.
 *
 * Somebody whose agents come from their organisation should be able to
 * see whose they are without asking an admin. That is now the whole of
 * the pill's user-facing behaviour, so it is what is tested here; the
 * polling effect is not, in line with the CodeIndex pill.
 *
 * Five of these used to cover a pending-conflict state: the group had
 * changed something under a local edit and the pill showed a count
 * waiting on the person, outranking the group's name. Group entries are
 * now read from the policy cache and a same-named project file simply
 * outranks them — nothing is copied into the project, so nothing can
 * diverge and there is no question to answer. Those tests are gone
 * rather than skipped, because the state they described cannot occur.
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
    ...over,
  };
}

describe("classify", () => {
  it("names the group", () => {
    const badge = classify(policy());
    expect(badge.label).toBe("Legal");
    expect(badge.tone).toBe("good");
  });

  it("says so when there is no group", () => {
    const badge = classify(policy({ group_name: null }));
    expect(badge.label).toBe("none");
    expect(badge.detail).toContain("this machine");
  });

  it("treats an unreachable backend as no group rather than an error", () => {
    expect(classify(null).label).toBe("none");
  });

  it("puts the entry count and the model in the tooltip", () => {
    // A group is exactly what its members get, so the count is the
    // whole story rather than one half of a merge.
    const badge = classify(policy({ default_model: "legal-reviewer" }));
    expect(badge.detail).toContain("4 entries");
    expect(badge.detail).toContain("legal-reviewer");
  });

  it("omits the model when the group names none", () => {
    expect(classify(policy()).detail).toBe("4 entries");
  });
});
