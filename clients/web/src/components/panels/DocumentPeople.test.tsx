// @vitest-environment jsdom
/**
 * The row of faces under a document's title.
 *
 * Most of what matters here is about the seams between two answers
 * that arrive separately: the roster, which is a fact about the
 * document, and presence, which is a fact about this instant. Getting
 * them to agree about who is one person is the whole job — matching
 * on the wrong field shows a colleague twice, once greyed and once
 * lit, which reads as two people with the same name.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  DocumentPeople,
  faces,
  initials,
  reachNote,
  type Occupant,
  type Roster,
} from "./DocumentPeople";

afterEach(cleanup);

const maya = {
  identity: "maya@acme.test",
  user_id: "u-maya",
  email: "maya@acme.test",
  name: "Maya Okonkwo",
  role: "owner",
};

const sam = {
  identity: "sam@acme.test",
  user_id: "u-sam",
  email: "sam@acme.test",
  name: "Sam Ortega",
  role: "reader",
};

const roster = (over: Partial<Roster> = {}): Roster => ({
  people: [maya, sam],
  reach: "users",
  domain: "",
  can_manage: true,
  ...over,
});

const present = (over: Partial<Occupant> = {}): Occupant => ({
  user_id: "u-sam",
  name: "Sam Ortega",
  email: "sam@acme.test",
  colour: "#3f9be8",
  editing: false,
  ...over,
});

describe("the letters on a face", () => {
  it("takes the first and last name", () => {
    expect(initials("Maya Okonkwo", "maya@acme.test")).toBe("MO");
  });

  it("takes two letters from a single name", () => {
    expect(initials("Cher", "c@acme.test")).toBe("CH");
  });

  it("reads an address when there is no name", () => {
    // "sam.patel@acme.test" has no words to take initials from, so
    // the local part is split on its punctuation — otherwise every
    // nameless account is a face saying "S".
    expect(initials("", "sam.patel@acme.test")).toBe("SP");
  });

  it("falls back to two letters of a one-word address", () => {
    expect(initials("", "maya@acme.test")).toBe("MA");
  });

  it("says something rather than nothing when it has nothing", () => {
    expect(initials("", "")).toBe("?");
  });

  it("handles a middle name by taking the ends", () => {
    expect(initials("Ada Byron Lovelace", "")).toBe("AL");
  });
});

describe("the rest of the sentence", () => {
  it("names the domain when a whole company can read it", () => {
    // Three faces on a document the company can read tells the
    // reader the wrong thing. This is what corrects it.
    expect(reachNote(roster({ reach: "domain", domain: "acme.test" }))).toBe(
      "Everyone at acme.test",
    );
  });

  it("says so when anybody with the link can read it", () => {
    expect(reachNote(roster({ reach: "public" }))).toBe("Anyone with the link");
  });

  it("says nothing extra when the faces are the whole story", () => {
    expect(reachNote(roster())).toBe("");
  });

  it("says only you when there is nobody else", () => {
    expect(reachNote(roster({ people: [maya], reach: "private" }))).toBe("Only you");
  });
});

describe("matching the roster against who is here", () => {
  it("lights the face of somebody present", () => {
    const row = faces(roster(), [present()]);

    expect(row.map((one) => [one.name, !!one.present])).toEqual([
      ["Maya Okonkwo", false],
      ["Sam Ortega", true],
    ]);
  });

  it("matches on the address when the ids do not line up", () => {
    // The roster names an identity the owner typed; presence carries
    // the account that turned up. They are the same colleague, and a
    // row that showed them twice would read as two people.
    const row = faces(roster(), [present({ user_id: "different-id" })]);

    expect(row).toHaveLength(2);
    expect(row[1].present).not.toBeNull();
  });

  it("appends somebody here who is not on the roster", () => {
    // How a domain reader appears: nobody named them, and they are
    // reading it. "Who is here" has to be answered completely.
    const row = faces(roster({ people: [maya], reach: "domain" }), [present()]);

    expect(row.map((one) => one.name)).toEqual(["Maya Okonkwo", "Sam Ortega"]);
  });

  it("does not append somebody twice for being on it and in it", () => {
    const row = faces(roster(), [present(), present({ user_id: "u-maya", email: maya.email })]);

    expect(row).toHaveLength(2);
  });

  it("keeps the owner first", () => {
    // Whose document it is is the first thing about it, and arrival
    // order must not reorder that.
    const row = faces(roster(), [present()]);

    expect(row[0].role).toBe("owner");
  });

  it("shows the roster with nobody here at all", () => {
    expect(faces(roster(), []).every((one) => one.present === null)).toBe(true);
  });

  it("shows whoever is here with no roster yet", () => {
    // The two answers arrive separately, and the faster one should
    // not wait for the slower.
    expect(faces(null, [present()]).map((one) => one.name)).toEqual(["Sam Ortega"]);
  });
});

describe("what gets drawn", () => {
  it("draws a face for everybody", () => {
    render(<DocumentPeople roster={roster()} here={[]} />);

    expect(screen.getByText("MO")).toBeTruthy();
    expect(screen.getByText("SO")).toBeTruthy();
  });

  it("marks the face of somebody in the document", () => {
    render(<DocumentPeople roster={roster()} here={[present()]} />);

    expect(screen.getByText("SO").className).toContain("is-here");
    expect(screen.getByText("MO").className).not.toContain("is-here");
  });

  it("marks the face of somebody typing", () => {
    render(<DocumentPeople roster={roster()} here={[present({ editing: true })]} />);

    expect(screen.getByText("SO").className).toContain("is-editing");
  });

  it("carries the colour the server chose", () => {
    // The same colour their cursor gets, so a face and a caret in
    // the text are recognisably one person.
    render(<DocumentPeople roster={roster()} here={[present()]} />);

    expect(screen.getByText("SO").getAttribute("style")).toContain("#3f9be8");
  });

  it("draws nothing at all on a private document nobody else is in", () => {
    // A row of one face saying "Only you" under every draft is
    // furniture.
    const { container } = render(
      <DocumentPeople roster={roster({ people: [maya], reach: "private" })} here={[]} />,
    );

    expect(container.querySelector(".doc-people")).toBeNull();
  });

  it("draws nothing before the roster has arrived", () => {
    const { container } = render(<DocumentPeople roster={null} here={[]} />);

    expect(container.querySelector(".doc-people")).toBeNull();
  });

  it("still draws on a private document once somebody else is in it", () => {
    // Which should not happen — and if it does, the reader needs to
    // know far more than they would on a shared one.
    const { container } = render(
      <DocumentPeople roster={roster({ people: [maya], reach: "private" })} here={[present()]} />,
    );

    expect(container.querySelector(".doc-people")).not.toBeNull();
  });
});
