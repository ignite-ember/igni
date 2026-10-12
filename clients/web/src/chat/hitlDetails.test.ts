import { describe, expect, it } from "vitest";

import type { HITLRequest } from "../protocol/messages";
import { withDetails } from "./hitlDetails";

const req = (id: string, details: string): HITLRequest =>
  ({ type: "hitl_request", requirement_id: id, tool_name: "bash", details }) as HITLRequest;

describe("withDetails", () => {
  it("replaces the reason of the matching requirement only", () => {
    const open = [req("a", "stock"), req("b", "other")];
    const next = withDetails(open, { requirement_id: "a", details: "Overwrites main." });
    expect(next?.map((r) => r.details)).toEqual(["Overwrites main.", "other"]);
  });

  it("leaves the list alone for a requirement that is no longer open", () => {
    const open = [req("a", "stock")];
    expect(withDetails(open, { requirement_id: "gone", details: "x" })).toBe(open);
    expect(withDetails(null, { requirement_id: "a", details: "x" })).toBeNull();
  });
});
