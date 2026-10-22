/**
 * Choosing who can open a document.
 *
 * The ordering rule is the one worth reading twice. The chosen come
 * first, so the audience is visible as a list rather than as a number
 * — but they are narrowed by the search along with everybody else,
 * because a picker that pinned every ticked name to the top would
 * answer a search for Sam with Dana. Nothing is lost to that: the
 * count underneath always says how many are in the audience.
 */
import { describe, expect, it } from "vitest";

import {
  isChosen,
  offerableAddress,
  ordered,
  toggled,
  type Colleague,
} from "./PeoplePicker";

const ana: Colleague = { user_id: "u1", email: "ana@acme.co", name: "Ana Diaz" };
const sam: Colleague = { user_id: "u2", email: "sam@acme.co", name: "Sam Ortega" };
const dana: Colleague = { user_id: "u3", email: "dana@acme.co", name: "Dana Reyes" };

describe("who the list shows, and in what order", () => {
  it("shows the directory when nobody is chosen", () => {
    expect(ordered([ana, sam], [], "").map((p) => p.email)).toEqual([
      "ana@acme.co",
      "sam@acme.co",
    ]);
  });

  it("puts the chosen first", () => {
    expect(ordered([ana, sam, dana], ["sam@acme.co"], "").map((p) => p.email)).toEqual([
      "sam@acme.co",
      "ana@acme.co",
      "dana@acme.co",
    ]);
  });

  it("shows nobody twice", () => {
    const shown = ordered([ana, sam], ["sam@acme.co"], "");

    expect(shown.filter((p) => p.email === "sam@acme.co")).toHaveLength(1);
  });

  it("shows exactly the search result while a search is running", () => {
    // One matching rule, and it is the server's. Filtering the chosen
    // here as well meant two: the server matches a prefix, the client
    // matched anywhere inside, so "am" showed nothing from the
    // directory and kept a ticked "sam@acme.co" on screen.
    const shown = ordered([dana], ["sam@acme.co"], "dan");

    expect(shown.map((p) => p.email)).toEqual(["dana@acme.co"]);
  });

  it("still ticks a chosen name that the search found", () => {
    // The search result is the list; being in the audience is a
    // property of a row rather than a reason to add one.
    const shown = ordered([sam], ["sam@acme.co"], "sam");

    expect(shown.map((p) => p.email)).toEqual(["sam@acme.co"]);
    expect(isChosen(["sam@acme.co"], shown[0].email)).toBe(true);
  });

  it("shows an address with no account behind it", () => {
    // An owner may share with a colleague before that colleague has
    // signed up, and a picker that hid them would draw an audience
    // missing the people it names.
    const shown = ordered([], ["newhire@acme.co"], "");

    expect(shown.map((p) => p.email)).toEqual(["newhire@acme.co"]);
    expect(shown[0].user_id).toBe("");
  });
});

describe("ticking somebody", () => {
  it("adds them", () => {
    expect(toggled([], "ana@acme.co")).toEqual(["ana@acme.co"]);
  });

  it("removes them again", () => {
    expect(toggled(["ana@acme.co", "sam@acme.co"], "ana@acme.co")).toEqual([
      "sam@acme.co",
    ]);
  });

  it("leaves everybody else where they were", () => {
    // Order is what the reader built. Rebuilding the list on every
    // tick would shuffle it under them.
    expect(toggled(["a@x.co", "b@x.co", "c@x.co"], "b@x.co")).toEqual([
      "a@x.co",
      "c@x.co",
    ]);
  });

  it("matches regardless of case", () => {
    // "Ana@acme.co" and "ana@acme.co" are one person, and the access
    // rule already case-folds — a picker that disagreed would add
    // somebody twice and show them ticked once.
    expect(toggled(["ana@acme.co"], "Ana@acme.co")).toEqual([]);
    expect(isChosen(["ana@acme.co"], "ANA@ACME.CO")).toBe(true);
  });

  it("says who is not in it", () => {
    expect(isChosen(["ana@acme.co"], "sam@acme.co")).toBe(false);
  });
});

describe("sharing with somebody outside the directory", () => {
  it("offers a whole address that matches nobody", () => {
    expect(offerableAddress("rita@contractor.io", [ana])).toBe("rita@contractor.io");
  });

  it("offers nothing for a half-typed name", () => {
    // Or the row appears under every keystroke of an ordinary search.
    expect(offerableAddress("rit", [ana])).toBe("");
    expect(offerableAddress("rita@", [ana])).toBe("");
  });

  it("offers nothing for somebody already in the list", () => {
    expect(offerableAddress("ana@acme.co", [ana])).toBe("");
  });

  it("ignores case and surrounding space", () => {
    expect(offerableAddress("  Rita@Contractor.io ", [])).toBe("rita@contractor.io");
  });
});

describe("not offering to add somebody twice", () => {
  it("says nothing about an address already in the audience", () => {
    // They may be absent from the list — searching narrows to the
    // directory, and an address with no account is in neither — so
    // without this the picker offers to add somebody it has already
    // added, which reads as the first add not having worked.
    expect(offerableAddress("rita@contractor.io", [], ["rita@contractor.io"])).toBe("");
  });

  it("still offers one that is not", () => {
    expect(offerableAddress("rita@contractor.io", [], ["ana@acme.co"])).toBe(
      "rita@contractor.io",
    );
  });
});
