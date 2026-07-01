// @vitest-environment jsdom
/**
 * Component tests for ``HitlDialog`` — the permission prompt that
 * surfaces when a tool call requires user approval. Critical UX
 * because users see this dialog every time the agent hits an
 * un-allowed tool; mis-firing the wrong button (Allow always vs
 * Allow once) silently widens the sandbox in a way the user
 * can't easily undo.
 *
 * The four button choices match the BE's HITLResponseBatch
 * vocabulary:
 *   • "once"    → permit this single call only
 *   • "always"  → add a permanent allow rule (cross-session)
 *   • "similar" → allow this tool with similar args
 *   • ""        → reject (any choice string when action="reject")
 *
 * The dialog steps through every requirement one at a time, then
 * submits the batch via onResolve in ONE call. That's the
 * load-bearing behaviour — submitting mid-batch would mean the BE
 * unblocks partially while the user still has choices to make.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { HitlDialog } from "./HitlDialog";
import type { HITLRequest } from "../protocol/messages";

afterEach(() => {
  cleanup();
});

function req(overrides: Partial<HITLRequest> = {}): HITLRequest {
  return {
    type: "hitl_request",
    requirement_id: "req-1",
    tool_name: "shell.run",
    friendly_name: "Run shell",
    tool_args: { command: "ls" },
    details: "",
    agent_path: "",
    ...overrides,
  };
}

describe("HitlDialog — single requirement", () => {
  it("renders 'Allow <friendly_name>?' as the title", () => {
    // friendly_name is the human-readable label the BE assigns
    // (e.g. "Run shell" rather than "shell.run"). Falling back
    // to tool_name is fine but we should prefer friendly when
    // present — pin the precedence.
    render(<HitlDialog requirements={[req()]} onResolve={() => undefined} />);
    expect(screen.getByText(/Allow Run shell\?/)).toBeTruthy();
  });

  it("falls back to tool_name when friendly_name is empty", () => {
    render(
      <HitlDialog
        requirements={[req({ friendly_name: "" })]}
        onResolve={() => undefined}
      />,
    );
    expect(screen.getByText(/Allow shell\.run\?/)).toBeTruthy();
  });

  it("does NOT show the N/M counter when only one requirement is pending", () => {
    // The counter is noise for the common case; only surfaces
    // when there's actually a batch.
    const { container } = render(
      <HitlDialog requirements={[req()]} onResolve={() => undefined} />,
    );
    // ``1/1`` would match a stray date or something — anchor on
    // the title bar only.
    expect(container.textContent).not.toMatch(/\b1\/1\b/);
  });

  it("renders the agent_path sub-line when set", () => {
    // agent_path identifies WHICH sub-agent (in a team run) is
    // asking for permission. Without it, a user with multiple
    // specialists running can't tell who triggered the prompt.
    render(
      <HitlDialog
        requirements={[req({ agent_path: "main > planner > test-runner" })]}
        onResolve={() => undefined}
      />,
    );
    expect(screen.getByText("main > planner > test-runner")).toBeTruthy();
  });

  it("renders the details sub-line when set", () => {
    render(
      <HitlDialog
        requirements={[req({ details: "Wants to read /etc/hosts" })]}
        onResolve={() => undefined}
      />,
    );
    expect(screen.getByText("Wants to read /etc/hosts")).toBeTruthy();
  });

  it("shows all four action buttons", () => {
    // Reject must be visually distinct (btn-danger). The three
    // confirm flavors map to BE choice strings — drift here
    // means the BE gets the wrong allow-rule type.
    render(<HitlDialog requirements={[req()]} onResolve={() => undefined} />);
    expect(screen.getByRole("button", { name: "Allow once" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Always allow" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Allow similar" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });

  it("'Allow once' resolves with action=confirm + choice=once", () => {
    const onResolve = vi.fn();
    render(<HitlDialog requirements={[req()]} onResolve={onResolve} />);
    fireEvent.click(screen.getByRole("button", { name: "Allow once" }));
    expect(onResolve).toHaveBeenCalledWith([
      { requirement_id: "req-1", action: "confirm", choice: "once" },
    ]);
  });

  it("'Always allow' resolves with choice=always", () => {
    // The dangerous-permanence button — must NOT silently map
    // to "once" or vice versa. A swap here is invisible in the
    // UI but persists allow rules the user didn't intend.
    const onResolve = vi.fn();
    render(<HitlDialog requirements={[req()]} onResolve={onResolve} />);
    fireEvent.click(screen.getByRole("button", { name: "Always allow" }));
    expect(onResolve).toHaveBeenCalledWith([
      { requirement_id: "req-1", action: "confirm", choice: "always" },
    ]);
  });

  it("'Allow similar' resolves with choice=similar", () => {
    const onResolve = vi.fn();
    render(<HitlDialog requirements={[req()]} onResolve={onResolve} />);
    fireEvent.click(screen.getByRole("button", { name: "Allow similar" }));
    expect(onResolve).toHaveBeenCalledWith([
      { requirement_id: "req-1", action: "confirm", choice: "similar" },
    ]);
  });

  it("'Reject' resolves with action=reject + empty choice", () => {
    const onResolve = vi.fn();
    render(<HitlDialog requirements={[req()]} onResolve={onResolve} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onResolve).toHaveBeenCalledWith([
      { requirement_id: "req-1", action: "reject", choice: "" },
    ]);
  });
});

describe("HitlDialog — acceptEdits shortcut", () => {
  // When the agent asks for permission on a file-edit tool AND
  // the session is in default mode, an extra button surfaces:
  // *"Accept all edits during this session"*. Clicking it
  // confirms every remaining req in the batch AND fires the
  // ``onAcceptEditsThisRun`` callback so the parent can ``/accept on``.

  const SHORTCUT_LABEL = "Accept all edits during this session";

  it("shows the shortcut button for edit tools in default mode", () => {
    render(
      <HitlDialog
        requirements={[req({ tool_name: "edit_file", friendly_name: "Edit" })]}
        onResolve={() => undefined}
        currentMode="default"
        onAcceptEditsThisRun={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: SHORTCUT_LABEL })).toBeTruthy();
  });

  it("shows the shortcut for catalog-name edit tools too (Edit/Write/NotebookEdit)", () => {
    // Both the internal Agno function name (``edit_file``) and
    // the friendly catalog name (``Edit``) appear in the wild —
    // the BE may send either depending on tool source. Both
    // must trigger the shortcut.
    for (const toolName of ["Edit", "Write", "NotebookEdit", "save_file", "create_file"]) {
      cleanup();
      render(
        <HitlDialog
          requirements={[req({ tool_name: toolName, friendly_name: toolName })]}
          onResolve={() => undefined}
          currentMode="default"
          onAcceptEditsThisRun={() => undefined}
        />,
      );
      expect(
        screen.getByRole("button", { name: SHORTCUT_LABEL }),
        `expected shortcut for ${toolName}`,
      ).toBeTruthy();
    }
  });

  it("HIDES the shortcut for non-edit tools", () => {
    // Bash is the most common non-edit tool that hits HITL.
    // Showing the acceptEdits shortcut there would be misleading
    // — acceptEdits doesn't auto-approve shell calls.
    render(
      <HitlDialog
        requirements={[req({ tool_name: "run_shell_command", friendly_name: "Bash" })]}
        onResolve={() => undefined}
        currentMode="default"
        onAcceptEditsThisRun={() => undefined}
      />,
    );
    expect(screen.queryByRole("button", { name: SHORTCUT_LABEL })).toBeNull();
  });

  it("HIDES the shortcut when the session is already in acceptEdits mode", () => {
    // Redundant — that mode already auto-approves edits, so a
    // HITL prompt for an edit shouldn't even appear. Defensive
    // gate in case it does.
    render(
      <HitlDialog
        requirements={[req({ tool_name: "edit_file", friendly_name: "Edit" })]}
        onResolve={() => undefined}
        currentMode="acceptEdits"
        onAcceptEditsThisRun={() => undefined}
      />,
    );
    expect(screen.queryByRole("button", { name: SHORTCUT_LABEL })).toBeNull();
  });

  it("HIDES the shortcut when the session is in plan mode", () => {
    // Plan mode is the opposite intent — going from plan into
    // acceptEdits would override the user's plan-mode decision
    // mid-dialog. Refuse to offer the shortcut here.
    render(
      <HitlDialog
        requirements={[req({ tool_name: "edit_file", friendly_name: "Edit" })]}
        onResolve={() => undefined}
        currentMode="plan"
        onAcceptEditsThisRun={() => undefined}
      />,
    );
    expect(screen.queryByRole("button", { name: SHORTCUT_LABEL })).toBeNull();
  });

  it("HIDES the shortcut when no onAcceptEditsThisRun callback is wired", () => {
    // The callback is the only way to enable the mode — without
    // it, the button would be inert.
    render(
      <HitlDialog
        requirements={[req({ tool_name: "edit_file" })]}
        onResolve={() => undefined}
        currentMode="default"
      />,
    );
    expect(screen.queryByRole("button", { name: SHORTCUT_LABEL })).toBeNull();
  });

  it("shows the shortcut when currentMode is empty (back-compat with older statuses)", () => {
    // Older BEs may not stamp ``permission_mode`` on every
    // StatusUpdate. Treat missing as default — better to
    // optimistically show the shortcut than hide it on every
    // session until status arrives.
    render(
      <HitlDialog
        requirements={[req({ tool_name: "edit_file" })]}
        onResolve={() => undefined}
        currentMode=""
        onAcceptEditsThisRun={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: SHORTCUT_LABEL })).toBeTruthy();
  });

  it("clicking the shortcut fires the callback AND resolves with all reqs confirmed", () => {
    const onResolve = vi.fn();
    const onAcceptEditsThisRun = vi.fn();
    const batch = [
      req({ requirement_id: "r1", tool_name: "edit_file" }),
      req({ requirement_id: "r2", tool_name: "edit_file" }),
      req({ requirement_id: "r3", tool_name: "edit_file" }),
    ];
    render(
      <HitlDialog
        requirements={batch}
        onResolve={onResolve}
        currentMode="default"
        onAcceptEditsThisRun={onAcceptEditsThisRun}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: SHORTCUT_LABEL }));

    expect(onAcceptEditsThisRun).toHaveBeenCalledTimes(1);
    expect(onResolve).toHaveBeenCalledTimes(1);
    // All three should be confirmed in one shot.
    expect(onResolve).toHaveBeenCalledWith([
      { requirement_id: "r1", action: "confirm", choice: "once" },
      { requirement_id: "r2", action: "confirm", choice: "once" },
      { requirement_id: "r3", action: "confirm", choice: "once" },
    ]);
  });

  it("preserves prior decisions when the shortcut fires mid-batch", () => {
    // User answers req 1 with Reject, then on req 2 clicks the
    // shortcut. The first decision must survive, the rest auto-confirm.
    const onResolve = vi.fn();
    const batch = [
      req({ requirement_id: "r1", tool_name: "run_shell_command", friendly_name: "Bash" }),
      req({ requirement_id: "r2", tool_name: "edit_file" }),
      req({ requirement_id: "r3", tool_name: "edit_file" }),
    ];
    render(
      <HitlDialog
        requirements={batch}
        onResolve={onResolve}
        currentMode="default"
        onAcceptEditsThisRun={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.click(screen.getByRole("button", { name: SHORTCUT_LABEL }));

    expect(onResolve).toHaveBeenCalledTimes(1);
    expect(onResolve).toHaveBeenCalledWith([
      { requirement_id: "r1", action: "reject", choice: "" },
      { requirement_id: "r2", action: "confirm", choice: "once" },
      { requirement_id: "r3", action: "confirm", choice: "once" },
    ]);
  });
});

describe("HitlDialog — empty list", () => {
  it("renders nothing for an empty requirements list", () => {
    // The parent can pass `[]` momentarily while the BE clears
    // a resolved batch. The dialog must collapse silently
    // rather than rendering an empty card.
    const { container } = render(
      <HitlDialog requirements={[]} onResolve={() => undefined} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe("HitlDialog — batch step-through", () => {
  // The trickiest behaviour: the dialog walks the user through
  // each pending requirement in turn, accumulates their
  // decisions, and only calls onResolve once with the FULL
  // batch. Submitting on every click would unblock the BE
  // halfway through and break the resume-paused-run flow.

  const batch: HITLRequest[] = [
    req({ requirement_id: "r1", friendly_name: "First op" }),
    req({ requirement_id: "r2", friendly_name: "Second op" }),
    req({ requirement_id: "r3", friendly_name: "Third op" }),
  ];

  it("renders the N/M counter for a multi-requirement batch", () => {
    render(<HitlDialog requirements={batch} onResolve={() => undefined} />);
    // Looking up by class isn't reliable — Allow-once is on
    // index 0 so the counter should show 1/3 next to it.
    expect(screen.getByText("1/3")).toBeTruthy();
  });

  it("first click does NOT call onResolve; counter advances", () => {
    const onResolve = vi.fn();
    render(<HitlDialog requirements={batch} onResolve={onResolve} />);
    fireEvent.click(screen.getByRole("button", { name: "Allow once" }));
    // Mid-batch — must not have resolved yet.
    expect(onResolve).not.toHaveBeenCalled();
    // Counter now reads 2/3 + the title changes to the next
    // requirement.
    expect(screen.getByText("2/3")).toBeTruthy();
    expect(screen.getByText(/Allow Second op\?/)).toBeTruthy();
  });

  it("only the LAST click fires onResolve, with the full batch in order", () => {
    const onResolve = vi.fn();
    render(<HitlDialog requirements={batch} onResolve={onResolve} />);
    fireEvent.click(screen.getByRole("button", { name: "Allow once" }));
    fireEvent.click(screen.getByRole("button", { name: "Always allow" }));
    expect(onResolve).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    // ONE call total — never one-per-decision.
    expect(onResolve).toHaveBeenCalledTimes(1);
    expect(onResolve).toHaveBeenCalledWith([
      { requirement_id: "r1", action: "confirm", choice: "once" },
      { requirement_id: "r2", action: "confirm", choice: "always" },
      { requirement_id: "r3", action: "reject", choice: "" },
    ]);
  });

  it("mixing confirm and reject choices in the batch is supported", () => {
    // Real-world flow: user lets one tool through, rejects the
    // next risky one. The dialog must not bail or short-circuit
    // on the first reject; both must land in the batch.
    const onResolve = vi.fn();
    render(<HitlDialog requirements={batch} onResolve={onResolve} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.click(screen.getByRole("button", { name: "Allow similar" }));
    fireEvent.click(screen.getByRole("button", { name: "Allow once" }));
    expect(onResolve).toHaveBeenCalledTimes(1);
    const batchPassed = onResolve.mock.calls[0][0];
    expect(batchPassed.map((d: { action: string }) => d.action)).toEqual([
      "reject",
      "confirm",
      "confirm",
    ]);
  });
});
