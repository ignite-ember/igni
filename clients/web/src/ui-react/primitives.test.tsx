// @vitest-environment jsdom
/**
 * The primitives are a mapping from props to class names, so most of
 * these pin that mapping — a renamed class is invisible otherwise,
 * and it breaks every extension at once.
 *
 * `Dialog` is tested differently because it is the one that does
 * something: focus, Escape, and the backdrop. Those are the parts an
 * extension would reimplement and get wrong in ways only keyboard
 * and screen-reader users notice.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MenuIcon } from "./MenuIcon";
import {
  Actions,
  Badge,
  Button,
  Card,
  Chip,
  Dialog,
  Empty,
  Input,
  Item,
  List,
  Checkbox,
  Code,
  Divider,
  Field,
  Heading,
  Progress,
  Row,
  Select,
  Spinner,
  Switch,
  Table,
  Tabs,
  Text,
  Textarea,
  Toolbar,
} from "./primitives";

afterEach(cleanup);

describe("controls carry the classes the app uses", () => {
  it("gives a plain button just `btn`", () => {
    const { container } = render(<Button>Go</Button>);
    expect(container.querySelector("button")?.className).toBe("btn");
  });

  it("adds a variant and a size only when they are not the default", () => {
    const { container } = render(
      <Button variant="primary" size="sm">
        Go
      </Button>,
    );
    expect(container.querySelector("button")?.className).toBe(
      "btn btn-primary btn-sm",
    );
  });

  it("defaults to type=button, so a button in a form does not submit it", () => {
    const { container } = render(<Button>Go</Button>);
    expect(container.querySelector("button")?.getAttribute("type")).toBe(
      "button",
    );
  });

  it("keeps a caller's own class alongside its own", () => {
    const { container } = render(<Button className="kb-go">Go</Button>);
    expect(container.querySelector("button")?.className).toBe("btn kb-go");
  });

  it("passes everything else straight through", () => {
    const onClick = vi.fn();
    render(
      <Button onClick={onClick} disabled aria-label="Index">
        Go
      </Button>,
    );
    const btn = screen.getByLabelText("Index");
    expect(btn).toHaveProperty("disabled", true);
    fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("actually forwards the handler, which the disabled case cannot show", () => {
    // jsdom does not dispatch to a disabled button, so the test above
    // passes even against a Button that dropped onClick on the floor.
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Go</Button>);
    fireEvent.click(screen.getByText("Go"));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("marks a tone as data rather than as a class", () => {
    // A tone is a state of the thing, not a different thing. Reading
    // it off `data-tone` keeps the class list one item long.
    const { container } = render(<Badge tone="danger">3</Badge>);
    const el = container.querySelector("span");
    expect(el?.className).toBe("ui-badge");
    expect(el?.getAttribute("data-tone")).toBe("danger");
  });

  it("gives an input the field class", () => {
    const { container } = render(<Input placeholder="Search" />);
    expect(container.querySelector("input")?.className).toBe("panel-input");
  });
});

describe("layout and containers", () => {
  const cases: [string, React.ReactElement, string][] = [
    ["Row", <Row key="a" />, "ui-row"],
    ["Actions", <Actions key="b" />, "ui-actions"],
    ["List", <List key="c" />, "ui-list"],
    ["Toolbar", <Toolbar key="d" />, "ui-toolbar"],
    ["Card", <Card key="e" />, "ui-card"],
    ["Chip", <Chip key="f" />, "chip"],
  ];
  it.each(cases)("%s renders as .%s", (_name, el, cls) => {
    const { container } = render(el);
    expect(container.firstElementChild?.className).toBe(cls);
  });

  it("renders Table as a real table", () => {
    const { container } = render(<Table />);
    expect(container.querySelector("table")?.className).toBe("ui-table");
  });

  it("renders Item as a button, not a clickable div", () => {
    // Something clickable that is not a button is a keyboard trap.
    const { container } = render(<Item>Pick me</Item>);
    expect(container.querySelector("button")?.className).toBe("ui-item");
  });

  it("gives an empty state room to say what to do next", () => {
    render(<Empty action={<Button>Add one</Button>}>No documents yet.</Empty>);
    expect(screen.getByText("No documents yet.")).toBeDefined();
    expect(screen.getByText("Add one")).toBeDefined();
  });
});

describe("Dialog does the things an extension would get wrong", () => {
  function open(onClose = vi.fn()) {
    render(
      <Dialog
        title="Delete this document?"
        sub="It cannot be undone."
        onClose={onClose}
        actions={
          <>
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="danger">Delete</Button>
          </>
        }
      />,
    );
    return onClose;
  }

  it("announces itself as a modal dialog", () => {
    open();
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    // And it says which dialog. Without a name a screen reader
    // announces "dialog" and stops.
    const labelledBy = dialog.getAttribute("aria-labelledby");
    expect(labelledBy).toBeTruthy();
    expect(document.getElementById(labelledBy as string)?.textContent).toBe(
      "Delete this document?",
    );
  });

  it("takes focus, so the first Tab lands inside it", () => {
    open();
    expect(document.activeElement).toBe(screen.getByRole("dialog"));
  });

  it("closes on Escape", () => {
    const onClose = open();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  it("closes on the backdrop but not on the panel", () => {
    const onClose = open();
    fireEvent.mouseDown(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();

    // A drag that starts inside and ends outside must not close it
    // either — that would eat a text selection.
    const overlay = document.querySelector(".overlay") as HTMLElement;
    fireEvent.mouseDown(overlay);
    expect(onClose).toHaveBeenCalled();
  });

  it("wraps Tab from the last control back to the first", () => {
    open();
    const dialog = screen.getByRole("dialog");
    const buttons = dialog.querySelectorAll("button");
    const last = buttons[buttons.length - 1];
    last.focus();

    fireEvent.keyDown(dialog, { key: "Tab" });

    expect(document.activeElement).toBe(buttons[0]);
  });

  it("wraps Shift+Tab from the first back to the last", () => {
    open();
    const dialog = screen.getByRole("dialog");
    const buttons = dialog.querySelectorAll("button");
    buttons[0].focus();

    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });

    expect(document.activeElement).toBe(buttons[buttons.length - 1]);
  });

  it("gives focus back to whatever opened it", () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();

    const { unmount } = render(<Dialog title="Hi" onClose={vi.fn()} />);
    unmount();

    expect(document.activeElement).toBe(opener);
    opener.remove();
  });
});

describe("the controls a floor has to have", () => {
  it("styles a select and a textarea as fields", () => {
    const { container } = render(
      <>
        <Select>
          <option>a</option>
        </Select>
        <Textarea />
      </>,
    );
    expect(container.querySelector("select")?.className).toBe("ui-select");
    expect(container.querySelector("textarea")?.className).toBe("ui-textarea");
  });

  it("wraps a checkbox in its own label rather than matching an id", () => {
    // An id an extension forgets to make unique is a label that
    // checks somebody else's box, and nothing looks wrong when it does.
    render(<Checkbox label="Index on save" />);
    const box = screen.getByLabelText("Index on save");
    expect(box.getAttribute("type")).toBe("checkbox");
    expect(box.closest("label")).not.toBeNull();
  });

  it("makes a switch a switch, not a checkbox", () => {
    const onChange = vi.fn();
    render(<Switch checked={false} onChange={onChange} label="Live sync" />);
    const sw = screen.getByRole("switch", { name: "Live sync" });

    // The distinction a screen reader announces: a checkbox is a
    // value you submit, a switch takes effect now.
    expect(sw.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("announces a field's error rather than only showing it", () => {
    render(
      <Field label="Name" htmlFor="n" error="Already taken.">
        <Input id="n" />
      </Field>,
    );
    const err = screen.getByRole("alert");
    expect(err.textContent).toBe("Already taken.");
    // The part the original test left out, which is the whole claim:
    // something has to *reference* the id. Without this the test
    // passed against a Field that announced nothing.
    const input = screen.getByLabelText("Name");
    expect(input.getAttribute("aria-describedby")).toBe(err.id);
    expect(input.getAttribute("aria-invalid")).toBe("true");
  });

  it("prefers the error over the hint, because one of them is urgent", () => {
    render(
      <Field label="Name" htmlFor="n" hint="Lowercase." error="Already taken.">
        <Input id="n" />
      </Field>,
    );
    expect(screen.queryByText("Lowercase.")).toBeNull();
  });

  it("reports progress as a value, and as sliding when it has none", () => {
    const { container, rerender } = render(
      <Progress value={42} label="Indexing" />,
    );
    const bar = screen.getByRole("progressbar", { name: "Indexing" });
    expect(bar.getAttribute("aria-valuenow")).toBe("42");
    expect(
      container.querySelector<HTMLElement>(".ui-progress-fill")?.style.width,
    ).toBe("42%");

    rerender(<Progress label="Indexing" />);
    // No number is honest about not knowing, rather than sitting at a
    // fixed width and reading as stuck.
    expect(
      screen.getByRole("progressbar").getAttribute("aria-valuenow"),
    ).toBeNull();
    expect(
      screen.getByRole("progressbar").getAttribute("data-indeterminate"),
    ).toBe("true");
  });

  it("clamps a progress value rather than overflowing the bar", () => {
    const { container } = render(<Progress value={300} label="x" />);
    expect(
      container.querySelector<HTMLElement>(".ui-progress-fill")?.style.width,
    ).toBe("100%");
  });

  it("moves tabs with the arrow keys", () => {
    const onChange = vi.fn();
    render(
      <Tabs
        label="Views"
        active="a"
        onChange={onChange}
        tabs={[
          { id: "a", label: "Browse" },
          { id: "b", label: "Search" },
        ]}
      />,
    );
    // A tab strip that answers only to clicks is one half the people
    // cannot use.
    fireEvent.keyDown(screen.getByRole("tablist"), { key: "ArrowRight" });
    expect(onChange).toHaveBeenCalledWith("b");
  });

  it("wraps around at the ends", () => {
    // Three tabs, not two: with two, ArrowLeft and ArrowRight from
    // the first both land on the second, so the original test could
    // not tell the directions apart — or tell wrapping from an
    // implementation that always returned tabs[1].
    const onChange = vi.fn();
    render(
      <Tabs
        label="Views"
        active="a"
        onChange={onChange}
        tabs={[
          { id: "a", label: "Browse" },
          { id: "b", label: "Search" },
          { id: "c", label: "Lineage" },
        ]}
      />,
    );
    fireEvent.keyDown(screen.getByRole("tablist"), { key: "ArrowLeft" });
    expect(onChange).toHaveBeenCalledWith("c");
  });

  it("takes Home and End", () => {
    const onChange = vi.fn();
    render(
      <Tabs
        label="Views"
        active="b"
        onChange={onChange}
        tabs={[
          { id: "a", label: "Browse" },
          { id: "b", label: "Search" },
          { id: "c", label: "Lineage" },
        ]}
      />,
    );
    const strip = screen.getByRole("tablist");
    fireEvent.keyDown(strip, { key: "Home" });
    expect(onChange).toHaveBeenLastCalledWith("a");
    fireEvent.keyDown(strip, { key: "End" });
    expect(onChange).toHaveBeenLastCalledWith("c");
  });

  it("does not crash on an empty tab list", () => {
    // `tabs[(i + step + 0) % 0]` is tabs[NaN], thrown inside React's
    // event dispatch, which takes the tree down.
    render(<Tabs label="Views" active="" onChange={vi.fn()} tabs={[]} />);
    expect(() =>
      fireEvent.keyDown(screen.getByRole("tablist"), { key: "ArrowRight" }),
    ).not.toThrow();
  });

  it("points each tab at a panel, and the panel back at the tab", () => {
    // Half the ARIA pattern is worse than none: the strip announced
    // it controlled something that did not exist.
    render(
      <Tabs
        label="Views"
        active="a"
        onChange={vi.fn()}
        tabs={[{ id: "a", label: "Browse" }]}
      />,
    );
    const tab = screen.getByRole("tab");
    expect(tab.id).not.toBe("");
    expect(tab.getAttribute("aria-controls")).toBe(`${tab.id}-panel`);
  });

  it("ties a heading's size to its outline level", () => {
    // So the two cannot disagree — an h3 that looks like an h1 is a
    // document that reads differently to a screen reader than to an eye.
    const { container } = render(<Heading level={3}>Lineage</Heading>);
    const h = container.querySelector("h3");
    expect(h?.className).toBe("ui-heading");
    expect(h?.style.fontSize).toBe("var(--text-lg)");
  });

  it("gives a spinner a live region, so it is announced not just seen", () => {
    const { container } = render(<Spinner label="Loading" />);
    const s = container.querySelector(".ui-spinner");
    expect(s?.getAttribute("role")).toBe("status");
    expect(s?.getAttribute("aria-live")).toBe("polite");
    // And it has something to say. role="status" on an empty span is
    // a live region that announces silence.
    expect(s?.textContent).toBe("Loading");
  });

  it("renders text tones as data, and a divider as an hr", () => {
    const { container } = render(
      <>
        <Text tone="muted">quiet</Text>
        <Divider />
        <Code>npm</Code>
      </>,
    );
    expect(container.querySelector("p")?.getAttribute("data-tone")).toBe(
      "muted",
    );
    expect(container.querySelector("hr")?.className).toBe("ui-divider");
    expect(container.querySelector("code")?.className).toBe("ui-code");
  });
});

describe("an extension can bring its own icon", () => {
  it("draws a path it was given", () => {
    const { container } = render(<MenuIcon path="M1 1 L15 15" />);
    expect(container.querySelector("path")?.getAttribute("d")).toBe(
      "M1 1 L15 15",
    );
  });

  it("still draws nothing for a name nobody has", () => {
    const { container } = render(<MenuIcon name={"nope" as never} />);
    expect(container.querySelector("svg")).toBeNull();
  });
});

describe("a typed prop that does nothing is worse than no prop", () => {
  // Found by an engineer trying to build a panel whose whole job was
  // ranking by severity: `<Badge tone="danger">` and
  // `<Badge tone="success">` rendered identically, because nothing
  // styled the tone. The prop existed, autocompleted, and lied.
  const css = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), "../../../ui/igni-ui.css"),
    "utf8",
  );

  it("styles every badge tone the type allows", () => {
    for (const tone of ["neutral", "success", "warning", "danger"]) {
      expect(css).toContain(`.ui-badge[data-tone="${tone}"]`);
    }
  });

  it("styles every button variant the type allows", () => {
    // `btn-warn` was typed and absent from the package: inside an
    // extension frame it rendered as a plain default button while
    // TypeScript vouched for it.
    for (const variant of ["primary", "danger", "warn", "quiet"]) {
      expect(css).toContain(`.btn-${variant}`);
    }
  });

  it("gives keyboard users a focus ring on everything focusable", () => {
    // Absent from the package entirely, which would have made an
    // extension the cause of its host's accessibility findings.
    expect(css).toContain(":focus-visible");
    for (const cls of [".btn", ".ui-item", ".ui-tab", ".ui-switch"]) {
      expect(css).toContain(`${cls}:focus-visible`);
    }
  });
});
