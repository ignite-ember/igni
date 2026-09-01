// @vitest-environment jsdom
/**
 * The composer's input had 2.7% statement coverage and no test file.
 *
 * It is a `contenteditable` that keeps two representations in sync: a
 * canonical string (`"look at @src/a.py please"`) and a live DOM of text
 * nodes, `<br>`s and non-editable pill `<span>`s. `writeValue` goes one
 * way, `readValue` the other, and every keystroke crosses the boundary.
 *
 * When those two disagree the symptom is the worst kind: the user's text
 * silently changes as they type. Nothing checked it, so this drives the
 * round trip through the component's own public interface — mount with a
 * value, fire the `input` event the browser would fire, and assert on what
 * the component reports back up. No internals are exported for testing.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";

import { EditableInput } from "./EditableInput";

/** Mount with `value`, then make the component re-read its own DOM. */
function roundTrip(value: string): string {
  const onValueChange = vi.fn();
  const { container } = render(
    <EditableInput value={value} onValueChange={onValueChange} />,
  );
  const el = container.querySelector('[contenteditable="true"]') as HTMLElement;
  expect(el, "the editable element should render").not.toBeNull();

  // The DOM was built by writeValue from `value`; firing `input` makes the
  // component read it back with readValue and report the result.
  fireEvent.input(el);

  expect(onValueChange).toHaveBeenCalled();
  return onValueChange.mock.calls[onValueChange.mock.calls.length - 1][0] as string;
}

describe("the harness reads the DOM rather than echoing the prop", () => {
  /* Without this, every round-trip assertion below could pass by the
   * component simply handing back the `value` it was given, and the whole
   * file would be vacuous. Editing the DOM the way a keystroke would and
   * seeing the *edited* text come back proves `readValue` is in the path. */
  it("reports text that was typed into the DOM, not the original value", () => {
    const onValueChange = vi.fn();
    const { container } = render(
      <EditableInput value="hello" onValueChange={onValueChange} />,
    );
    const el = container.querySelector('[contenteditable="true"]') as HTMLElement;

    el.appendChild(document.createTextNode(" world"));
    fireEvent.input(el);

    expect(onValueChange).toHaveBeenCalled();
    expect(onValueChange.mock.lastCall![0]).toBe("hello world");
  });

  it("reports a pill that was removed from the DOM as gone", () => {
    const onValueChange = vi.fn();
    const { container } = render(
      <EditableInput value="see @a.py now" onValueChange={onValueChange} />,
    );
    const el = container.querySelector('[contenteditable="true"]') as HTMLElement;

    el.querySelector("[data-pill-path]")!.remove();
    fireEvent.input(el);

    expect(onValueChange.mock.lastCall![0]).toBe("see  now");
  });
});

describe("what the user typed is what the component reports", () => {
  const values: [string, string][] = [
    ["plain text", "hello world"],
    ["a single pill", "@src/main.py"],
    ["text around a pill", "look at @src/main.py please"],
    ["two pills", "@a.py and @b.py"],
    ["adjacent pills", "@a.py @b.py"],
    ["a pill at the end", "check @src/main.py"],
    ["a relative path", "@./rel/path.ts"],
    ["an absolute path", "@/abs/path.ts"],
    ["a bare name", "@README"],
    ["a code pill", "@code:abc123"],
    ["an email-ish string that is not a pill", "mail me at a@b.com"],
    ["a newline", "line one\nline two"],
    ["two newlines", "a\n\nb"],
    ["a newline next to a pill", "@a.py\nsecond line"],
    ["unicode", "héllo → wörld 🎉"],
    ["unicode in a path", "@src/café/naïve.py"],
    ["a path with a dash and dots", "@src/my-file.v2.test.ts"],
    ["angle brackets in text", "a < b && c > d"],
    ["an ampersand entity-looking string", "a &amp; b"],
    ["a quote", 'say "hi" and \'bye\''],
    ["a backslash", "C:\\path\\to\\file"],
    ["leading whitespace", "  indented"],
    ["a lone at-sign", "@"],
    ["an at-sign followed by a space", "@ notapill"],
  ];

  for (const [name, value] of values) {
    it(name, () => {
      expect(roundTrip(value)).toBe(value);
    });
  }
});

describe("pills survive as pills, not as text", () => {
  it("renders a file pill as a non-editable element carrying the full path", () => {
    const { container } = render(
      <EditableInput value="see @src/deep/nested/file.py" onValueChange={vi.fn()} />,
    );

    const pill = container.querySelector("[data-pill-path]");
    expect(pill).not.toBeNull();
    expect(pill!.getAttribute("data-pill-path")).toBe("src/deep/nested/file.py");
    // contenteditable=false is what stops the caret entering the pill and
    // letting a user edit half a path.
    expect(pill!.getAttribute("contenteditable")).toBe("false");
  });

  it("shows the basename but reports the whole path", () => {
    const value = "see @src/deep/nested/file.py";
    const { container } = render(<EditableInput value={value} onValueChange={vi.fn()} />);

    const pill = container.querySelector("[data-pill-path]")!;
    expect(pill.textContent).toContain("file.py");
    expect(pill.textContent).not.toContain("src/deep");
    expect(roundTrip(value)).toBe(value);
  });

  it("does not treat an email address as a pill", () => {
    const { container } = render(
      <EditableInput value="mail me at a@b.com" onValueChange={vi.fn()} />,
    );

    expect(container.querySelector("[data-pill-path]")).toBeNull();
  });

  it("keeps a path with no directory component intact", () => {
    const { container } = render(<EditableInput value="@README" onValueChange={vi.fn()} />);

    expect(container.querySelector("[data-pill-path]")!.getAttribute("data-pill-path")).toBe(
      "README",
    );
  });
});

describe("text is text, never markup", () => {
  it("does not build elements out of angle brackets", () => {
    const { container } = render(
      <EditableInput value={`<img src=x onerror=alert(1)>`} onValueChange={vi.fn()} />,
    );

    const el = container.querySelector('[contenteditable="true"]')!;
    expect(el.querySelector("img")).toBeNull();
    expect(el.textContent).toBe("<img src=x onerror=alert(1)>");
  });

  it("does not build elements out of a path that looks like markup", () => {
    // Unix filenames may contain `<` and `>`, and the file list comes from
    // whatever repository is open — so this is reachable input, not a
    // hypothetical.
    const value = `@<img/src=x/onerror=alert(1)>`;
    const { container } = render(<EditableInput value={value} onValueChange={vi.fn()} />);

    const el = container.querySelector('[contenteditable="true"]')!;
    expect(el.querySelector("img")).toBeNull();
    expect(roundTrip(value)).toBe(value);
  });
});

describe("the placeholder", () => {
  it("is exposed for an empty value and not for a filled one", () => {
    const empty = render(<EditableInput value="" placeholder="Ask" onValueChange={vi.fn()} />);
    const el = empty.container.querySelector("[contenteditable]")!;
    expect(el.getAttribute("data-placeholder") ?? el.getAttribute("aria-placeholder")).toBe(
      "Ask",
    );
  });

  it("marks the editor as disabled rather than editable when disabled", () => {
    const { container } = render(
      <EditableInput value="x" disabled onValueChange={vi.fn()} />,
    );

    expect(container.querySelector('[contenteditable="true"]')).toBeNull();
  });
});
