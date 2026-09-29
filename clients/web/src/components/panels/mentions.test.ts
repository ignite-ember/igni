/**
 * Reading an ``@`` out of a half-written sentence.
 *
 * The cases worth the most scrutiny are the ones where a popup must
 * *not* open. Comments on these documents are full of email addresses
 * and file paths, and a mention menu that appeared every time
 * somebody wrote "mail sam@acme.test" would be a feature people
 * learned to type around.
 */
import { describe, expect, it } from "vitest";

import {
  accessNotice,
  cannotRead,
  handleFor,
  insertMention,
  mentionSpan,
  namedIn,
  unknownHandles,
  writtenHandles,
  type Person,
} from "./mentions";

const person = (over: Partial<Person> = {}): Person => ({
  user_id: "u1",
  email: "sam.patel@contractor.test",
  name: "Sam Patel",
  has_access: false,
  ...over,
});

/** The other half of every contrast here: named, and able to read it. */
const maya = person({
  user_id: "u2",
  email: "maya@acme.test",
  name: "Maya Okonkwo",
  has_access: true,
});

describe("finding the mention under the caret", () => {
  it("reads what has been typed after an @", () => {
    expect(mentionSpan("@sam", 4)).toEqual({ start: 0, query: "sam" });
  });

  it("opens on a bare @ before anything is typed", () => {
    // The list has to be offerable before the first letter, or the
    // only way to discover who exists is to guess a prefix.
    expect(mentionSpan("ask @", 5)).toEqual({ start: 4, query: "" });
  });

  it("finds one mid-sentence, after a space", () => {
    expect(mentionSpan("could you check this, @sam", 26)).toEqual({ start: 22, query: "sam" });
  });

  it("stays shut on an address somebody is typing out", () => {
    // "write to sam@acme.test" is not a mention. A popup here fires
    // every time anybody writes down an email address, which in these
    // comments is constantly.
    expect(mentionSpan("write to sam@acme.test", 22)).toBeNull();
  });

  it("stays shut when the caret has moved past the word", () => {
    expect(mentionSpan("@sam and then some more", 23)).toBeNull();
  });

  it("closes once the mention is finished with a space", () => {
    expect(mentionSpan("@sam ", 5)).toBeNull();
  });

  it("reads the mention the caret is in, not the last one in the text", () => {
    // Going back to fix an earlier name. The menu has to follow the
    // caret, not the end of the line.
    expect(mentionSpan("@sam and @may", 13)).toEqual({ start: 9, query: "may" });
  });

  it("finds nothing in text with no @ at all", () => {
    expect(mentionSpan("just a remark", 13)).toBeNull();
  });
});

describe("writing the chosen name in", () => {
  it("replaces what was typed and leaves a space after", () => {
    const span = mentionSpan("could you check, @sa", 20)!;

    expect(insertMention("could you check, @sa", span, "sam.patel")).toEqual({
      text: "could you check, @sam.patel ",
      caret: 28,
    });
  });

  it("keeps whatever came after the caret", () => {
    // Going back to fill in a name in the middle of a finished
    // sentence, which is what the caret-following span is for.
    const text = "@sa should look at this";
    const span = mentionSpan(text, 3)!;

    expect(insertMention(text, span, "sam.patel").text).toBe(
      "@sam.patel  should look at this",
    );
  });

  it("writes the local part of an address, not the whole thing", () => {
    // Two @ in one word reads as a mistake.
    expect(handleFor(person(), [person(), maya])).toBe("sam.patel");
  });

  it("writes the whole address when two people would share a handle", () => {
    // A mention that could mean either of two colleagues names
    // neither of them.
    const other = person({ user_id: "u3", email: "sam.patel@acme.test", name: "Sam Patel" });

    expect(handleFor(person(), [person(), other])).toBe("sam.patel@contractor.test");
  });
});

describe("who a finished comment names", () => {
  it("finds somebody written by their handle", () => {
    expect(namedIn("@sam.patel what do you think?", [person(), maya])).toEqual([person()]);
  });

  it("finds somebody written by their full address", () => {
    expect(namedIn("@sam.patel@contractor.test ping", [person(), maya])).toEqual([person()]);
  });

  it("does not match a plain address that was never a mention", () => {
    expect(namedIn("write to sam.patel@contractor.test", [person(), maya])).toEqual([]);
  });

  it("finds more than one", () => {
    expect(namedIn("@sam.patel and @maya", [person(), maya])).toEqual([person(), maya]);
  });

  it("forgets somebody once their name is deleted", () => {
    // Why this reads the text rather than remembering what was
    // clicked: a notice about somebody who is no longer mentioned
    // asks for a decision about a sentence already rewritten.
    expect(namedIn("and then", [person(), maya])).toEqual([]);
  });
});

describe("who was named and cannot read it", () => {
  it("is the named people without access", () => {
    expect(cannotRead("@sam.patel @maya", [person(), maya])).toEqual([person()]);
  });

  it("is nobody when everyone named can read it", () => {
    expect(cannotRead("@sam.patel", [person({ has_access: true })])).toEqual([]);
  });

  it("ignores colleagues who were never mentioned", () => {
    // The directory is full of people without access. Only the ones
    // actually named are anybody's problem.
    expect(cannotRead("nothing to see", [person(), maya])).toEqual([]);
  });
});

describe("what it says about them", () => {
  it("states it as a fact when it can be fixed here", () => {
    const said = accessNotice(person(), { can_grant: true, owner_name: "Maya Okonkwo" });

    expect(said).toBe("Sam Patel can’t open this document yet.");
  });

  it("names who to ask when it cannot", () => {
    // The whole point of not being able to grant it is that somebody
    // else can, so the sentence has to say who.
    const said = accessNotice(person(), { can_grant: false, owner_name: "Maya Okonkwo" });

    expect(said).toContain("Maya Okonkwo");
  });

  it("falls back to a description when the owner has no name", () => {
    const said = accessNotice(person(), { can_grant: false, owner_name: "" });

    expect(said).toBe("Sam Patel can’t open this document. Only its owner can change that.");
  });

  it("uses an address when a person has no name on file", () => {
    const nameless = person({ name: "" });

    expect(accessNotice(nameless, { can_grant: true, owner_name: "" })).toBe(
      "sam.patel@contractor.test can’t open this document yet.",
    );
  });
});

describe("names the composer has not looked up yet", () => {
  it("reads the handles out of finished text", () => {
    expect(writtenHandles("@sam.patel and @maya, not sam@acme.test")).toEqual([
      "sam.patel",
      "maya",
    ]);
  });

  it("counts each handle once however often it is written", () => {
    expect(writtenHandles("@maya @maya @maya")).toEqual(["maya"]);
  });

  it("asks about a name nobody in hand accounts for", () => {
    // The case the menu misses: typed faster than its pause, or
    // pasted in whole. Without this, the quickest way to mention
    // somebody was also the way that told you nothing.
    expect(unknownHandles("@sam ", [])).toEqual(["sam"]);
  });

  it("does not ask again about somebody already known", () => {
    expect(unknownHandles("@sam.patel ", [person()])).toEqual([]);
  });

  it("recognises somebody written out in full", () => {
    expect(unknownHandles("@sam.patel@contractor.test ", [person()])).toEqual([]);
  });

  it("never asks the directory about igni", () => {
    // It is the agent, it is in every other sentence in this rail,
    // and the answer can only ever be empty.
    expect(unknownHandles("@igni please fix this", [])).toEqual([]);
  });

  it("asks about nothing when nobody is named", () => {
    expect(unknownHandles("just a remark", [])).toEqual([]);
  });
});
