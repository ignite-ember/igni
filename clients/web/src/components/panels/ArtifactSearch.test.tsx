// @vitest-environment jsdom
/**
 * Finding a document.
 *
 * The bug worth a test of its own: the results list this replaces
 * paired a hit back to a document by comparing names and sources, and
 * when that comparison failed the row silently stopped being
 * clickable. A search result you cannot open is worse than no result
 * — it looks like the product found the thing and then refused to
 * show it. Hits carry their `artifact_id` now, so every row opens.
 *
 * The rest is about the two ways of looking. Filtering runs per
 * keystroke against the loaded list; searching runs on Enter against
 * the vector index. They return different things, so the UI has to
 * say which one produced what is on screen — otherwise a missing
 * document has no explanation.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ArtifactSearch, type SearchableDoc } from "./ArtifactSearch";

afterEach(cleanup);

const DOCS: SearchableDoc[] = [
  {
    id: "a1",
    name: "Auth model",
    kind: "design",
    n: 3,
    preview: "Auth is a signed JWT, not a session cookie.",
    added_at: "",
    size: 400,
  },
  {
    id: "a2",
    name: "Login flow",
    kind: "note",
    n: 2,
    preview: "The login flow uses the auth model.",
    added_at: "",
    size: 200,
  },
  {
    id: "a3",
    name: "Release checklist",
    kind: "note",
    n: 1,
    preview: "Steps before a tag is cut.",
    added_at: "",
    size: 120,
  },
];

function setup(overrides: {
  hits?: unknown;
  stale?: string[];
  docs?: SearchableDoc[];
} = {}) {
  const opened: string[] = [];
  const asked: string[] = [];
  const rpc = vi.fn(async () => overrides.hits ?? []);
  render(
    <ArtifactSearch
      client={{ rpc } as never}
      docs={overrides.docs ?? DOCS}
      staleIds={new Set(overrides.stale ?? [])}
      onOpen={(id) => opened.push(id)}
      onAsk={(q) => asked.push(q)}
    />,
  );
  const input = screen.getByPlaceholderText(/filter by name/i);
  return { opened, asked, rpc, input };
}

describe("filtering", () => {
  it("lists everything before anything is typed", () => {
    setup();
    expect(screen.getByText("Auth model")).toBeTruthy();
    expect(screen.getByText("Login flow")).toBeTruthy();
    expect(screen.getByText("Release checklist")).toBeTruthy();
  });

  it("narrows on each keystroke without asking the backend", () => {
    const { rpc, input } = setup();
    fireEvent.change(input, { target: { value: "login" } });

    expect(screen.getByText("Login flow")).toBeTruthy();
    expect(screen.queryByText("Release checklist")).toBeNull();
    expect(rpc).not.toHaveBeenCalled();
  });

  it("matches on the preview text, not just the name", () => {
    const { input } = setup();
    fireEvent.change(input, { target: { value: "session cookie" } });
    expect(screen.getByText("Auth model")).toBeTruthy();
  });

  it("filters by kind", () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: /^design 1$/i }));
    expect(screen.getByText("Auth model")).toBeTruthy();
    expect(screen.queryByText("Login flow")).toBeNull();
  });

  it("offers no kind chips when everything is one kind", () => {
    setup({ docs: [DOCS[1], DOCS[2]] });
    expect(screen.queryByRole("button", { name: /^all /i })).toBeNull();
  });
});

describe("searching by meaning", () => {
  it("goes to the backend only on Enter", async () => {
    const { rpc, input } = setup({ hits: [] });
    fireEvent.change(input, { target: { value: "tokens" } });
    expect(rpc).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(rpc).toHaveBeenCalledWith("artifact_search", { query: "tokens" }),
    );
  });

  it("says which way produced the results on screen", async () => {
    const { input } = setup({
      hits: [
        { artifact_id: "a1", name: "Auth model", kind: "design", n: 3, content: "…" },
      ],
    });
    fireEvent.change(input, { target: { value: "tokens" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(await screen.findByText(/match by meaning for/i)).toBeTruthy();
  });

  it("typing again returns to the filtered list", async () => {
    const { input } = setup({
      hits: [
        { artifact_id: "a1", name: "Auth model", kind: "design", n: 3, content: "…" },
      ],
    });
    fireEvent.change(input, { target: { value: "tokens" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await screen.findByText(/match by meaning for/i);

    // Asserted on the results banner, not on the words "by meaning" —
    // the empty-state hint also contains them, so the looser pattern
    // passed for the wrong reason.
    fireEvent.change(input, { target: { value: "token" } });
    expect(screen.queryByText(/match by meaning for/i)).toBeNull();
  });
});

describe("opening a result", () => {
  it("opens a filtered row by its id", () => {
    const { opened } = setup();
    fireEvent.click(screen.getByText("Login flow"));
    expect(opened).toEqual(["a2"]);
  });

  it("opens a search hit whose name matches no document in the list", async () => {
    // The regression. The old list paired a hit back to a document by
    // name, so a hit the loaded list did not contain became a dead
    // row. The id is authoritative now.
    const { opened, input } = setup({
      hits: [
        {
          artifact_id: "zz9",
          name: "Something not in the loaded list",
          kind: "note",
          n: 1,
          content: "…",
        },
      ],
    });
    fireEvent.change(input, { target: { value: "anything" } });
    fireEvent.keyDown(input, { key: "Enter" });

    fireEvent.click(await screen.findByText("Something not in the loaded list"));
    expect(opened).toEqual(["zz9"]);
  });

  it("every row is reachable as a button", () => {
    setup();
    // Rows are <button>, so keyboard users get them for free. Three
    // documents plus the kind chips.
    const rows = screen
      .getAllByRole("button")
      .filter((b) => b.className.includes("afs-row"));
    expect(rows).toHaveLength(3);
  });
});

describe("what a row tells you", () => {
  it("carries kind and version", () => {
    setup();
    expect(screen.getByText("design")).toBeTruthy();
    expect(screen.getByText("v3")).toBeTruthy();
  });

  it("marks a document whose source has moved", () => {
    setup({ stale: ["a2"] });
    expect(screen.getByText("out of date")).toBeTruthy();
  });

  it("marks nothing when nothing is stale", () => {
    setup();
    expect(screen.queryByText("out of date")).toBeNull();
  });
});

describe("the dead end", () => {
  it("suggests searching by meaning when a filter finds nothing", () => {
    const { input } = setup();
    fireEvent.change(input, { target: { value: "zzzz" } });
    expect(screen.getByText(/search by meaning/i)).toBeTruthy();
  });

  it("hands the query to the graph when a search finds nothing", async () => {
    const { asked, input } = setup({ hits: [] });
    fireEvent.change(input, { target: { value: "what broke" } });
    fireEvent.keyDown(input, { key: "Enter" });

    fireEvent.click(await screen.findByRole("button", { name: /ask the graph/i }));
    expect(asked).toEqual(["what broke"]);
  });

  it("does not offer to ask about an empty query", () => {
    setup({ docs: [] });
    expect(screen.queryByRole("button", { name: /ask the graph/i })).toBeNull();
  });
});
