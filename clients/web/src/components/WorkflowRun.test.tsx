// @vitest-environment jsdom
/**
 * Component tests for ``WorkflowRun``.
 *
 * Covers the four states the card lands in over a run's lifetime
 * (running → completed, running → failed, running → cancelled)
 * and the rendering of nested phases + agents with their
 * status pills + elapsed time.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
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

describe("WorkflowRun — Rerun + Cancel actions", () => {
  afterEach(() => cleanup());

  it("shows a Cancel button while running; clicking it calls onCancel", () => {
    const onCancel = vi.fn();
    const run = makeRun({ status: "running" });
    render(<WorkflowRun run={run} onCancel={onCancel} />);
    const cancelBtn = screen.getByTestId("wf-cancel");
    expect(cancelBtn).toBeDefined();
    fireEvent.click(cancelBtn);
    expect(onCancel).toHaveBeenCalledWith(run);
  });

  it("hides the Cancel button when the run is not running", () => {
    const onCancel = vi.fn();
    const run = makeRun({ status: "completed" });
    render(<WorkflowRun run={run} onCancel={onCancel} />);
    expect(screen.queryByTestId("wf-cancel")).toBeNull();
  });

  it("shows a Rerun button on failure; clicking it calls onRerun with the run", () => {
    const onRerun = vi.fn();
    const run = makeRun({
      status: "failed",
      error: "boom",
    });
    render(<WorkflowRun run={run} onRerun={onRerun} />);
    const rerunBtn = screen.getByTestId("wf-rerun");
    expect(rerunBtn).toBeDefined();
    fireEvent.click(rerunBtn);
    expect(onRerun).toHaveBeenCalledWith(run);
  });

  it("shows a Rerun button on cancellation too", () => {
    const onRerun = vi.fn();
    const run = makeRun({ status: "cancelled" });
    render(<WorkflowRun run={run} onRerun={onRerun} />);
    expect(screen.getByTestId("wf-rerun")).toBeDefined();
  });

  it("hides the Rerun button on success", () => {
    const onRerun = vi.fn();
    const run = makeRun({ status: "completed" });
    render(<WorkflowRun run={run} onRerun={onRerun} />);
    expect(screen.queryByTestId("wf-rerun")).toBeNull();
  });

  it("renders the run-level error banner with the error message", () => {
    const run = makeRun({ status: "failed", error: "boom — agent timeout" });
    render(<WorkflowRun run={run} />);
    const banner = screen.getByTestId("wf-run-error");
    expect(banner).toBeDefined();
    expect(banner.textContent).toContain("boom — agent timeout");
  });

  it("renders a 'X failed' chip in the header when any agent failed", () => {
    const run = makeRun({
      status: "failed",
      phases: [
        {
          phaseId: "p1",
          title: "Design",
          startedAtMs: 1,
          endedAtMs: 2,
          status: "completed",
          agents: [
            {
              agentId: "a1",
              label: "ok-agent",
              status: "completed",
              startedAtMs: 1,
              endedAtMs: 2,
            },
            {
              agentId: "a2",
              label: "bad-agent",
              status: "failed",
              startedAtMs: 1,
              endedAtMs: 2,
              error: "boom",
            },
          ],
        },
      ],
    });
    render(<WorkflowRun run={run} />);
    const failedChip = screen.getByTestId("wf-failed-count");
    expect(failedChip.textContent).toContain("1 failed");
  });

  it("renders per-phase failure count + error message under the phase header", () => {
    const run = makeRun({
      status: "failed",
      phases: [
        {
          phaseId: "p1",
          title: "Design",
          startedAtMs: 1,
          endedAtMs: 2,
          status: "failed",
          agents: [
            {
              agentId: "a1",
              label: "d-1",
              status: "failed",
              startedAtMs: 1,
              endedAtMs: 2,
              error: "free-function-with-state-arg still present",
            },
          ],
        },
      ],
    });
    render(<WorkflowRun run={run} />);
    const phaseFailed = screen.getByTestId("wf-phase-failed");
    expect(phaseFailed.textContent).toContain("1 failed");
    const phaseErrors = screen.getByTestId("wf-phase-errors");
    expect(phaseErrors.textContent).toContain("d-1");
    expect(phaseErrors.textContent).toContain(
      "free-function-with-state-arg still present",
    );
  });
});
