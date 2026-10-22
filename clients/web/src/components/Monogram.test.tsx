// @vitest-environment jsdom
/**
 * Two letters standing in for a face.
 *
 * The header used to render the *organisation* and nothing else, so
 * an account with no org claim showed a bare cloud icon: signed in,
 * and unable to say as whom.
 *
 * What is tested here is the reading of a name, because the input is
 * often an email address and treating one as a name is the obvious
 * wrong turn — "maya@acme.test" must read as M, not as MA picked from
 * "maya" and "acme".
 */
import { describe, expect, it } from "vitest";
import { hexOf, hueOf, initialsOf } from "./Monogram";

describe("hexOf", () => {
  it("answers a six-digit hex, which is all the caret accepts", () => {
    // The collaborative caret validates against this exact shape and
    // renders anything else transparent — so a colour that is merely
    // valid CSS is not enough.
    expect(hexOf("maya@acme.test")).toMatch(/^#[0-9a-f]{6}$/);
  });

  it("gives the same person the same colour every time", () => {
    expect(hexOf("sam@acme.test")).toBe(hexOf("sam@acme.test"));
  });

  it("gives different people different colours", () => {
    expect(hexOf("maya@acme.test")).not.toBe(hexOf("sam@acme.test"));
  });

  it("agrees with the monogram's hue, so a caret matches its initials", () => {
    const hex = hexOf("maya@acme.test").slice(1);
    const [r, g, b] = [0, 2, 4].map((at) => parseInt(hex.slice(at, at + 2), 16) / 255);
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    let hue = 0;
    if (max !== min) {
      const d = max - min;
      if (max === r) hue = ((g - b) / d + (g < b ? 6 : 0)) * 60;
      else if (max === g) hue = ((b - r) / d + 2) * 60;
      else hue = ((r - g) / d + 4) * 60;
    }
    expect(Math.round(hue)).toBeCloseTo(hueOf("maya@acme.test"), -1);
  });

  it("stays in range for every hue", () => {
    for (const name of ["a", "zz", "person@example.test", "Ada Byron", ""]) {
      expect(hexOf(name)).toMatch(/^#[0-9a-f]{6}$/);
    }
  });
});

describe("initialsOf", () => {
  it("takes the first and last of a full name", () => {
    expect(initialsOf("Maya Okonkwo")).toBe("MO");
  });

  it("skips middle names rather than cramming them in", () => {
    expect(initialsOf("Ada Grace Lovelace")).toBe("AL");
  });

  it("reads an address as its local part only", () => {
    // The domain is not part of anybody's name.
    expect(initialsOf("maya@acme.test")).toBe("MA");
  });

  it("splits a dotted address into initials", () => {
    expect(initialsOf("maya.okonkwo@acme.test")).toBe("MO");
  });

  it("handles underscores and hyphens the same way", () => {
    expect(initialsOf("maya_okonkwo")).toBe("MO");
    expect(initialsOf("maya-okonkwo")).toBe("MO");
  });

  it("gives a single word two letters rather than one", () => {
    // One letter in a circle reads as an icon, not a person.
    expect(initialsOf("maya")).toBe("MA");
  });

  it("says nothing rather than crashing on nothing", () => {
    expect(initialsOf("")).toBe("?");
    expect(initialsOf("   ")).toBe("?");
  });

  it("upper-cases whatever it found", () => {
    expect(initialsOf("ada lovelace")).toBe("AL");
  });
});

describe("hueOf", () => {
  it("is stable for the same name", () => {
    // The monogram is a recognition aid. One that changed colour per
    // session would be decoration.
    expect(hueOf("Maya Okonkwo")).toBe(hueOf("Maya Okonkwo"));
  });

  it("separates different names", () => {
    expect(hueOf("Maya Okonkwo")).not.toBe(hueOf("Dana Reyes"));
  });

  it("stays inside the colour wheel", () => {
    for (const name of ["", "a", "Maya Okonkwo", "x".repeat(200)]) {
      const hue = hueOf(name);
      expect(hue).toBeGreaterThanOrEqual(0);
      expect(hue).toBeLessThan(360);
    }
  });
});
