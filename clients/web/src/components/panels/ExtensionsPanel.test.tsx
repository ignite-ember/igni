// @vitest-environment jsdom
/**
 * The form for `contributes.settings`, and the first place the host
 * says what an extension's state is.
 *
 * The settings shape was modelled, validated against the manifest,
 * stored beside the bundle and carried on `list_extensions` — and
 * the only way to change one was to call `set_extension_settings`
 * by hand. A declared setting nobody can reach is a declaration.
 *
 * What is pinned here is mostly about *not* writing the wrong
 * thing: a cleared number input reads as `NaN`, and sending that
 * would store `null` and look like the setting had been reset by
 * somebody. The form refuses and names the field instead.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { ExtensionsPanel } from "./ExtensionsPanel";
import type { ExtensionSummary } from "../../extensions/ExtensionPage";
import { PageRegistry } from "../../chat/pageStack";

afterEach(cleanup);

function extension(over: Partial<ExtensionSummary> = {}): ExtensionSummary {
  return {
    id: "acme.scanner",
    name: "Compliance Scanner",
    version: "1.0.0",
    state: "ready",
    reason: "",
    origin: "http://127.0.0.1:41234",
    allow: [],
    pages: [],
    menu: [],
    indicators: [],
    settings: [],
    values: {},
    ...over,
  };
}

const WITH_SETTINGS = extension({
  settings: [
    {
      key: "endpoint",
      type: "string",
      label: "Endpoint",
      default: "",
      options: [],
    },
    {
      key: "retries",
      type: "number",
      label: "Retries",
      default: 3,
      options: [],
    },
    {
      key: "notify",
      type: "boolean",
      label: "Notify",
      default: true,
      options: [],
    },
    {
      key: "failOn",
      type: "enum",
      label: "Fail at or above",
      default: "high",
      options: ["critical", "high", "low"],
    },
  ],
  values: {
    endpoint: "https://acme.test",
    retries: 3,
    notify: true,
    failOn: "high",
  },
});

function panelWith(
  extensions: ExtensionSummary[],
  saved: ExtensionSummary[] = [],
) {
  const rpc = vi.fn(async () => ({ extensions: saved }));
  const onChanged = vi.fn();
  render(
    <ExtensionsPanel
      client={{ rpc } as never}
      extensions={extensions}
      onChanged={onChanged}
      onClose={() => {}}
    />,
  );
  return { rpc, onChanged };
}

describe("what is installed", () => {
  it("says so when nothing is", () => {
    panelWith([]);
    expect(screen.getByText(/No extensions installed/)).toBeTruthy();
    // Extensions come from their services through igni; installing
    // one by hand is a development install and not advice for users.
    expect(screen.getByText(/arrive from your organisation/)).toBeTruthy();
    expect(screen.queryByText(/igni ext install/)).toBeNull();
  });

  it("shows the state and the reason it is not ready", () => {
    // Before this panel a FAILED extension was a sidebar entry that
    // opened onto its own reason. There was nowhere to see three
    // extensions together and notice one had been down since boot.
    panelWith([
      extension({
        state: "failed",
        reason: "its service could not be started",
      }),
    ]);
    expect(screen.getByText("failed")).toBeTruthy();
    expect(screen.getByText(/could not be started/)).toBeTruthy();
  });

  it("offers no form for an extension that declares no settings", () => {
    panelWith([extension()]);
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("says what a collision switched off, and why", () => {
    // Two extensions replacing one prompt section: the one that lost
    // is running, and its section is not in the prompt. Without this
    // row that is a mystery to whoever installed it.
    panelWith([
      extension({
        switched_off: [
          {
            claim: "prompt:main#workflow",
            reason: "acme.review has prompt:main#workflow: decided by admin@acme.test",
          },
        ],
      }),
    ]);
    expect(screen.getByText("prompt:main#workflow")).toBeTruthy();
    expect(screen.getByText(/decided by admin@acme.test/)).toBeTruthy();
  });

  it("says which of its prompt edits land nowhere, and why", () => {
    panelWith([
      extension({
        dropped: [
          {
            claim: "prompt:main+our_review",
            reason: "acme.lead overrides main, and its prompt has no section 'workflow'",
          },
        ],
      }),
    ]);
    expect(screen.getByText("prompt:main+our_review")).toBeTruthy();
    expect(screen.getByText(/land nowhere/)).toBeTruthy();
    expect(screen.getByText(/has no section 'workflow'/)).toBeTruthy();
  });

  it("says nothing about collisions when there are none", () => {
    panelWith([extension()]);
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("says an extension is not responding, and why", () => {
    panelWith([
      extension({
        faulty:
          "Compliance Scanner isn't responding: 3 calls in a row timed out or crashed",
      }),
    ]);
    expect(screen.getByRole("alert").textContent).toMatch(
      /Not responding: Compliance Scanner isn't responding/,
    );
  });

  it("says nothing about responding while it answers", () => {
    panelWith([extension()]);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("the form", () => {
  it("renders a control per declared type", () => {
    panelWith([WITH_SETTINGS]);
    expect(screen.getByDisplayValue("https://acme.test")).toBeTruthy();
    expect(screen.getByDisplayValue("3")).toBeTruthy();
    expect(screen.getByRole("checkbox")).toBeTruthy();
    expect(screen.getByRole("combobox")).toBeTruthy();
  });

  it("starts clean, so Save is not offered until something changes", () => {
    panelWith([WITH_SETTINGS]);
    expect(
      screen.getByRole("button", { name: "Save" }).hasAttribute("disabled"),
    ).toBe(true);
  });

  it("sends only this extension's values, keyed as declared", async () => {
    const { rpc, onChanged } = panelWith([WITH_SETTINGS], [WITH_SETTINGS]);
    fireEvent.change(screen.getByDisplayValue("https://acme.test"), {
      target: { value: "https://other.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(rpc).toHaveBeenCalled());
    expect(rpc).toHaveBeenCalledWith("set_extension_settings", {
      extension_id: "acme.scanner",
      values: {
        endpoint: "https://other.test",
        retries: 3,
        notify: true,
        failOn: "high",
      },
    });
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("refuses a cleared number rather than storing nothing", async () => {
    // `valueAsNumber` on an empty input is `NaN`. Sending it would
    // store `null`, which reads as somebody having reset the
    // setting — so the form names the field and will not save.
    const { rpc } = panelWith([WITH_SETTINGS]);
    fireEvent.change(screen.getByDisplayValue("3"), { target: { value: "" } });
    await waitFor(() =>
      expect(screen.getByText(/Retries needs a number/)).toBeTruthy(),
    );
    expect(
      screen.getByRole("button", { name: "Save" }).hasAttribute("disabled"),
    ).toBe(true);
    expect(rpc).not.toHaveBeenCalled();
  });

  it("reverts to what was saved", () => {
    panelWith([WITH_SETTINGS]);
    fireEvent.change(screen.getByDisplayValue("https://acme.test"), {
      target: { value: "typed by mistake" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Revert" }));
    expect(screen.getByDisplayValue("https://acme.test")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Save" }).hasAttribute("disabled"),
    ).toBe(true);
  });

  it("shows why a save failed instead of looking like it worked", async () => {
    const rpc = vi.fn(async () => {
      throw new Error("no extension 'acme.scanner' is installed");
    });
    render(
      <ExtensionsPanel
        client={{ rpc } as never}
        extensions={[WITH_SETTINGS]}
        onChanged={() => {}}
        onClose={() => {}}
      />,
    );
    fireEvent.change(screen.getByDisplayValue("https://acme.test"), {
      target: { value: "https://other.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText(/is installed/)).toBeTruthy());
  });

  it("will not guess at a type this host does not know", () => {
    // The same rule the indicator follows for a tone it does not
    // have: rendering a guess that saves is worse than rendering
    // nothing. `type` is a plain string on the wire, so a newer
    // manifest can carry one this build has never heard of.
    panelWith([
      extension({
        settings: [
          { key: "when", type: "datetime", label: "When", options: [] },
        ],
        values: {},
      }),
    ]);
    expect(screen.getByText(/needs a newer igni/)).toBeTruthy();
  });

  it("falls back to the declared default for a key never saved", () => {
    // An upgrade adds a setting; the stored values predate it. The
    // backend fills defaults in, but a stale `values` must not
    // render the control empty either.
    panelWith([
      extension({
        settings: [
          {
            key: "depth",
            type: "number",
            label: "Depth",
            default: 7,
            options: [],
          },
        ],
        values: {},
      }),
    ]);
    expect(screen.getByDisplayValue("7")).toBeTruthy();
  });
});

describe("the page kind is igni's own", () => {
  it("cannot be contributed by an extension", () => {
    // Otherwise an extension named its page `extensions` and the
    // host's own settings surface — the one place you go to turn
    // that extension off — is the page it replaced.
    const registry = new PageRegistry();
    expect(registry.add("extensions", "acme.scanner")).toMatch(/igni's own/);
  });
});
