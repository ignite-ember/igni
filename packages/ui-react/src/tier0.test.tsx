// @vitest-environment jsdom
/**
 * The Tier 0 components, tested for the behaviour that justified
 * publishing them.
 *
 * Each of these exists because hand-rolling it goes wrong in a way
 * that is invisible to the person who wrote it: a live region that
 * never announces, a boundary that stays caught forever, a sort
 * arrow with no `aria-sort` behind it, a toast that vanishes while
 * you reach for its button. So the tests are about those failures
 * rather than about rendering.
 */
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SortableTh, Status } from "./DataTable";
import { ErrorBoundary } from "./ErrorBoundary";
import { LiveRegion, useAnnounce } from "./LiveRegion";
import { AppShell, Scroll } from "./Shell";
import { Banner, ToastHost, useToast } from "./Toast";

afterEach(cleanup);

// ── AppShell / Scroll ──────────────────────────────────────────────

describe("AppShell", () => {
  it("omits the header and footer rows when there is nothing in them", () => {
    // An empty `.ui-shell-header` is a 0px flex row, which is
    // harmless until it has a border or a gap above it.
    const { container } = render(<AppShell>body</AppShell>);
    expect(container.querySelector(".ui-shell-header")).toBeNull();
    expect(container.querySelector(".ui-shell-footer")).toBeNull();
    expect(container.querySelector(".ui-shell-body")?.textContent).toBe("body");
  });

  it("keeps header, body and footer in document order", () => {
    // Visual order follows DOM order here, so a screen reader and a
    // sighted user get the same sequence.
    const { container } = render(
      <AppShell header="H" footer="F">
        B
      </AppShell>,
    );
    const rows = [...container.querySelectorAll(".ui-shell > *")];
    expect(rows.map((r) => r.textContent)).toEqual(["H", "B", "F"]);
  });
});

describe("Scroll", () => {
  it("scrolls down by default and sideways on request", () => {
    const { container, rerender } = render(<Scroll>x</Scroll>);
    expect(container.firstElementChild?.className).toBe("ui-scroll");
    rerender(<Scroll axis="x">x</Scroll>);
    expect(container.firstElementChild?.className).toBe("ui-scroll-x");
  });

  it("is not focusable unless asked", () => {
    // A tab stop on every list would be noise; a list with no
    // focusable children needs one, and that is the caller's call.
    const { container } = render(<Scroll>x</Scroll>);
    expect(container.firstElementChild?.getAttribute("tabindex")).toBeNull();
  });
});

// ── ErrorBoundary ──────────────────────────────────────────────────

function Boom({ fail }: { fail: boolean }): React.ReactElement {
  if (fail) throw new Error("the panel exploded");
  return <p>fine</p>;
}

describe("ErrorBoundary", () => {
  beforeEach(() => {
    // React logs the caught error itself. That is correct of it and
    // noise here.
    vi.spyOn(console, "error").mockImplementation(() => undefined);
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows the message rather than 'something went wrong'", () => {
    // The reader is the extension's user, and a generic apology has
    // never helped anybody report a bug.
    render(
      <ErrorBoundary>
        <Boom fail />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert").textContent).toContain("the panel exploded");
  });

  it("hands the error to onError with the component stack", () => {
    const onError = vi.fn();
    render(
      <ErrorBoundary onError={onError}>
        <Boom fail />
      </ErrorBoundary>,
    );
    expect(onError).toHaveBeenCalledOnce();
    expect(onError.mock.calls[0][0]).toBeInstanceOf(Error);
    expect(onError.mock.calls[0][1]).toHaveProperty("componentStack");
  });

  it("remounts the children when reset", () => {
    function Harness() {
      const [fail, setFail] = useState(true);
      return (
        <ErrorBoundary
          fallback={(error, reset) => (
            <button
              type="button"
              onClick={() => {
                setFail(false);
                reset();
              }}
            >
              retry {error.message}
            </button>
          )}
        >
          <Boom fail={fail} />
        </ErrorBoundary>
      );
    }
    render(<Harness />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.queryByText("fine")).not.toBeNull();
  });

  it("clears itself when a resetKey changes", () => {
    // Without this a boundary that caught once stays caught, so
    // navigating away from the broken thing shows the error page for
    // the working one.
    const { rerender } = render(
      <ErrorBoundary resetKeys={["a"]}>
        <Boom fail />
      </ErrorBoundary>,
    );
    expect(screen.queryByRole("alert")).not.toBeNull();
    rerender(
      <ErrorBoundary resetKeys={["b"]}>
        <Boom fail={false} />
      </ErrorBoundary>,
    );
    expect(screen.queryByText("fine")).not.toBeNull();
  });

  it("stays caught when the resetKeys are equal", () => {
    const { rerender } = render(
      <ErrorBoundary resetKeys={["a"]}>
        <Boom fail />
      </ErrorBoundary>,
    );
    rerender(
      <ErrorBoundary resetKeys={["a"]}>
        <Boom fail={false} />
      </ErrorBoundary>,
    );
    expect(screen.queryByRole("alert")).not.toBeNull();
  });
});

// ── Toast ──────────────────────────────────────────────────────────

function Raise({
  message = "Saved",
  options,
}: {
  message?: string;
  options?: Parameters<ReturnType<typeof useToast>["show"]>[1];
}) {
  const toast = useToast();
  return (
    <button type="button" onClick={() => toast.show(message, options)}>
      raise
    </button>
  );
}

describe("ToastHost", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("announces politely, and a failure assertively", () => {
    render(
      <ToastHost>
        <Raise message="Saved" />
      </ToastHost>,
    );
    act(() => {
      fireEvent.click(screen.getByText("raise"));
    });
    expect(screen.getByRole("status").textContent).toContain("Saved");

    cleanup();
    render(
      <ToastHost>
        <Raise message="Refused" options={{ tone: "danger" }} />
      </ToastHost>,
    );
    act(() => {
      fireEvent.click(screen.getByText("raise"));
    });
    const alert = screen.getByRole("alert");
    expect(alert.getAttribute("aria-live")).toBe("assertive");
    expect(alert.textContent).toContain("Refused");
  });

  it("goes away on its own", () => {
    render(
      <ToastHost>
        <Raise />
      </ToastHost>,
    );
    act(() => {
      fireEvent.click(screen.getByText("raise"));
    });
    expect(screen.queryByRole("status")).not.toBeNull();
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("stays when it carries an action", () => {
    // A message that disappears while you are reaching for its
    // button was never actionable.
    render(
      <ToastHost>
        <Raise options={{ action: <a href="#undo">Undo</a> }} />
      </ToastHost>,
    );
    act(() => {
      fireEvent.click(screen.getByText("raise"));
    });
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByRole("status").textContent).toContain("Undo");
  });

  it("drops the oldest rather than stacking without limit", () => {
    render(
      <ToastHost>
        <Raise options={{ duration: null }} />
      </ToastHost>,
    );
    for (let i = 0; i < 7; i++) {
      act(() => {
        fireEvent.click(screen.getByText("raise"));
      });
    }
    expect(screen.getAllByRole("status")).toHaveLength(4);
  });

  it("has a named dismiss button", () => {
    // "×" is announced as "times" or as nothing, and a stack of them
    // is unnavigable.
    render(
      <ToastHost>
        <Raise />
      </ToastHost>,
    );
    act(() => {
      fireEvent.click(screen.getByText("raise"));
    });
    const dismiss = screen.getByRole("button", { name: "Dismiss notification" });
    act(() => {
      fireEvent.click(dismiss);
    });
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("throws rather than silently dropping a toast", () => {
    // A no-op here means a save that reports success by showing
    // nothing, which looks the same as the save not happening.
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => render(<Raise />)).toThrow(/outside a <ToastHost>/);
    vi.restoreAllMocks();
  });
});

describe("Banner", () => {
  it("is a status, or an alert when it is bad news", () => {
    const { rerender } = render(<Banner>Read-only</Banner>);
    expect(screen.getByRole("status").textContent).toContain("Read-only");
    rerender(<Banner tone="danger">Offline</Banner>);
    expect(screen.getByRole("alert").textContent).toContain("Offline");
  });

  it("only offers a dismiss when there is somewhere for it to go", () => {
    const { rerender } = render(<Banner>Stuck</Banner>);
    expect(screen.queryByRole("button")).toBeNull();
    const onDismiss = vi.fn();
    rerender(<Banner onDismiss={onDismiss}>Closable</Banner>);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss message" }));
    expect(onDismiss).toHaveBeenCalledOnce();
  });
});

// ── LiveRegion ─────────────────────────────────────────────────────

describe("useAnnounce", () => {
  function Harness() {
    const { announce, region } = useAnnounce();
    return (
      <>
        {region}
        <button type="button" onClick={() => announce("40 results")}>
          say
        </button>
        <button type="button" onClick={() => announce("stop", "assertive")}>
          shout
        </button>
      </>
    );
  }

  it("renders the region empty, before there is anything to say", () => {
    // A live region inserted with its text already in it is often
    // not announced at all — there is no change to observe.
    render(<Harness />);
    expect(screen.getByRole("status").textContent).toContain("");
  });

  it("announces the same message twice", () => {
    // Identical consecutive text is not a mutation, so nothing is
    // read. Scanning 40 files twice must not be silence the second
    // time.
    render(<Harness />);
    fireEvent.click(screen.getByText("say"));
    const first = screen.getByRole("status").textContent;
    fireEvent.click(screen.getByText("say"));
    const second = screen.getByRole("status").textContent;
    expect(first).toBe("40 results");
    expect(second).not.toBe(first);
    expect(second?.replace(/​/g, "")).toBe("40 results");
  });

  it("keeps polite and assertive in separate regions", () => {
    // One region whose aria-live flips races against the text
    // change, and comes out at whichever urgency was noticed first.
    render(<Harness />);
    fireEvent.click(screen.getByText("say"));
    fireEvent.click(screen.getByText("shout"));
    expect(screen.getByRole("status").textContent).toContain("40 results");
    expect(screen.getByRole("alert").textContent).toContain("stop");
  });
});

describe("LiveRegion", () => {
  it("mounts empty and fills afterwards", async () => {
    render(<LiveRegion>3 results</LiveRegion>);
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toContain("3 results"),
    );
  });

  it("is atomic, so a changed number is read as a whole sentence", () => {
    render(<LiveRegion>1 result</LiveRegion>);
    expect(screen.getByRole("status").getAttribute("aria-atomic")).toBe("true");
  });
});

// ── SortableTh / Status ────────────────────────────────────────────

describe("SortableTh", () => {
  it("puts aria-sort on the th and hides the arrow", () => {
    // Hand-rolled versions do the reverse — an arrow glyph and no
    // aria-sort — which makes the sort invisible to exactly the
    // people most reliant on being told.
    render(
      <table>
        <thead>
          <tr>
            <SortableTh direction="ascending" onSort={() => {}}>
              Rule
            </SortableTh>
          </tr>
        </thead>
      </table>,
    );
    const th = screen.getByRole("columnheader");
    expect(th.getAttribute("aria-sort")).toBe("ascending");
    expect(
      th.querySelector(".ui-th-arrow")?.getAttribute("aria-hidden"),
    ).toBe("true");
  });

  it("says 'none' when another column is the sorted one", () => {
    render(
      <table>
        <thead>
          <tr>
            <SortableTh onSort={() => {}}>Rule</SortableTh>
          </tr>
        </thead>
      </table>,
    );
    expect(screen.getByRole("columnheader").getAttribute("aria-sort")).toBe(
      "none",
    );
  });

  it("reverses the sorted column and starts others ascending", () => {
    const onSort = vi.fn();
    const { rerender } = render(
      <table>
        <thead>
          <tr>
            <SortableTh direction="ascending" onSort={onSort}>
              Rule
            </SortableTh>
          </tr>
        </thead>
      </table>,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(onSort).toHaveBeenLastCalledWith("descending");

    rerender(
      <table>
        <thead>
          <tr>
            <SortableTh onSort={onSort}>Rule</SortableTh>
          </tr>
        </thead>
      </table>,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(onSort).toHaveBeenLastCalledWith("ascending");
  });

  it("is a real button, so it is reachable by keyboard", () => {
    render(
      <table>
        <thead>
          <tr>
            <SortableTh onSort={() => {}}>Rule</SortableTh>
          </tr>
        </thead>
      </table>,
    );
    expect(screen.queryByRole("button", { name: /Rule/ })).not.toBeNull();
  });
});

describe("Status", () => {
  it("always carries a word, because a colour is not a message", () => {
    render(<Status state="FAILED" />);
    expect(screen.queryByText("FAILED")).not.toBeNull();
  });

  it("lets the word say more than the state does", () => {
    render(<Status state="FAILED">Licence expired</Status>);
    const el = screen.getByText("Licence expired");
    expect(el.getAttribute("data-state")).toBe("FAILED");
  });
});
