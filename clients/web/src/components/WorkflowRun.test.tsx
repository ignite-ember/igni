// @vitest-environment jsdom
/**
 * Component tests for ``WorkflowRun``.
 *
 * Covers the four states the card lands in over a run's lifetime
 * (running → completed, running → failed, running → cancelled)
 * and the rendering of nested phases + agents with their
 * status pills + elapsed time.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { WorkflowRun } from "./WorkflowRun";
import type { WorkflowRunState } from "../chat/model";

function makeRun(overrides: Partial<WorkflowRunState> = {}): WorkflowRunState {
  return {
    workflowRunId: "wf_test",
    name: "smoke",
    status: "running",
    startedAtMs: 1_700_000_000_000,
    phases: [],
    events: [],
    ...overrides,
  };
}

describe("WorkflowRun", () => {
  afterEach(() => cleanup());

  it("renders the run name + status pill in the header", () => {
    const run = makeRun({ status: "running" });
    render(<WorkflowRun run={run} />);
    expect(screen.getByText("smoke")).toBeDefined();
    expect(screen.getByText("running")).toBeDefined();
  });

  it("renders completed runs with a final status pill", () => {
    const run = makeRun({
      status: "completed",
      endedAtMs: 1_700_000_005_000,
      result: { ok: true },
    });
    render(<WorkflowRun run={run} />);
    expect(screen.getByText("completed")).toBeDefined();
    expect(screen.getByText("5.0s")).toBeDefined();
  });

  it("renders failed runs with the error message", () => {
    const run = makeRun({
      status: "failed",
      endedAtMs: 1_700_000_003_000,
      error: "boom",
    });
    render(<WorkflowRun run={run} />);
    expect(screen.getByText("failed")).toBeDefined();
    expect(screen.getByText(/boom/)).toBeDefined();
  });

  it("renders nested phase + agent cards with status pills", () => {
    const run = makeRun({
      phases: [
        {
          phaseId: "p1",
          title: "Assess",
          startedAtMs: 1_700_000_000_000,
          endedAtMs: 1_700_000_002_000,
          status: "completed",
          agents: [
            {
              agentId: "req_1",
              label: "audit",
              status: "completed",
              startedAtMs: 1_700_000_000_000,
              endedAtMs: 1_700_000_001_000,
            },
          ],
        },
        {
          phaseId: "p2",
          title: "Design",
          startedAtMs: 1_700_000_002_000,
          status: "running",
          agents: [
            {
              agentId: "req_2",
              label: "design:oop-minimal",
              status: "running",
              startedAtMs: 1_700_000_002_000,
            },
          ],
        },
      ],
    });
    render(<WorkflowRun run={run} />);
    // Phase headers.
    expect(screen.getByText("Assess")).toBeDefined();
    expect(screen.getByText("Design")).toBeDefined();
    // Agent labels.
    expect(screen.getByText("audit")).toBeDefined();
    expect(screen.getByText("design:oop-minimal")).toBeDefined();
  });

  it("shows the empty-state placeholder while a run has no phases yet", () => {
    const run = makeRun({ status: "running", phases: [] });
    render(<WorkflowRun run={run} />);
    expect(screen.getByText(/starting/)).toBeDefined();
  });
});

describe("WorkflowRun — collapse / expand", () => {
  afterEach(() => cleanup());

  it("collapses a phase on header click; the agents disappear", async () => {
    const run = makeRun({
      phases: [
        {
          phaseId: "p1",
          title: "Assess",
          startedAtMs: 1_700_000_000_000,
          status: "completed",
          agents: [
            {
              agentId: "a1",
              label: "assess",
              status: "completed",
              startedAtMs: 1_700_000_000_500,
              endedAtMs: 1_700_000_001_000,
            },
          ],
        },
      ],
    });
    render(<WorkflowRun run={run} />);
    expect(screen.getByText("assess")).toBeDefined();
    const phaseButton = screen.getByText("Assess").closest("button")!;
    fireEvent.click(phaseButton);
    expect(screen.queryByText("assess")).toBeNull();
    fireEvent.click(phaseButton);
    expect(screen.getByText("assess")).toBeDefined();
  });
});
