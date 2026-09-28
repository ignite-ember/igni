// @vitest-environment jsdom
/**
 * Which version the preview shows, and what it says when the passage
 * has gone.
 *
 * Every row in the review queue says "the document has moved on
 * since", which is true of all 38 of them and cannot tell a passage
 * that survived from one that was deleted. Those are opposite
 * decisions — one is a comment you can still act on, the other is a
 * comment about text that no longer exists — so the preview has to
 * distinguish them, and these tests are that distinction.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CommentDocumentPeek } from "./CommentDocumentPeek";
import type { IgniClient } from "../../protocol/client";

afterEach(cleanup);

const HEAD = "# Auth model\n\nAuth is a signed JWT.\n\nRevocation is checked on refresh only.";
const OLD = "# Auth model\n\nAuth is a session cookie, good for 14 days.";

/** Answers `artifact_get`, version 0 meaning head. */
function store(head: string, older: string) {
  const asked: number[] = [];
  const rpc = vi.fn(async (_method: string, args?: Record<string, unknown>) => {
    const version = Number(args?.version ?? 0);
    asked.push(version);
    return version === 0 ? { content: head, n: 5 } : { content: older, n: version };
  });
  return { client: { rpc } as unknown as IgniClient, asked, rpc };
}

function peek(over: Partial<Parameters<typeof CommentDocumentPeek>[0]> = {}) {
  const fallback = store(HEAD, OLD);
  const props = {
    client: fallback.client,
    artifactId: "a1",
    documentName: "Auth model",
    quote: "Auth is a signed JWT.",
    commentN: 2,
    headN: 5,
    anchor: new DOMRect(0, 0, 100, 40),
    cache: new Map(),
    onEnter: () => {},
    onLeave: () => {},
    ...over,
  };
  render(<CommentDocumentPeek {...props} />);
  return fallback;
}

describe("the version the preview shows", () => {
  it("shows head when the quoted passage is still there", async () => {
    // Head is the text the reviewer is acting on: accepting appends
    // after whatever is current, not after the version commented on.
    const { asked } = peek();

    expect(await screen.findByText("v5")).toBeTruthy();
    expect(screen.queryByText(/not in v5 any more/)).toBeNull();
    expect(asked).toEqual([0]);
  });

  it("falls back to the commented version when the passage has gone", async () => {
    const { asked } = peek({ quote: "Auth is a session cookie, good for 14 days." });

    expect(await screen.findByText(/not in v5 any more/)).toBeTruthy();
    expect(screen.getByText("v2")).toBeTruthy();
    // Head first, then the older one — never the older one alone.
    expect(asked).toEqual([0, 2]);
  });

  it("says so when the passage is in neither", async () => {
    // Rewritten in both, or quoted from something else entirely.
    // The first cut fell back and then said "showing v2, where the
    // comment was left" — pointing the reviewer at a version that
    // does not contain it either.
    peek({ quote: "a sentence that was never in this document" });

    expect(await screen.findByText(/could not be located in v2/)).toBeTruthy();
    expect(screen.queryByText(/where the comment was left/)).toBeNull();
  });

  it("does not go looking for an older version it is already on", async () => {
    const { asked } = peek({ commentN: 5, quote: "nowhere to be found" });
    await screen.findByText("v5");
    expect(asked).toEqual([0]);
  });
});

describe("what it costs", () => {
  it("reads a document once, however many rows point at it", async () => {
    // The queue groups by document, so the same one is hovered over
    // and over on the way down the list.
    const shared = new Map();
    const first = peek({ cache: shared });
    await screen.findByText("v5");
    cleanup();

    const second = store(HEAD, OLD);
    render(
      <CommentDocumentPeek
        client={second.client}
        artifactId="a1"
        documentName="Auth model"
        quote="Auth is a signed JWT."
        commentN={2}
        headN={5}
        anchor={new DOMRect(0, 0, 100, 40)}
        cache={shared}
        onEnter={() => {}}
        onLeave={() => {}}
      />,
    );

    expect(await screen.findByText("v5")).toBeTruthy();
    expect(first.asked).toEqual([0]);
    expect(second.asked).toEqual([]);
  });
});

describe("when it cannot read the document", () => {
  it("shows the error rather than an empty panel", async () => {
    const rpc = vi.fn(async () => {
      throw new Error("backend is down");
    });
    render(
      <CommentDocumentPeek
        client={{ rpc } as unknown as IgniClient}
        artifactId="a1"
        documentName="Auth model"
        quote="anything"
        commentN={2}
        headN={5}
        anchor={new DOMRect(0, 0, 100, 40)}
        cache={new Map()}
        onEnter={() => {}}
        onLeave={() => {}}
      />,
    );

    expect(await screen.findByText(/backend is down/)).toBeTruthy();
  });
});
