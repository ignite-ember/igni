// @vitest-environment jsdom
/**
 * End-to-end smoke test for ``JsonRenderView``: pass a real json-render
 * spec through ``@json-render/react``'s ``Renderer`` and assert the
 * DOM the user would see.
 *
 * The "Apple stock chart" is our canonical demo — it exercises the
 * full render path (Card > LineGraph > SVG) with a realistic payload,
 * so regressions in the catalog, the CSS wiring, or the underlying
 * library show up as test failures instead of blank cards. Also
 * covers the Table and Metric/Grid paths for basic coverage of the
 * data-display family.
 *
 * Interactive components (Button, Input) are exercised in a separate
 * ``it`` block because they need the JSONUIProvider action wire the
 * component installs — asserting the dispatch callback gets the right
 * (action, params) tuple is what proves the round-trip is intact.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { Spec } from "@json-render/core";
import { JsonRenderView } from "./JsonRenderView";

// ── Fixtures ────────────────────────────────────────────────────────

/** AAPL 2023 monthly close — real historical data (rounded).
 *  Kept here (not fetched at test time) so tests are hermetic. */
const AAPL_2023: { x: string; y: number }[] = [
  { x: "Jan", y: 143.0 },
  { x: "Feb", y: 147.41 },
  { x: "Mar", y: 164.9 },
  { x: "Apr", y: 169.68 },
  { x: "May", y: 177.25 },
  { x: "Jun", y: 193.97 },
  { x: "Jul", y: 196.45 },
  { x: "Aug", y: 187.87 },
  { x: "Sep", y: 171.21 },
  { x: "Oct", y: 170.77 },
  { x: "Nov", y: 189.95 },
  { x: "Dec", y: 192.53 },
];

const AAPL_SPEC: Spec = {
  root: "root",
  elements: {
    root: {
      type: "Card",
      props: { title: "AAPL — Monthly Close", subtitle: "2023" },
      children: ["chart"],
    },
    chart: {
      type: "LineGraph",
      props: {
        yPrefix: "$",
        xLabel: "Month",
        yLabel: "Close",
        data: AAPL_2023,
      },
      children: [],
    },
  },
};

const TABLE_SPEC: Spec = {
  root: "root",
  elements: {
    root: {
      type: "Card",
      props: { title: "Test results" },
      children: ["t"],
    },
    t: {
      type: "Table",
      props: {
        columns: [
          { key: "name", label: "Test" },
          { key: "ms", label: "ms", align: "right" },
          { key: "status", label: "Status" },
        ],
        rows: [
          { name: "test_login", ms: 42, status: "pass" },
          { name: "test_logout", ms: 18, status: "pass" },
          { name: "test_refresh", ms: 210, status: "fail" },
        ],
      },
      children: [],
    },
  },
};

const KPI_SPEC: Spec = {
  root: "root",
  elements: {
    root: {
      type: "Grid",
      props: { columns: 3, gap: "md" },
      children: ["a", "b", "c"],
    },
    a: { type: "Metric", props: { label: "Requests", value: 12480, delta: 8.4 }, children: [] },
    b: {
      type: "Metric",
      props: { label: "p95", value: 128, suffix: "ms", delta: -3.2 },
      children: [],
    },
    c: {
      type: "Metric",
      props: { label: "Errors", value: 0.42, suffix: "%", delta: 0.1 },
      children: [],
    },
  },
};

const CONFIRM_SPEC: Spec = {
  root: "root",
  elements: {
    root: {
      type: "Card",
      props: { title: "Apply migration?" },
      children: ["row"],
    },
    row: {
      type: "Stack",
      props: { gap: "sm" },
      children: ["approve", "cancel"],
    },
    approve: {
      type: "Button",
      props: { text: "Apply now", variant: "primary" },
      children: [],
      on: {
        press: {
          action: "apply_migration",
          params: { migration_id: "0042" },
        },
      },
    },
    cancel: {
      type: "Button",
      props: { text: "Cancel", variant: "ghost" },
      children: [],
      on: {
        press: {
          action: "cancel_migration",
          params: { migration_id: "0042" },
        },
      },
    },
  },
};

// ── Tests ───────────────────────────────────────────────────────────

describe("JsonRenderView", () => {
  afterEach(cleanup);

  it("renders the AAPL LineGraph inside a titled Card", () => {
    const { container } = render(
      <JsonRenderView spec={AAPL_SPEC} title="AAPL 2023" sourceAgent="visualizer" />,
    );
    // Header
    expect(screen.getByText("AAPL 2023")).toBeTruthy();
    expect(screen.getByText("via visualizer")).toBeTruthy();
    // Card title/subtitle from the spec itself
    expect(screen.getByText("AAPL — Monthly Close")).toBeTruthy();
    expect(screen.getByText("2023")).toBeTruthy();
    // Chart SVG present with one dot per data point
    const dots = container.querySelectorAll(".jr-linechart-dot");
    expect(dots.length).toBe(AAPL_2023.length);
    // The line polyline is present
    expect(container.querySelector(".jr-linechart-line")).toBeTruthy();
    // Y-axis prefix leaks through into tick labels
    const yTicks = container.querySelectorAll(".jr-linechart-ytick");
    expect(yTicks.length).toBeGreaterThan(0);
    expect(Array.from(yTicks).some((t) => t.textContent?.startsWith("$"))).toBe(true);
  });

  it("renders a Table with headers and rows", () => {
    render(<JsonRenderView spec={TABLE_SPEC} />);
    expect(screen.getByText("Test results")).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "Test" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "Status" })).toBeTruthy();
    expect(screen.getByText("test_login")).toBeTruthy();
    expect(screen.getByText("test_refresh")).toBeTruthy();
    expect(screen.getByText("fail")).toBeTruthy();
  });

  it("renders a KPI Grid of three Metric cards", () => {
    const { container } = render(<JsonRenderView spec={KPI_SPEC} />);
    const metrics = container.querySelectorAll(".jr-metric");
    expect(metrics.length).toBe(3);
    expect(screen.getByText("Requests")).toBeTruthy();
    // Metric.value renders the number verbatim
    expect(screen.getByText("12480")).toBeTruthy();
    // Positive delta gets the "up" tone class
    const upDelta = container.querySelector(".jr-metric-up");
    expect(upDelta).toBeTruthy();
    // Negative delta gets the "down" tone class
    const downDelta = container.querySelector(".jr-metric-down");
    expect(downDelta).toBeTruthy();
  });

  it("dispatches Button actions through the onDispatchAction wire", async () => {
    const dispatch = vi.fn(async () => ({ ok: true }));
    render(<JsonRenderView spec={CONFIRM_SPEC} onDispatchAction={dispatch} />);
    const approve = screen.getByRole("button", { name: /Apply now/ });
    fireEvent.click(approve);
    // The renderer resolves the action binding and calls our handler
    // through the JSONUIProvider handlers Proxy. Give it a microtask
    // to settle in case the renderer awaits.
    await Promise.resolve();
    await Promise.resolve();
    expect(dispatch).toHaveBeenCalled();
    const call = dispatch.mock.calls[0] as unknown as [
      string,
      Record<string, unknown>,
    ];
    expect(call[0]).toBe("apply_migration");
    expect(call[1]).toMatchObject({ migration_id: "0042" });
  });

  it("falls through to an 'Unknown component' placeholder for missing types", () => {
    const spec: Spec = {
      root: "root",
      elements: {
        root: { type: "MysteryComponent", props: {}, children: [] },
      },
    };
    const { container } = render(<JsonRenderView spec={spec} />);
    const unknown = container.querySelector(".jr-unknown");
    expect(unknown?.textContent).toContain("MysteryComponent");
  });
});
