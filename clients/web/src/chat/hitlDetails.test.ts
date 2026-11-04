import { describe, expect, it } from "vitest";

import type { HITLRequest } from "../protocol/messages";
import { withDetails } from "./hitlDetails";

const req = (id: string, details: string): HITLRequest =>
  ({ type: "hitl_request", requirement_id: id, tool_name: "bash", details }) as HITLRequest;

describe("withDetails", () => {
  it("fills in the matching requirement only and stops its spinner", () => {
    const open = [{ ...req("a", ""), details_pending: true }, req("b", "other")];
    const next = withDetails(open, { requirement_id: "a", details: "Overwrites main." });
    expect(next?.map((r) => r.details)).toEqual(["Overwrites main.", "other"]);
    expect(next?.[0].details_pending).toBe(false);
  });

  it("leaves the list alone for a requirement that is no longer open", () => {
    const open = [req("a", "stock")];
    expect(withDetails(open, { requirement_id: "gone", details: "x" })).toBe(open);
    expect(withDetails(null, { requirement_id: "a", details: "x" })).toBeNull();
  });
});
