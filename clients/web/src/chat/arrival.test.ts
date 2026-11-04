import { describe, expect, it } from "vitest";

import { Arrivals } from "./arrival";

describe("a link that opens a page", () => {
  it("names the page and hands it the rest as params", () => {
    expect(Arrivals.fromSearch("?open=igni.artifacts&slug=runbook&comment=c1")).toEqual({
      page: "igni.artifacts",
      params: { slug: "runbook", comment: "c1" },
    });
  });

  it("keeps igni's own parameters out of the page's", () => {
    expect(Arrivals.fromSearch("?dir=%2Fsrc%2Fapp&open=acme.notes&ws=ws%3A%2F%2Fx&note=n2")).toEqual({
      page: "acme.notes",
      params: { note: "n2" },
    });
  });

  it("is no arrival without `open`", () => {
    expect(Arrivals.fromSearch("?dir=%2Fsrc&slug=runbook")).toBeNull();
    expect(Arrivals.fromSearch("?open=")).toBeNull();
  });

  it("comes out of the address bar whole, leaving igni's own", () => {
    expect(Arrivals.withoutArrival("?dir=%2Fsrc&open=igni.artifacts&slug=runbook")).toBe(
      "?dir=%2Fsrc",
    );
    expect(Arrivals.withoutArrival("?open=igni.artifacts&slug=runbook")).toBe("");
    expect(Arrivals.withoutArrival("?dir=%2Fsrc")).toBe("?dir=%2Fsrc");
  });
});
