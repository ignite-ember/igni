// @vitest-environment jsdom
/**
 * Tests for ``HitlArgsView`` — the renderer the permission dialog
 * uses to show each tool argument in a human-readable shape rather
 * than a raw JSON dump.
 *
 * The component is a router: it picks a render strategy per
 * (key-name, value-type). Each branch is small but adds up — a
 * regression on the file-path detector means a user sees
 * ``/Users/dz/long/absolute/path.py`` as plain text instead of
 * the file pill, and can't easily check the basename.
 *
 * Tests pin the BRANCH chosen, not every pixel — the visible markup
 * is the affordance the user reads.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { HitlArgsView } from "./HitlArgsView";

afterEach(() => {
  cleanup();
});

describe("HitlArgsView — empty / sentinel inputs", () => {
  it("renders nothing for undefined args", () => {
    // The dialog calls this unconditionally for every requirement;
    // tool calls with no args (e.g. ``get_status``) must collapse
    // silently rather than show an empty wrapper.
    const { container } = render(<HitlArgsView args={undefined} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing for an empty object", () => {
    const { container } = render(<HitlArgsView args={{}} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders one row per key for a generic object", () => {
    // Sanity: each key/value pair must appear in the rendered
    // tree exactly once.
    const { container } = render(
      <HitlArgsView args={{ alpha: "one", beta: 2 }} />,
    );
    const rows = container.querySelectorAll(".hitl-arg-row");
    expect(rows.length).toBe(2);
  });
});

describe("HitlArgsView — primitive value rendering", () => {
  it("null/undefined values render as the em-dash placeholder", () => {
    // The agent occasionally passes ``null`` for optional fields.
    // ``— `` reads as "intentionally empty"; rendering "null" or
    // "undefined" leaks JS into UI copy.
    const { container } = render(
      <HitlArgsView args={{ optional_field: null }} />,
    );
    expect(container.querySelector(".hitl-arg-null")?.textContent).toBe("—");
  });

  it("booleans render with the bool-class wrapper", () => {
    const { container } = render(<HitlArgsView args={{ dry_run: true }} />);
    const node = container.querySelector(".hitl-arg-bool");
    expect(node?.textContent).toBe("true");
  });

  it("numbers render with the num-class wrapper", () => {
    const { container } = render(<HitlArgsView args={{ count: 42 }} />);
    const node = container.querySelector(".hitl-arg-num");
    expect(node?.textContent).toBe("42");
  });

  it("short single-line strings render as a code chip (compact)", () => {
    const { container } = render(<HitlArgsView args={{ id: "abc-1" }} />);
    // Code chip → ``.hitl-arg-code`` (inline, not the multi-line
    // pre block).
    expect(container.querySelector(".hitl-arg-code")?.textContent).toBe("abc-1");
    expect(container.querySelector(".hitl-arg-pre")).toBeNull();
  });

  it("long strings (>=80 chars) escape the code chip into a pre block", () => {
    // Threshold transition: the chip's max width can't hold an
    // 80-char string cleanly, so we promote it to a multi-line
    // pre. Anchor on a value just past the boundary.
    const longVal = "x".repeat(80);
    const { container } = render(<HitlArgsView args={{ note: longVal }} />);
    expect(container.querySelector(".hitl-arg-pre")?.textContent).toBe(longVal);
    expect(container.querySelector(".hitl-arg-code")).toBeNull();
  });

  it("multi-line strings always render as pre, even when short", () => {
    // Embedded newlines mean the value is structurally not "a
    // chip" — the chip would collapse the line breaks.
    const { container } = render(
      <HitlArgsView args={{ blurb: "one\ntwo" }} />,
    );
    expect(container.querySelector(".hitl-arg-pre")?.textContent).toContain(
      "\n",
    );
  });
});

describe("HitlArgsView — key-pattern routing", () => {
  // The component branches on key NAME (not value) to pick a
  // render strategy. Each branch is one regex — refactoring
  // those regexes is the most likely source of silent
  // regressions.

  it("``file_path`` key renders the file pill with basename + full path", () => {
    // Two strings are visible: the basename (large) and the
    // full path (small / tooltip). A user verifying a tool call
    // wants both — basename for scan, full path for precision.
    const { container } = render(
      <HitlArgsView args={{ file_path: "/Users/dz/proj/src/foo.py" }} />,
    );
    const pill = container.querySelector(".hitl-file");
    expect(pill).toBeTruthy();
    expect(container.querySelector(".hitl-file-name")?.textContent).toBe(
      "foo.py",
    );
    expect(container.querySelector(".hitl-file-path")?.textContent).toBe(
      "/Users/dz/proj/src/foo.py",
    );
  });

  it("``path`` key (bare) also triggers the file pill branch", () => {
    // The agent uses ``path`` for some tools (read/list/glob),
    // ``file_path`` for others (edit/write). Both must route
    // the same way or the dialog looks inconsistent.
    const { container } = render(
      <HitlArgsView args={{ path: "src/bar.ts" }} />,
    );
    expect(container.querySelector(".hitl-file")).toBeTruthy();
    expect(container.querySelector(".hitl-file-name")?.textContent).toBe(
      "bar.ts",
    );
  });

  it("``command`` key renders as a shell-styled block", () => {
    // Shells get a distinct visual style (monospace + dark
    // background usually) so the user can scan for "is this a
    // destructive shell" at a glance.
    const { container } = render(
      <HitlArgsView args={{ command: "rm -rf /tmp/scratch" }} />,
    );
    expect(container.querySelector(".hitl-shell")?.textContent).toBe(
      "rm -rf /tmp/scratch",
    );
  });

  it("``cmd`` key is the case-insensitive alias for command", () => {
    const { container } = render(<HitlArgsView args={{ cmd: "ls -l" }} />);
    expect(container.querySelector(".hitl-shell")?.textContent).toBe("ls -l");
  });

  it("``contents`` key promotes the value to a pre block regardless of length", () => {
    // Short content still goes in a pre (not the chip), so a
    // file-write tool with a one-liner body still reads as "this
    // is a file body" rather than "this is a misc string".
    const { container } = render(
      <HitlArgsView args={{ contents: "short" }} />,
    );
    expect(container.querySelector(".hitl-arg-pre")?.textContent).toBe("short");
    expect(container.querySelector(".hitl-arg-code")).toBeNull();
  });

  it("``body`` / ``text`` / ``prompt`` / ``query`` all route to pre", () => {
    // The same content-block branch — any of these key names
    // means "this is the meat of the call". Pinning each so a
    // future refactor of the regex doesn't drop one.
    for (const key of ["body", "text", "prompt", "query"]) {
      const { container, unmount } = render(
        <HitlArgsView args={{ [key]: "v" }} />,
      );
      expect(container.querySelector(".hitl-arg-pre")?.textContent).toBe("v");
      unmount();
    }
  });
});

describe("HitlArgsView — array values", () => {
  it("array of primitives renders as mini-tags", () => {
    // Tag-cloud style: scannable at a glance. JSON would be
    // overkill for ``files: ["a", "b"]``.
    const { container } = render(
      <HitlArgsView args={{ files: ["a.py", "b.py"] }} />,
    );
    const tags = container.querySelectorAll(".mini-tag");
    expect(tags).toHaveLength(2);
    expect(tags[0].textContent).toBe("a.py");
    expect(tags[1].textContent).toBe("b.py");
  });

  it("array of mixed primitives still renders as tags", () => {
    // Numbers + strings is a common shape (e.g. ``[1, \"a\"]``);
    // promoting it to JSON would lose the scan-friendliness.
    const { container } = render(<HitlArgsView args={{ ids: [1, "two", 3] }} />);
    const tags = container.querySelectorAll(".mini-tag");
    expect(tags).toHaveLength(3);
  });

  it("array of objects falls back to JSON pre", () => {
    // Once we have nested structure, the tag-cloud breaks down —
    // there's no good tag-string for ``{a:1}``. JSON is the
    // honest fallback.
    const { container } = render(
      <HitlArgsView args={{ items: [{ a: 1 }, { b: 2 }] }} />,
    );
    expect(container.querySelector(".hitl-arg-pre")).toBeTruthy();
    expect(container.querySelector(".mini-tag")).toBeNull();
  });
});

describe("HitlArgsView — Edit-pair (old_string + new_string)", () => {
  it("renders the diff strip when both keys are strings", () => {
    // The most-used Edit tool's args. Showing them as two
    // separate rows ("old_string: ..." / "new_string: ...")
    // would be much harder to read than the side-by-side
    // delete/add pair.
    const { container } = render(
      <HitlArgsView
        args={{
          file_path: "src/x.py",
          old_string: "return 1",
          new_string: "return 2",
        }}
      />,
    );
    const del = container.querySelector(".hitl-diff-del");
    const add = container.querySelector(".hitl-diff-add");
    expect(del?.textContent).toBe("return 1");
    expect(add?.textContent).toBe("return 2");
  });

  it("hides the source keys (no duplication of old_string/new_string rows)", () => {
    // If both the rows AND the diff render, the user sees the
    // same content three times. Pin that the source keys are
    // filtered out.
    const { container } = render(
      <HitlArgsView
        args={{
          file_path: "src/x.py",
          old_string: "before",
          new_string: "after",
        }}
      />,
    );
    const rowLabels = Array.from(
      container.querySelectorAll(".hitl-arg-key"),
    ).map((n) => n.textContent);
    expect(rowLabels).toContain("file path");
    expect(rowLabels).toContain("change");
    expect(rowLabels).not.toContain("old string");
    expect(rowLabels).not.toContain("new string");
  });

  it("uses '(empty)' placeholder when a side of the diff is blank", () => {
    // A pure insert / delete edit has an empty side. Showing
    // a blank pre confuses "is this an empty string or is the
    // dialog broken?". The placeholder makes the intent
    // explicit.
    const { container } = render(
      <HitlArgsView
        args={{
          file_path: "src/x.py",
          old_string: "",
          new_string: "added line",
        }}
      />,
    );
    expect(container.querySelector(".hitl-diff-del")?.textContent).toBe(
      "(empty)",
    );
    expect(container.querySelector(".hitl-diff-add")?.textContent).toBe(
      "added line",
    );
  });

  it("does NOT trigger the diff strip when only one of the pair is a string", () => {
    // Both must be strings to be a real Edit pair. ``old_string``
    // alone with no ``new_string`` is some other tool's arg —
    // render normally (short single-line string → code chip).
    const { container } = render(
      <HitlArgsView args={{ old_string: "alone" }} />,
    );
    expect(container.querySelector(".hitl-diff-del")).toBeNull();
    // The fallback for ``old_string`` outside an Edit pair is
    // the generic short-string branch — code chip, not the
    // diff styling.
    expect(container.querySelector(".hitl-arg-code")?.textContent).toBe(
      "alone",
    );
  });
});

describe("HitlArgsView — key label formatting", () => {
  it("converts snake_case key to spaced label", () => {
    // ``source_file_path`` → ``source file path`` reads cleanly;
    // raw snake_case is jarring at the visual top of a row.
    const { container } = render(
      <HitlArgsView args={{ source_file_path: "x.py" }} />,
    );
    expect(container.querySelector(".hitl-arg-key")?.textContent).toBe(
      "source file path",
    );
  });
});
