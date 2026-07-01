// @vitest-environment jsdom
/**
 * Component-render tests for ``PlanCard`` (CC parity row 50 — the
 * agent's ``exit_plan_mode`` lands here as a ChatItem).
 *
 * Uses jsdom + @testing-library/react. The rest of the vitest suite
 * runs under node; this file opts into jsdom via the directive at
 * the top so the other files don't pay the env-spin-up cost.
 *
 * What's pinned: the rendering contract the user sees + the
 * Approve/Refine button wiring. Per-state styling (CSS) and the
 * markdown body's GFM/highlight pipeline are out of scope — they're
 * library plumbing, not our logic.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { planItem, type ChatItem, type PlanTask } from "../chat/model";
import { PlanCard } from "./ChatItems";

afterEach(() => {
  cleanup();
});

type PlanChatItem = Extract<ChatItem, { kind: "plan" }>;

function makeItem(
  plan: string,
  tasks: PlanTask[] = [],
  state: "pending" | "approved" | "dismissed" = "pending",
): PlanChatItem {
  const item = planItem(plan, tasks);
  // ``planItem``'s return type is the ChatItem union; narrow it
  // here so the JSX type check passes. The factory always returns
  // a plan-kind item.
  if (item.kind !== "plan") throw new Error("planItem broke");
  return state === "pending" ? item : { ...item, state };
}

describe("PlanCard — pending state", () => {
  it("renders the plan body + both action buttons", () => {
    const item = makeItem("## Refactor auth\n\nTouch the file.");
    render(<PlanCard item={item} onApprove={() => undefined} onReject={() => undefined} />);
    // Markdown body — heading text reaches the DOM.
    expect(screen.getByText("Refactor auth")).toBeTruthy();
    // Both action buttons present.
    expect(screen.getByRole("button", { name: /approve/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /refine/i })).toBeTruthy();
  });

  it("shows the pause hint while pending", () => {
    // The hint copy ("The agent is paused…") is load-bearing UX —
    // without it, a passive user might assume the agent will
    // continue on its own.
    const item = makeItem("plan body");
    render(<PlanCard item={item} onApprove={() => undefined} onReject={() => undefined} />);
    expect(screen.getByText(/agent is paused/i)).toBeTruthy();
  });

  it("Approve click calls onApprove with the item id", () => {
    const item = makeItem("plan");
    const onApprove = vi.fn();
    render(<PlanCard item={item} onApprove={onApprove} onReject={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onApprove).toHaveBeenCalledWith(item.id);
  });

  it("Refine click calls onReject with the item id", () => {
    const item = makeItem("plan");
    const onReject = vi.fn();
    render(<PlanCard item={item} onApprove={() => undefined} onReject={onReject} />);
    fireEvent.click(screen.getByRole("button", { name: /refine/i }));
    expect(onReject).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledWith(item.id);
  });
});

describe("PlanCard — task checklist", () => {
  it("renders nothing for a prose-only plan (no tasks)", () => {
    // The ``<ul class="plan-card-tasks">`` must be absent when
    // tasks is empty — otherwise the spacing under the markdown
    // body is wrong (an empty <ul> still gets margin).
    const item = makeItem("just prose");
    const { container } = render(
      <PlanCard item={item} onApprove={() => undefined} onReject={() => undefined} />,
    );
    expect(container.querySelector("ul.plan-card-tasks")).toBeNull();
  });

  it("renders each task with the right marker per status", () => {
    // ○ pending · ● in_progress · ✓ completed — the visual
    // ladder a user scans to know "where is the agent now".
    // Locking the markers down here prevents a "neat refactor"
    // from swapping ● and ✓ and breaking the affordance.
    const tasks: PlanTask[] = [
      { content: "Read code", status: "completed", activeForm: "Reading code" },
      { content: "Run tests", status: "in_progress", activeForm: "Running tests" },
      { content: "Ship", status: "pending", activeForm: "Shipping" },
    ];
    const item = makeItem("plan with tasks", tasks);
    const { container } = render(
      <PlanCard item={item} onApprove={() => undefined} onReject={() => undefined} />,
    );
    const items = container.querySelectorAll("li.plan-card-task");
    expect(items).toHaveLength(3);
    const text = (i: number) => items[i].textContent ?? "";
    expect(text(0)).toContain("✓");
    expect(text(0)).toContain("Read code");
    expect(text(1)).toContain("●");
    // While in_progress, activeForm is shown, NOT content.
    expect(text(1)).toContain("Running tests");
    expect(text(1)).not.toContain("Run tests");
    expect(text(2)).toContain("○");
    expect(text(2)).toContain("Ship");
  });

  it("falls back to content when activeForm is empty for an in-progress task", () => {
    // The agent occasionally submits a task with no activeForm
    // (todo-tool sloppiness). Showing nothing would be worse
    // than showing the imperative form.
    const tasks: PlanTask[] = [
      { content: "Just do it", status: "in_progress", activeForm: "" },
    ];
    const item = makeItem("plan", tasks);
    const { container } = render(
      <PlanCard item={item} onApprove={() => undefined} onReject={() => undefined} />,
    );
    const li = container.querySelector("li.plan-card-task");
    expect(li?.textContent).toContain("Just do it");
  });
});

describe("PlanCard — non-pending states", () => {
  it("approved state hides buttons and shows the approved footer", () => {
    // After Approve, the plan stays visible (it's a
    // conversation artifact) but the buttons must vanish to
    // signal that the decision is locked in.
    const item = makeItem("plan", [], "approved");
    render(<PlanCard item={item} onApprove={() => undefined} onReject={() => undefined} />);
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /refine/i })).toBeNull();
    expect(screen.getByText(/plan approved/i)).toBeTruthy();
  });

  it("dismissed state hides buttons and shows the dismissed footer", () => {
    const item = makeItem("plan", [], "dismissed");
    render(<PlanCard item={item} onApprove={() => undefined} onReject={() => undefined} />);
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.getByText(/plan dismissed/i)).toBeTruthy();
  });

  it("approved/dismissed states drop the pause hint", () => {
    // Hint is for pending only — once decided, the agent is no
    // longer paused. Showing it would be misleading.
    const item = makeItem("plan", [], "approved");
    render(<PlanCard item={item} onApprove={() => undefined} onReject={() => undefined} />);
    expect(screen.queryByText(/agent is paused/i)).toBeNull();
  });

  it("state-specific CSS class is applied (approved variant)", () => {
    // The variant class drives the green/dimmed footer color.
    // Useful for visual regression even without snapshots.
    const item = makeItem("plan", [], "approved");
    const { container } = render(
      <PlanCard item={item} onApprove={() => undefined} onReject={() => undefined} />,
    );
    const root = container.querySelector(".plan-card");
    expect(root?.className).toContain("plan-card--approved");
  });
});
