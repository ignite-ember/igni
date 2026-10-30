// @vitest-environment jsdom
/**
 * `Split` and `Markdown` — the two Tier 1 components that were
 * measured rather than reasoned.
 *
 * `examples/release-notes` hand-rolled both and said so in its
 * README, which is what moved them to the front of the list ahead of
 * `Combobox` — the component previously marked "hardest and
 * most-wanted", and which neither extension built so far has wanted.
 *
 * jsdom has no layout, so `getBoundingClientRect` is zero everywhere
 * and the drag arithmetic cannot be exercised here. What can be, and
 * what matters, is the part a hand-rolled version gets wrong: the
 * separator's ARIA, the keyboard, and that nothing is persisted.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Markdown } from "./Markdown";
import { Split } from "./Split";

afterEach(cleanup);

// ── Split ──────────────────────────────────────────────────────────

function split(props: Partial<Parameters<typeof Split>[0]> = {}) {
  return render(
    <Split aside={<p>list</p>} label="Resize the list" {...props}>
      <p>document</p>
    </Split>,
  );
}

describe("Split", () => {
  it("is a separator that says where it is", () => {
    // A focusable separator that never reports a value tells a
    // screen-reader user that something is adjustable and not what
    // it is adjusted to. The app's own handle omits this, which is
    // part of why it was not simply published as it stood.
    split({ defaultWidth: 300, min: 160, max: 640 });
    const handle = screen.getByRole("separator", { name: "Resize the list" });
    expect(handle.getAttribute("aria-valuenow")).toBe("300");
    expect(handle.getAttribute("aria-valuemin")).toBe("160");
    expect(handle.getAttribute("aria-valuemax")).toBe("640");
    expect(handle.getAttribute("aria-orientation")).toBe("vertical");
  });

  it("is reachable by keyboard", () => {
    // A 5px strip is a hard target with a trackpad and an impossible
    // one without a pointer.
    split();
    expect(screen.getByRole("separator").getAttribute("tabindex")).toBe("0");
  });

  it("widens and narrows with the arrow keys", () => {
    const onResize = vi.fn();
    split({ defaultWidth: 300, onResize });
    const handle = screen.getByRole("separator");

    fireEvent.keyDown(handle, { key: "ArrowRight" });
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(onResize).toHaveBeenCalledTimes(2);
  });

  it("jumps to the ends with Home and End", () => {
    const onResize = vi.fn();
    split({ min: 200, max: 500, onResize });
    const handle = screen.getByRole("separator");

    fireEvent.keyDown(handle, { key: "Home" });
    expect(onResize).toHaveBeenLastCalledWith(200);

    fireEvent.keyDown(handle, { key: "End" });
    // Clamped to a share of the window as well as to `max`, so this
    // asserts the direction rather than the number.
    expect(onResize.mock.calls.at(-1)?.[0]).toBeGreaterThan(200);
  });

  it("ignores keys that are not its own", () => {
    const onResize = vi.fn();
    split({ onResize });
    fireEvent.keyDown(screen.getByRole("separator"), { key: "a" });
    expect(onResize).not.toHaveBeenCalled();
  });

  it("goes back to where it started on a double click", () => {
    const onResize = vi.fn();
    split({ defaultWidth: 280, onResize });
    fireEvent.doubleClick(screen.getByRole("separator"));
    expect(onResize).toHaveBeenLastCalledWith(280);
  });

  it("persists nothing", () => {
    // The deliberate difference from the app's version. An extension
    // is served from an ephemeral port, a port is part of an origin,
    // so its localStorage is wiped whenever the backend restarts —
    // persisting there would be a promise this cannot keep.
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    split({ onResize: () => {} });
    fireEvent.keyDown(screen.getByRole("separator"), { key: "ArrowRight" });
    expect(setItem).not.toHaveBeenCalled();
    setItem.mockRestore();
  });

  it("puts the aside first, or last", () => {
    const { container, rerender } = split();
    const order = () =>
      [...container.querySelectorAll(".ui-split > *")].map((el) =>
        el.className.replace("ui-split-", ""),
      );
    expect(order()).toEqual(["aside", "handle", "main"]);

    rerender(
      <Split aside={<p>list</p>} label="Resize" side="end">
        <p>document</p>
      </Split>,
    );
    expect(order()).toEqual(["main", "handle", "aside"]);
  });

  it("gives each instance its own width variable", () => {
    // Two splits on one page would otherwise fight over a single
    // custom property.
    const { container } = render(
      <>
        <Split aside={<p>a</p>} label="One">
          <p>x</p>
        </Split>
        <Split aside={<p>b</p>} label="Two">
          <p>y</p>
        </Split>
      </>,
    );
    const vars = [...container.querySelectorAll("[data-split-var]")].map((el) =>
      el.getAttribute("data-split-var"),
    );
    expect(new Set(vars).size).toBe(2);
  });
});

// ── Markdown ───────────────────────────────────────────────────────

describe("Markdown", () => {
  it("keeps block structure, which is the whole point", () => {
    // `MarkdownInline` renders this as one run of text with middots.
    // That is right for a preview and exactly wrong for a document.
    const { container } = render(
      <Markdown>{"# Title\n\n- one\n- two\n\nA paragraph."}</Markdown>,
    );
    expect(container.querySelector("h1")?.textContent).toBe("Title");
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("p")?.textContent).toBe("A paragraph.");
  });

  it("is wrapped in the published prose class", () => {
    // Which is what makes prose in an extension look like prose in
    // igni — the gap that made a hand-rolled renderer useless even
    // once it worked.
    const { container } = render(<Markdown>text</Markdown>);
    expect(container.firstElementChild?.className).toContain("ui-prose");
  });

  it("renders GitHub-flavoured tables, because people write them", () => {
    const { container } = render(
      <Markdown>{"| a | b |\n| - | - |\n| 1 | 2 |"}</Markdown>,
    );
    expect(container.querySelectorAll("td")).toHaveLength(2);
  });

  it("opens links with noopener", () => {
    // A document can arrive from a colleague, so the href is
    // somebody else's. Without `noopener` the opened page gets a
    // handle on this frame and can navigate it elsewhere.
    render(<Markdown>{"[a link](https://example.test)"}</Markdown>);
    const link = screen.getByRole("link");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("does not render raw HTML", () => {
    // `rehype-raw` in a surface that renders text somebody else
    // wrote is a script tag waiting to happen.
    const { container } = render(
      <Markdown>{'<img src="x" onerror="alert(1)">'}</Markdown>,
    );
    expect(container.querySelector("img")).toBeNull();
  });

  it("renders an empty document without falling over", () => {
    const { container } = render(<Markdown>{""}</Markdown>);
    expect(container.firstElementChild?.className).toContain("ui-prose");
  });
});
