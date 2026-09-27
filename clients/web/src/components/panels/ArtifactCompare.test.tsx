// @vitest-environment jsdom
/**
 * Comparing two versions.
 *
 * The rows arrive aligned from the backend, so what this tests is the
 * rendering decisions that carry meaning: that a changed line shows
 * both halves on one row, that the empty half of an unpaired change is
 * visibly absent rather than blank, and that "nothing changed" is said
 * out loud instead of shown as an empty panel.
 *
 * The default pair matters too. Somebody opening a comparison nearly
 * always means "what changed in the last revision", and a picker that
 * opened on v1-against-v1 would make them set it up every time.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ArtifactCompare } from "./ArtifactCompare";
import { DiffTable, codeRows, tableCells, type DiffRow } from "./ArtifactDiffTable";
import type { IgniClient } from "../../protocol/client";

afterEach(cleanup);

function row(over: Record<string, unknown> = {}) {
  return {
    kind: "equal",
    left_n: 1,
    left: "same",
    right_n: 1,
    right: "same",
    left_parts: [],
    right_parts: [],
    ...over,
  };
}

function diff(over: Record<string, unknown> = {}) {
  return {
    artifact_id: "a1",
    name: "Login flow",
    from_n: 2,
    to_n: 3,
    rows: [row()],
    added: 0,
    removed: 0,
    changed: 0,
    truncated: 0,
    ...over,
  };
}

function makeClient(result: Record<string, unknown>) {
  const calls: { method: string; args: unknown }[] = [];
  const client = {
    rpc: vi.fn(async (method: string, args?: unknown) => {
      calls.push({ method, args });
      return result;
    }),
  } as unknown as IgniClient;
  return { client, calls };
}

describe("ArtifactCompare", () => {
  it("asks for the previous version against the current one", async () => {
    // "What changed in the last revision" is what opening this means.
    const { client, calls } = makeClient(diff());
    render(
      <ArtifactCompare
        client={client}
        artifactId="a1"
        versions={[3, 2, 1]}
        headN={3}
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    expect(calls[0].args).toMatchObject({ id: "a1", from_n: 2, to_n: 3 });
  });

  it("shows a rewritten line as one row with both halves", async () => {
    const { client } = makeClient(
      diff({
        rows: [
          row({
            kind: "changed",
            left: "good for 24 hours",
            right: "expires in 15 minutes",
          }),
        ],
        changed: 1,
      }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    await screen.findByText("good for 24 hours");

    const tr = document.querySelector('tr[data-kind="changed"]')!;
    expect(tr.textContent).toContain("good for 24 hours");
    expect(tr.textContent).toContain("expires in 15 minutes");
  });

  it("marks an addition so only the right half is filled", async () => {
    const { client } = makeClient(
      diff({
        rows: [row({ kind: "added", left_n: 0, left: "", right_n: 4, right: "new line" })],
        added: 1,
      }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    await screen.findByText("new line");

    const cells = document.querySelectorAll('tr[data-kind="added"] td');
    // left gutter, left cell, right gutter, right cell
    expect(cells[0].textContent).toBe("");
    expect(cells[1].textContent).toBe("");
    expect(cells[3].textContent).toBe("new line");
  });

  it("marks a removal so only the left half is filled", async () => {
    const { client } = makeClient(
      diff({
        rows: [row({ kind: "removed", left_n: 2, left: "gone", right_n: 0, right: "" })],
        removed: 1,
      }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    await screen.findByText("gone");
    expect(document.querySelectorAll('tr[data-kind="removed"] td')[3].textContent).toBe("");
  });

  it("says nothing changed rather than showing an empty panel", async () => {
    // A revision that only touched metadata produces an empty diff.
    const { client } = makeClient(diff({ rows: [], added: 0, removed: 0, changed: 0 }));
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    expect(await screen.findByText(/Nothing changed between v2 and v3/)).toBeTruthy();
  });

  it("counts a rewrite on both sides of the tally", async () => {
    // One changed line is one line gone and one arrived; a tally that
    // showed +0 −0 for a rewritten paragraph would be lying.
    const { client } = makeClient(
      diff({ rows: [row({ kind: "changed" })], changed: 2, added: 1, removed: 1 }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    expect(await screen.findByText("+3")).toBeTruthy();
    expect(screen.getByText("−3")).toBeTruthy();
  });

  it("re-asks when the pair changes", async () => {
    const { client, calls } = makeClient(diff());
    render(
      <ArtifactCompare
        client={client}
        artifactId="a1"
        versions={[3, 2, 1]}
        headN={3}
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(calls.length).toBe(1));

    const selects = document.querySelectorAll("select");
    fireEvent.change(selects[0], { target: { value: "1" } });
    await waitFor(() => expect(calls.length).toBe(2));
    expect(calls[1].args).toMatchObject({ from_n: 1, to_n: 3 });
  });

  it("says how much it left out", async () => {
    // A diff that quietly stops reads as "and nothing else changed".
    const { client } = makeClient(
      diff({ rows: [row({ kind: "added" })], added: 1, truncated: 40 }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    expect(await screen.findByText(/40 more lines not shown/)).toBeTruthy();
  });

  it("surfaces a failure instead of looking like no changes", async () => {
    const client = {
      rpc: vi.fn(async () => {
        throw new Error("backend is down");
      }),
    } as unknown as IgniClient;
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    expect(await screen.findByText(/backend is down/)).toBeTruthy();
  });

  it("marks which side is current", async () => {
    const { client } = makeClient(diff({ rows: [row({ kind: "added" })], added: 1 }));
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    expect(await screen.findByText(/v3 · current/)).toBeTruthy();
  });
});

describe("it reads like a document, not like a terminal", () => {
  it("marks only the words that changed inside a rewritten line", async () => {
    // Marking the whole line tells a reader something in this
    // paragraph moved and leaves them to find out what — which, for a
    // paragraph, is the hard part.
    const { client } = makeClient(
      diff({
        rows: [
          row({
            kind: "changed",
            left: "The cookie is good for 24 hours.",
            right: "The token is good for 15 hours.",
            left_parts: [
              { text: "The ", changed: false },
              { text: "cookie", changed: true },
              { text: " is good for 24 hours.", changed: false },
            ],
            right_parts: [
              { text: "The ", changed: false },
              { text: "token", changed: true },
              { text: " is good for 15 hours.", changed: false },
            ],
          }),
        ],
        changed: 1,
      }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    await screen.findByText("cookie");

    const marked = [...document.querySelectorAll(".diff-word")].map((n) => n.textContent);
    expect(marked).toEqual(["cookie", "token"]);
  });

  it("still renders a line that has no word breakdown", async () => {
    // Added and removed lines have nothing to compare against, so
    // they arrive whole.
    const { client } = makeClient(
      diff({ rows: [row({ kind: "added", left: "", right: "a new sentence" })], added: 1 }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    expect(await screen.findByText("a new sentence")).toBeTruthy();
  });

  it("shows an unchanged document in full, not a summary of it", async () => {
    // The opposite of a code review. There the question is "what did
    // this change"; here it is "what does the document say now, and
    // what moved" — and a document with its middle replaced by
    // "29 unchanged lines" cannot answer the first half.
    const many = Array.from({ length: 12 }, (_, i) =>
      row({ kind: "equal", left: `line ${i}`, right: `line ${i}` }),
    );
    const { client } = makeClient(
      diff({ rows: [...many, row({ kind: "added", right: "new" })], added: 1 }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );

    await screen.findByText("new");
    expect(screen.queryByRole("button", { name: /unchanged lines/ })).toBeNull();
    // Every line, including ones far from the edit.
    expect(screen.getAllByText("line 0")).toHaveLength(2);
    expect(screen.getAllByText("line 6")).toHaveLength(2);
  });

  it("keeps a little context either side of a change", async () => {
    const many = Array.from({ length: 12 }, (_, i) =>
      row({ kind: "equal", left: `line ${i}`, right: `line ${i}` }),
    );
    const { client } = makeClient(
      diff({ rows: [...many, row({ kind: "added", right: "new" })], added: 1 }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    await screen.findByText("new");
    // The change needs somewhere to sit, so the lines next to it stay.
    // Two matches — the line appears in both columns, unchanged.
    expect(screen.getAllByText("line 11")).toHaveLength(2);
  });

  it("says how much moved, without machinery to walk it", async () => {
    // There was briefly a stepper here. The changes are highlighted
    // and a document is a few screens: two buttons and an index to
    // walk five rows is machinery in front of something visible.
    const many = Array.from({ length: 12 }, (_, i) =>
      row({ kind: "equal", left: `line ${i}`, right: `line ${i}` }),
    );
    const { client } = makeClient(
      diff({ rows: [...many, row({ kind: "added", right: "new" })], added: 1 }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );

    await screen.findByText("new");
    expect(screen.getByText(/1 change across 13 rows/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Next change" })).toBeNull();
  });

  it("keeps the tally, which answers the question at a glance", async () => {
    const { client } = makeClient(
      diff({ rows: [row({ kind: "added", right: "one" })], added: 1 }),
    );
    render(
      <ArtifactCompare client={client} artifactId="a1" versions={[3, 2]} headN={3} onClose={() => {}} />,
    );
    await screen.findByText("one");
    expect(screen.getByText("+1")).toBeTruthy();
  });
});

describe("showing the whole document", () => {
  const eq = (i: number) =>
    row({ kind: "equal", left: `l${i}`, right: `l${i}` }) as unknown as DiffRow;
  const change = () => row({ kind: "added", right: "x" }) as unknown as DiffRow;

  it("renders every row, however little changed", () => {
    // Folding the unchanged parts away is what a code review wants —
    // the question there is "what did this change". Here the question
    // is "what does the document say now, and what moved", and a
    // document with its middle replaced by "29 unchanged lines"
    // cannot answer the first half of it.
    const rows = [...Array.from({ length: 12 }, (_, i) => eq(i)), change()];
    const { container } = render(
      <DiffTable
        diff={{ artifact_id: "a1", name: "Doc", from_n: 1, to_n: 2, rows, added: 1, removed: 0, changed: 0, truncated: 0 }}
        leftLabel="v1"
        rightLabel="v2"
      />,
    );

    expect(container.querySelectorAll("tbody tr")).toHaveLength(rows.length);
    expect(container.querySelector(".diff-fold")).toBeNull();
  });

  it("counts the changes, since the whole document is shown", () => {
    const rows = [eq(0), change(), eq(1), change(), eq(2)];
    render(
      <DiffTable
        diff={{ artifact_id: "a1", name: "Doc", from_n: 1, to_n: 2, rows, added: 2, removed: 0, changed: 0, truncated: 0 }}
        leftLabel="v1"
        rightLabel="v2"
      />,
    );
    expect(screen.getByText(/2 changes across 5 rows/)).toBeTruthy();
  });

  it("counts a run of adjacent rows as one change", () => {
    // Stepping through each line of a rewritten paragraph separately
    // is the navigator getting in the way of the thing it is for.
    const rows = [eq(0), change(), change(), change(), eq(1)];
    render(
      <DiffTable
        diff={{ artifact_id: "a1", name: "Doc", from_n: 1, to_n: 2, rows, added: 3, removed: 0, changed: 0, truncated: 0 }}
        leftLabel="v1"
        rightLabel="v2"
      />,
    );
    expect(screen.getByText(/1 change(?!s)/)).toBeTruthy();
  });

  it("says nothing at all when nothing moved", () => {
    const rows = [eq(0), eq(1)];
    const { container } = render(
      <DiffTable
        diff={{ artifact_id: "a1", name: "Doc", from_n: 1, to_n: 2, rows, added: 0, removed: 0, changed: 0, truncated: 0 }}
        leftLabel="v1"
        rightLabel="v2"
      />,
    );
    expect(container.querySelector(".diff-count")).toBeNull();
  });

  it("anchors each change, so a link can point at one", () => {
    const rows = [eq(0), change(), eq(1)];
    const { container } = render(
      <DiffTable
        diff={{ artifact_id: "a1", name: "Doc", from_n: 1, to_n: 2, rows, added: 1, removed: 0, changed: 0, truncated: 0 }}
        leftLabel="v1"
        rightLabel="v2"
      />,
    );
    expect(container.querySelector("#diff-change-1")).toBeTruthy();
    // Unchanged rows carry no id: they are not destinations.
    expect(container.querySelector("#diff-change-0")).toBeNull();
  });
});

describe("tableCells", () => {
  it("splits a pipe row into cells", () => {
    expect(tableCells("| a | b | c |")).toEqual(["a", "b", "c"]);
  });

  it("ignores the separator row", () => {
    // Drawing a row of dashes says nothing and costs a row to say it.
    expect(tableCells("| --- | --- |")).toBeNull();
    expect(tableCells("| :--- | ---: |")).toBeNull();
  });

  it("is not fooled by prose containing a pipe", () => {
    expect(tableCells("run a | b in the shell")).toBeNull();
  });

  it("keeps empty cells so the columns still line up", () => {
    expect(tableCells("| a |  | c |")).toEqual(["a", "", "c"]);
  });
});

describe("codeRows", () => {
  const r = (over: Record<string, unknown> = {}) => row(over) as unknown as DiffRow;

  it("marks the lines between two fences", () => {
    const rows = [
      r({ left: "prose", right: "prose" }),
      r({ left: "```yaml", right: "```yaml" }),
      r({ left: "  key: 1", right: "  key: 2" }),
      r({ left: "```", right: "```" }),
      r({ left: "after", right: "after" }),
    ];
    expect(codeRows(rows)).toEqual([false, true, true, true, false]);
  });

  it("treats an unclosed fence as running to the end", () => {
    // Malformed input should degrade, not mis-render the rest as
    // markdown that happens to start with a hash.
    const rows = [r({ left: "```", right: "```" }), r({ left: "# not a heading", right: "x" })];
    expect(codeRows(rows)).toEqual([true, true]);
  });

  it("handles a fence that exists on one side only", () => {
    const rows = [r({ left: "", right: "```" }), r({ left: "", right: "code" })];
    expect(codeRows(rows)).toEqual([true, true]);
  });
});
