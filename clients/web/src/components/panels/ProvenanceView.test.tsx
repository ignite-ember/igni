// @vitest-environment jsdom
/**
 * Component tests for ``ProvenanceView``.
 *
 * The layout is an aligned sidebar because that was the only one of
 * footer / hover-card / aligned-sidebar to be both noticed and used
 * when the three were compared on the same citations. What that costs
 * is a pairing mechanism — a number in the text and the same number
 * on the card — so most of what is pinned here is that the pairing
 * holds, and that nothing claims more than it knows.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ProvenanceView,
  balanceFences,
  hueFor,
  layOutPassages,
  strengthLabel,
  type Provenance,
  type ProvenanceMatch,
  type ProvenancePassage,
} from "./ProvenanceView";

afterEach(cleanup);

const match = (over: Partial<ProvenanceMatch> = {}): ProvenanceMatch => ({
  source_artifact_id: "s1",
  source_name: "Auth model",
  source_n: 2,
  source_line_from: 1,
  source_line_to: 4,
  source_text: "Auth is a signed JWT.",
  similarity: 0.82,
  strength: "close",
  changed: false,
  ...over,
});

const provenance = (over: Partial<Provenance> = {}): Provenance => ({
  artifact_id: "a1",
  n: 4,
  sources: ["Auth model"],
  affected_count: 0,
  inferred: true,
  unplaced: 0,
  error: "",
  passages: [
    {
      chunk_id: "c1",
      line_from: 1,
      line_to: 2,
      text: "The flow stores the signed JWT.",
      matches: [match()],
      affected: false,
    },
  ],
  ...over,
});

describe("saying how sure it is", () => {
  it("gives the word and the number, not one or the other", () => {
    // The word alone is an opinion with nothing behind it; the number
    // alone means nothing to a reader. Both, or the reader cannot
    // check the first against the second.
    expect(strengthLabel(match({ similarity: 0.783, strength: "close" }))).toBe(
      "close · 0.78",
    );
  });

  it("does not band the number itself", () => {
    // The cuts live in core/artifacts/provenance.py, where the
    // measurements they are argued from are written down. A second
    // copy here is the one that goes stale — this file's did, and
    // went on calling a measured derivation "loose" long after the
    // floor moved.
    expect(strengthLabel(match({ similarity: 0.74, strength: "whatever" }))).toBe(
      "whatever · 0.74",
    );
  });

  it("still shows the number when no label arrived", () => {
    expect(strengthLabel(match({ similarity: 0.9, strength: "" }))).toBe("0.90");
  });
});

describe("colouring by source", () => {
  it("gives each source its own hue", () => {
    const sources = ["Auth model", "Release checklist"];
    expect(hueFor(sources, "Auth model")).not.toBe(hueFor(sources, "Release checklist"));
  });

  it("is stable for the same source", () => {
    const sources = ["A", "B", "C"];
    expect(hueFor(sources, "B")).toBe(hueFor(sources, "B"));
  });

  it("falls back rather than throwing on an unknown source", () => {
    expect(typeof hueFor([], "nope")).toBe("number");
  });
});

describe("balancing code fences", () => {
  it("closes a fence a chunk left open", () => {
    // A passage is a slice of the document and the slicing does not
    // respect fences. Unbalanced, the fence swallows every passage
    // rendered after it.
    expect(balanceFences("```py\nx = 1")).toBe("```py\nx = 1\n```");
  });

  it("leaves a balanced passage alone", () => {
    const text = "```py\nx = 1\n```";
    expect(balanceFences(text)).toBe(text);
  });

  it("leaves prose alone", () => {
    expect(balanceFences("just words")).toBe("just words");
  });
});

describe("the legend", () => {
  it("says how much of the document each source accounts for", () => {
    // Naming the sources without the counts leaves "which one carries
    // this document" unanswered.
    // The name appears twice by design — once in the legend and once
    // on the card — so this looks in the legend specifically.
    const { container } = render(<ProvenanceView provenance={provenance()} />);
    const chip = container.querySelector(".prov-chip");
    expect(chip?.textContent).toContain("Auth model");
    expect(chip?.textContent).toContain("1 passage");
  });

  it("counts affected passages apart from the rest", () => {
    render(
      <ProvenanceView
        provenance={provenance({
          affected_count: 1,
          passages: [
            {
              chunk_id: "c1",
              line_from: 1,
              line_to: 2,
              text: "x",
              matches: [match({ changed: true })],
              affected: true,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("1 affected")).toBeTruthy();
  });

  it("says plainly when nothing traced", () => {
    render(<ProvenanceView provenance={provenance({ passages: [
      { chunk_id: "c1", line_from: 1, line_to: 1, text: "x", matches: [], affected: false },
    ] })} />);
    expect(screen.getByText(/Nothing here resembles a confirmed source/)).toBeTruthy();
  });

  it("carries the caveat once rather than on every card", () => {
    const { container } = render(<ProvenanceView provenance={provenance()} />);
    expect(container.querySelectorAll(".prov-caveat")).toHaveLength(1);
    expect(screen.getByText(/not recorded when the document was written/)).toBeTruthy();
  });
});

describe("pairing a passage with its source", () => {
  it("numbers the passage and its card the same", () => {
    // The whole mechanism: no line is drawn, so the number is how a
    // reader knows which card belongs to which sentence.
    const { container } = render(<ProvenanceView provenance={provenance()} />);
    expect(container.querySelector(".prov-marker")?.textContent).toBe("1");
    expect(container.querySelector(".prov-card-number")?.textContent).toBe("1");
  });

  it("numbers only the passages that traced", () => {
    const { container } = render(
      <ProvenanceView
        provenance={provenance({
          passages: [
            { chunk_id: "c0", line_from: 1, line_to: 1, text: "untraced", matches: [], affected: false },
            { chunk_id: "c1", line_from: 2, line_to: 2, text: "traced", matches: [match()], affected: false },
          ],
        })}
      />,
    );
    const markers = Array.from(container.querySelectorAll(".prov-marker")).map((n) => n.textContent);
    expect(markers).toEqual(["1"]);
  });

  it("points at the source's lines, not just the document", () => {
    // A citation that resolves to "somewhere in Auth model" is the
    // thing this view exists to stop being the answer.
    render(<ProvenanceView provenance={provenance()} />);
    expect(screen.getByText("v2 · L1–4")).toBeTruthy();
  });

  it("shows the source passage itself as the evidence", () => {
    render(<ProvenanceView provenance={provenance()} />);
    expect(screen.getByText("Auth is a signed JWT.")).toBeTruthy();
  });

  it("opens the source when its name is clicked", () => {
    const onOpenSource = vi.fn();
    render(<ProvenanceView provenance={provenance()} onOpenSource={onOpenSource} />);
    fireEvent.click(screen.getByRole("button", { name: "Auth model" }));
    expect(onOpenSource).toHaveBeenCalledWith("s1");
  });
});

describe("what it will not hide", () => {
  it("marks an untraced passage as untraced rather than leaving it blank", () => {
    // A gap left silent reads as "nothing to see"; named, it reads as
    // a limit of the method.
    render(
      <ProvenanceView
        provenance={provenance({
          passages: [
            { chunk_id: "c1", line_from: 1, line_to: 1, text: "x", matches: [], affected: false },
          ],
        })}
      />,
    );
    expect(screen.getByText("not traced")).toBeTruthy();
  });

  it("names the alternatives instead of converging on the top match", () => {
    // Showing only the best makes a guess look like a finding — the
    // documented failure of inline citation.
    render(
      <ProvenanceView
        provenance={provenance({
          passages: [
            {
              chunk_id: "c1",
              line_from: 1,
              line_to: 1,
              text: "x",
              matches: [match(), match({ source_line_from: 9, source_line_to: 9 })],
              affected: false,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/also resembles 1 other passage/)).toBeTruthy();
  });

  it("says when passages are still waiting to be indexed", () => {
    render(<ProvenanceView provenance={provenance({ unplaced: 3 })} />);
    expect(screen.getByText(/3 passages not indexed yet/)).toBeTruthy();
  });

  it("surfaces a refusal rather than an empty view", () => {
    render(<ProvenanceView provenance={provenance({ error: "Artifacts are disabled." })} />);
    expect(screen.getByText("Artifacts are disabled.")).toBeTruthy();
  });
});

// ── The document, read once ────────────────────────────────────────

/**
 * Line numbers and overlaps taken from the live graph: "Session
 * handling" v6 indexes as seven chunks, and three of them begin
 * mid-word ("ores", "on:", "tion") because chunks are cut to a
 * character budget, not to lines.
 */
const DOC = [
  "# Session handling", // 1
  "", // 2
  "## What the client stores", // 3
  "", // 4
  "- The refresh cookie", // 5
  "- Nothing else", // 6
  "", // 7
  "## Configuration", // 8
  "", // 9
  "```yaml", // 10
  "session:", // 11
  "  access_ttl_minutes: 15", // 12
  "```", // 13
  "", // 14
  "## Limits", // 15
].join("\n");

const passage = (over: Partial<ProvenancePassage>): ProvenancePassage => ({
  chunk_id: "c",
  line_from: 1,
  line_to: 1,
  text: "",
  matches: [],
  affected: false,
  ...over,
});

const resembles = (over: Partial<ProvenanceMatch> = {}): ProvenanceMatch => ({
  source_artifact_id: "s1",
  source_name: "Auth model",
  source_n: 2,
  source_line_from: 4,
  source_line_to: 9,
  source_text: "…",
  similarity: 0.84,
  strength: "close",
  changed: false,
  ...over,
});

describe("laying the passages out as the document", () => {
  it("shows whole lines instead of the chunker's mid-word cuts", () => {
    const rows = layOutPassages(
      [passage({ chunk_id: "a", line_from: 1, line_to: 6, text: "…client st" }),
       passage({ chunk_id: "b", line_from: 4, line_to: 15, text: "ores\n\n- The refresh" })],
      DOC,
    );

    expect(rows[1].markdown.startsWith("ores")).toBe(false);
    expect(rows[1].markdown.split("\n")[0]).toBe("");
  });

  it("shows no line twice, however far the chunks overlap", () => {
    const rows = layOutPassages(
      [passage({ chunk_id: "a", line_from: 1, line_to: 6 }),
       passage({ chunk_id: "b", line_from: 4, line_to: 15 })],
      DOC,
    );

    const all = rows.map((r) => r.markdown).join("\n");
    expect(all.match(/- The refresh cookie/g)).toHaveLength(1);
    // And between them they still show the whole document.
    expect(all.split("\n")).toHaveLength(DOC.split("\n").length);
  });

  it("opens a fence a passage starts inside of", () => {
    // The bug this replaced: the closing ``` was read as an unclosed
    // opening one, a second closing fence was appended, and the YAML
    // was parsed as prose — "access_ttl_minutes" lost its underscores
    // to emphasis.
    const rows = layOutPassages(
      [passage({ chunk_id: "a", line_from: 1, line_to: 11 }),
       passage({ chunk_id: "b", line_from: 11, line_to: 15 })],
      DOC,
    );

    const inner = rows[1].markdown;
    expect(inner.startsWith("```\n")).toBe(true);
    expect((inner.match(/```/g) ?? []).length % 2).toBe(0);
  });

  it("closes a fence a passage ends inside of", () => {
    const rows = layOutPassages([passage({ line_from: 1, line_to: 11 })], DOC);
    expect(rows[0].markdown.trimEnd().endsWith("```")).toBe(true);
  });

  it("keeps the citations of a chunk it had nothing left to show", () => {
    // Fully swallowed by the row above. Dropping it would lose a
    // source to a layout decision.
    const swallowed = resembles({ source_name: "Login flow", source_line_from: 30 });
    const rows = layOutPassages(
      [passage({ chunk_id: "a", line_from: 1, line_to: 15, matches: [resembles()] }),
       passage({ chunk_id: "b", line_from: 12, line_to: 15, matches: [swallowed], affected: true })],
      DOC,
    );

    expect(rows).toHaveLength(1);
    expect(rows[0].matches.map((m) => m.source_name)).toEqual(["Auth model", "Login flow"]);
    expect(rows[0].affected).toBe(true);
  });

  it("does not list the same source twice when folding", () => {
    const rows = layOutPassages(
      [passage({ chunk_id: "a", line_from: 1, line_to: 15, matches: [resembles()] }),
       passage({ chunk_id: "b", line_from: 12, line_to: 15, matches: [resembles()] })],
      DOC,
    );
    expect(rows[0].matches).toHaveLength(1);
  });

  it("shows the tail no chunk reached", () => {
    // Indexing that stopped early must not silently truncate the
    // document — `unplaced` says some is missing, it does not say
    // which.
    const rows = layOutPassages([passage({ line_from: 1, line_to: 6 })], DOC);
    expect(rows).toHaveLength(2);
    expect(rows[1].matches).toEqual([]);
    expect(rows.map((r) => r.markdown).join("\n").split("\n")).toHaveLength(15);
  });

  it("falls back to the chunk text when the document is not to hand", () => {
    const rows = layOutPassages([passage({ text: "raw chunk", line_to: 3 })], "");
    expect(rows[0].markdown).toBe("raw chunk");
  });
});

describe("a table the chunker cut below its header", () => {
  const TABLE = [
    "## Budgeting", // 1
    "", // 2
    "| Endpoint group | anonymous | authenticated |", // 3
    "| --- | --- | --- |", // 4
    "| reads | 120 / min | 1200 / min |", // 5
    "| writes | 10 / min | 120 / min |", // 6
    "", // 7
    "After the table.", // 8
  ].join("\n");

  it("puts the header back so the rows stay a table", () => {
    // Without this the second row rendered as literal text —
    // "| writes | 10 / min | 120 / min |" straight across the page.
    const rows = layOutPassages(
      [passage({ chunk_id: "a", line_from: 1, line_to: 5 }),
       passage({ chunk_id: "b", line_from: 5, line_to: 8 })],
      TABLE,
    );

    expect(rows[1].markdown.split("\n").slice(0, 2)).toEqual([
      "| Endpoint group | anonymous | authenticated |",
      "| --- | --- | --- |",
    ]);
  });

  it("leaves a slice that already has its header alone", () => {
    const rows = layOutPassages([passage({ line_from: 1, line_to: 6 })], TABLE);
    expect(rows[0].markdown.match(/\| Endpoint group/g)).toHaveLength(1);
  });

  it("does not hand a header to the prose after the table", () => {
    const rows = layOutPassages(
      [passage({ chunk_id: "a", line_from: 1, line_to: 7 }),
       passage({ chunk_id: "b", line_from: 7, line_to: 8 })],
      TABLE,
    );
    expect(rows[1].markdown).not.toContain("---");
  });

  it("treats a pipe inside a code block as a pipe", () => {
    // A fence wins: "repairing" this would inject two header rows
    // into somebody's shell command.
    const doc = "```\na | b\nc | d\n```\nafter";
    const rows = layOutPassages(
      [passage({ chunk_id: "a", line_from: 1, line_to: 2 }),
       passage({ chunk_id: "b", line_from: 3, line_to: 5 })],
      doc,
    );
    expect(rows[1].markdown).not.toContain("---");
    expect(rows[1].markdown.startsWith("```\n")).toBe(true);
  });
});
