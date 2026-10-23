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
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
  Row,
  Table,
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
