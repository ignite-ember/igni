// @vitest-environment jsdom
/**
 * The scheduled-tasks page: what is waiting, what ran, and the one thing
 * a person can do about a task — cancel it while it is still live.
 *
 * Pinned here: the default view is the live tasks, done ones stay
 * readable behind a filter, a finished task never offers Cancel, and a
 * backend that fails the list call reads as "nothing scheduled" rather
 * than a page stuck on "Loading…".
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SchedulePanel } from "./SchedulePanel";

afterEach(cleanup);

interface Task {
  id?: string;
  description?: string;
  scheduled_at?: string;
  status?: string;
  recurrence?: string;
  result?: string;
  error?: string;
}

const TASKS: Task[] = [
  {
    id: "t-ok",
    description: "Nightly dependency audit",
    scheduled_at: "2026-11-01T02:00:00Z",
    status: "completed",
    result: "No **new** advisories.",
  },
  {
    id: "t-live",
    description: "Summarise open PRs",
    scheduled_at: "2026-11-04T09:00:00Z",
    status: "pending",
    recurrence: "every day",
  },
  {
    id: "t-bad",
    description: "Rotate the staging token",
    scheduled_at: "2026-11-02T03:00:00Z",
    status: "failed",
    error: "permission denied",
  },
  { id: "t-off", description: "Old reminder", status: "cancelled" },
  { id: "t-run", status: "running", scheduled_at: "2026-11-03T10:00:00Z" },
];

function panelWith(tasks: Task[] | Error) {
  const calls: { method: string; params: unknown }[] = [];
  const rpc = vi.fn(async (method: string, params?: unknown) => {
    calls.push({ method, params });
    if (method === "get_scheduled_tasks") {
      if (tasks instanceof Error) throw tasks;
      return tasks;
    }
    return { ok: true };
  });
  render(<SchedulePanel client={{ rpc } as never} onClose={() => {}} />);
  return { rpc, calls };
}

describe("what is scheduled", () => {
  it("asks for done tasks too, so past results stay readable", async () => {
    const { calls } = panelWith([]);
    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0]).toEqual({
      method: "get_scheduled_tasks",
      params: { include_done: true },
    });
  });

  it("says how to schedule something when nothing is", async () => {
    panelWith([]);
    expect(await screen.findByText(/Nothing scheduled/)).toBeTruthy();
    // No filters or search over an empty list.
    expect(screen.queryByPlaceholderText(/Search description/)).toBeNull();
  });

  it("reads a failed list call as nothing scheduled, not as loading forever", async () => {
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    panelWith(new Error("backend gone"));
    expect(await screen.findByText(/Nothing scheduled/)).toBeTruthy();
    expect(screen.queryByText("Loading…")).toBeNull();
    err.mockRestore();
  });

  it("shows the live tasks first and only them by default", async () => {
    panelWith(TASKS);
    await screen.findByText("Summarise open PRs");
    expect(screen.getByText("(no description)")).toBeTruthy();
    expect(screen.queryByText("Nightly dependency audit")).toBeNull();
    expect(screen.queryByText("Rotate the staging token")).toBeNull();
    expect(screen.getByText("every day")).toBeTruthy();
  });

  it("counts each band on its filter", async () => {
    panelWith(TASKS);
    await screen.findByText("Summarise open PRs");
    const count = (label: string) =>
      screen.getByRole("button", { name: new RegExp(`^${label}`) }).textContent;
    expect(count("Active")).toBe("Active2");
    expect(count("All")).toBe("All5");
    expect(count("Done")).toBe("Done1");
    expect(count("Failed")).toBe("Failed1");
    expect(count("Cancelled")).toBe("Cancelled1");
  });

  it("puts live tasks above finished ones under All, newest first within a band", async () => {
    panelWith(TASKS);
    await screen.findByText("Summarise open PRs");
    fireEvent.click(screen.getByRole("button", { name: /^All/ }));
    const names = [...document.querySelectorAll(".task-entry .name")].map(
      (n) => n.firstChild?.textContent,
    );
    expect(names).toEqual([
      "Summarise open PRs",
      "(no description)",
      "Rotate the staging token",
      "Nightly dependency audit",
      "Old reminder",
    ]);
  });

  it("filters by description or id, and says when nothing matches", async () => {
    panelWith(TASKS);
    await screen.findByText("Summarise open PRs");
    fireEvent.click(screen.getByRole("button", { name: /^All/ }));
    const search = screen.getByPlaceholderText(/Search description/);
    fireEvent.change(search, { target: { value: "T-BAD" } });
    expect(screen.getByText("Rotate the staging token")).toBeTruthy();
    expect(screen.queryByText("Summarise open PRs")).toBeNull();
    fireEvent.change(search, { target: { value: "audit" } });
    expect(screen.getByText("Nightly dependency audit")).toBeTruthy();
    fireEvent.change(search, { target: { value: "nothing like this" } });
    expect(screen.getByText("No tasks match.")).toBeTruthy();
  });
});

describe("a task's details", () => {
  it("shows a finished task's result as markdown and offers no Cancel", async () => {
    panelWith(TASKS);
    await screen.findByText("Summarise open PRs");
    fireEvent.click(screen.getByRole("button", { name: /^Done/ }));
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    fireEvent.click(screen.getByText("Nightly dependency audit"));
    expect(screen.getByText("Result")).toBeTruthy();
    expect(screen.getByText("new").tagName).toBe("STRONG");
  });

  it("shows a failed task's error", async () => {
    panelWith(TASKS);
    await screen.findByText("Summarise open PRs");
    fireEvent.click(screen.getByRole("button", { name: /^Failed/ }));
    fireEvent.click(screen.getByText("Rotate the staging token"));
    expect(screen.getByText("permission denied")).toBeTruthy();
  });

  it("says a task that has not run has no output yet, and closes again", async () => {
    panelWith(TASKS);
    fireEvent.click(await screen.findByText("Summarise open PRs"));
    expect(screen.getByText(/hasn't run/)).toBeTruthy();
    fireEvent.click(screen.getByText("Summarise open PRs"));
    expect(screen.queryByText(/hasn't run/)).toBeNull();
  });
});

describe("cancelling", () => {
  it("cancels by id without opening the row, then reloads the list", async () => {
    const { calls } = panelWith(TASKS);
    await screen.findByText("Summarise open PRs");
    const [first] = screen.getAllByRole("button", { name: "Cancel" });
    fireEvent.click(first);
    await waitFor(() =>
      expect(calls.some((c) => c.method === "cancel_scheduled_task")).toBe(true),
    );
    expect(calls.find((c) => c.method === "cancel_scheduled_task")!.params).toEqual({
      task_id: "t-live",
    });
    await waitFor(() =>
      expect(calls.filter((c) => c.method === "get_scheduled_tasks").length).toBe(2),
    );
    expect(screen.queryByText(/hasn't run/)).toBeNull();
  });

  it("sends nothing for a task without an id", async () => {
    const { calls } = panelWith([{ description: "Anonymous", status: "pending" }]);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await Promise.resolve();
    expect(calls.some((c) => c.method === "cancel_scheduled_task")).toBe(false);
  });

  it("polls while open and stops when closed", async () => {
    vi.useFakeTimers();
    try {
      const rpc = vi.fn(async () => []);
      const { unmount } = render(
        <SchedulePanel client={{ rpc } as never} onClose={() => {}} />,
      );
      expect(rpc).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(3_000);
      expect(rpc).toHaveBeenCalledTimes(2);
      unmount();
      await vi.advanceTimersByTimeAsync(9_000);
      expect(rpc).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
