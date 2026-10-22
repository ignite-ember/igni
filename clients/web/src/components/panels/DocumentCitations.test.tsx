// @vitest-environment jsdom
/**
 * Finding a cited passage in the document the reader is looking at.
 *
 * The passage text comes from the store with its markdown intact — "##
 * Reading your budget", backticks, table pipes. The body it has to be
 * found in is *rendered*, where none of that survives. Matching the
 * two verbatim finds nothing, every time, and the failure is silent:
 * no marker, no error, a feature that looks switched off. That is
 * what these tests are about.
 */
import { describe, expect, it } from "vitest";

import { asRendered } from "./DocumentCitations";

describe("reading a passage the way the document shows it", () => {
  it("drops heading markers", () => {
    expect(asRendered("## Reading your budget")).toBe("Reading your budget");
  });

  it("drops the backticks around inline code", () => {
    // `RateLimit-Remaining` is rendered as text inside a <code>, so
    // the backticks are not in the body to match against.
    expect(asRendered("read `RateLimit-Remaining` first")).toBe(
      "read RateLimit-Remaining first",
    );
  });

  it("drops emphasis without eating the words", () => {
    expect(asRendered("**no longer** sent as a _header_")).toBe(
      "no longer sent as a header",
    );
  });

  it("drops list markers, ordered and not", () => {
    expect(asRendered("- one\n- two")).toBe("one two");
    expect(asRendered("1. first\n2. second")).toBe("first second");
  });

  it("drops blockquote markers", () => {
    expect(asRendered("> Sessions are not an audit trail.")).toBe(
      "Sessions are not an audit trail.",
    );
  });

  it("keeps table cells and loses the pipes", () => {
    // Rendered, a table is its cell text; the delimiter row is not
    // shown at all.
    expect(asRendered("| reads | 120 / min |\n| --- | --- |\n| writes | 10 / min |")).toBe(
      "reads 120 / min writes 10 / min",
    );
  });

  it("keeps a link's text and loses its target", () => {
    expect(asRendered("see [the spec](https://example.test/x) for more")).toBe(
      "see the spec for more",
    );
  });

  it("drops a fence line but keeps what is inside it", () => {
    // The code itself is on screen; the ``` is not.
    expect(asRendered("```yaml\nsession:\n  ttl: 15\n```")).toBe("session: ttl: 15");
  });

  it("collapses the line breaks a chunk was cut on", () => {
    // Chunks are cut to a character budget, so a sentence arrives
    // wrapped wherever the source wrapped it.
    expect(asRendered("How a client should behave when it is\nbeing rate limited.")).toBe(
      "How a client should behave when it is being rate limited.",
    );
  });

  it("survives an empty or missing passage", () => {
    expect(asRendered("")).toBe("");
    expect(asRendered(undefined as unknown as string)).toBe("");
  });

  it("turns a real stored passage into something the body contains", () => {
    // Taken from the live store: the head of "Client retry guide" v3
    // as the chunker cut it, against the text the reader sees.
    const stored =
      "# Client retry guide\n\nHow a client should behave when it is being rate limited. Written\n" +
      "against the limits and headers the edge publishes.\n\n## Reading your budget\n\n" +
      "Before retrying, read `RateLimit-Remaining`. If it is above zero the\n" +
      "failure was not a rate limit and retrying immediately is fine.\n";
    const rendered =
      "Client retry guide How a client should behave when it is being rate limited. " +
      "Written against the limits and headers the edge publishes. Reading your budget " +
      "Before retrying, read RateLimit-Remaining. If it is above zero the failure was " +
      "not a rate limit and retrying immediately is fine.";

    expect(rendered).toContain(asRendered(stored));
  });
});
