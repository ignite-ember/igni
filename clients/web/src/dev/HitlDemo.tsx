/**
 * UI sandbox for the HITL permission dialog.
 *
 * Reachable at ``?demo=hitl`` (see main.tsx). Renders the
 * ``HitlDialog`` in every meaningful variant so layout iterations
 * can happen against canned data instead of standing up a real BE
 * + agent + paused tool call. The demo is the spec — when you
 * change the dialog's actions layout, this page is what you
 * eyeball.
 *
 * Scenarios:
 *   • Single edit-tool prompt in default mode → acceptEdits
 *     shortcut row visible.
 *   • Batch of three edit prompts → counter + shortcut.
 *   • Shell-tool prompt → NO shortcut (acceptEdits doesn't cover
 *     shell).
 *   • Edit prompt while session is already in acceptEdits →
 *     shortcut hidden (redundant).
 *
 * The shortcut click logs to the on-page console pane so you can
 * confirm it fires both the resolve batch AND the "this run only"
 * callback the parent uses to revert when streaming ends.
 */

import { useState } from "react";
import { HitlDialog, type HitlDecision } from "../components/HitlDialog";
import type { HITLRequest } from "../protocol/messages";

function req(overrides: Partial<HITLRequest> = {}): HITLRequest {
  return {
    type: "hitl_request",
    requirement_id: "req-1",
    tool_name: "edit_file",
    friendly_name: "Edit",
    tool_args: {
      file_path: "src/ember_code/cli.py",
      old_string: '"""CLI interface for igni."""',
      new_string: "# ember CLI entry point\n" + '"""CLI interface for igni."""',
    },
    details: "",
    agent_path: "",
    ...overrides,
  };
}

interface Scenario {
  key: string;
  label: string;
  description: string;
  requirements: HITLRequest[];
  currentMode: string;
  withShortcut: boolean;
}

const SCENARIOS: Scenario[] = [
  {
    key: "single-edit-default",
    label: "Single edit · default mode",
    description:
      "The common case the user just installed the new affordance for. Shortcut row is visible.",
    requirements: [req()],
    currentMode: "default",
    withShortcut: true,
  },
  {
    key: "batch-edits",
    label: "Three edits · default mode",
    description:
      "A multi-file refactor. Counter reads 1/3; clicking the shortcut should auto-confirm all three.",
    requirements: [
      req({
        requirement_id: "r1",
        tool_args: { file_path: "QUICKSTART.md", old_string: "", new_string: "header\n" },
      }),
      req({
        requirement_id: "r2",
        tool_args: { file_path: "CHANGELOG.md", old_string: "", new_string: "header\n" },
      }),
      req({
        requirement_id: "r3",
        tool_args: { file_path: "README.md", old_string: "", new_string: "header\n" },
      }),
    ],
    currentMode: "default",
    withShortcut: true,
  },
  {
    key: "shell-no-shortcut",
    label: "Shell · default mode",
    description:
      "Bash isn't covered by acceptEdits mode — the shortcut row must be hidden so we don't suggest a gate the user thinks applies to shell.",
    requirements: [
      req({
        requirement_id: "shell-1",
        tool_name: "run_shell_command",
        friendly_name: "Bash",
        tool_args: { command: "rg --files src/ | wc -l" },
      }),
    ],
    currentMode: "default",
    withShortcut: false,
  },
  {
    key: "edit-already-accept",
    label: "Edit · already in acceptEdits",
    description:
      "Defensive: an edit-tool HITL shouldn't normally fire when acceptEdits is on, but if it does, the shortcut is redundant. Row hidden.",
    requirements: [req()],
    currentMode: "acceptEdits",
    withShortcut: false,
  },
  {
    key: "edit-plan-mode",
    label: "Edit · plan mode",
    description:
      "User explicitly chose plan mode; suggesting acceptEdits mid-flow would override that intent. Row hidden.",
    requirements: [req()],
    currentMode: "plan",
    withShortcut: false,
  },
];

interface LogEntry {
  ts: number;
  text: string;
}

export function HitlDemo() {
  const [scenarioKey, setScenarioKey] = useState(SCENARIOS[0].key);
  const [log, setLog] = useState<LogEntry[]>([]);
  // Reset key forces the dialog to remount on Reset / scenario change
  // so the internal index resets to 0 cleanly.
  const [resetCount, setResetCount] = useState(0);

  const scenario = SCENARIOS.find((s) => s.key === scenarioKey) ?? SCENARIOS[0];

  const append = (text: string) =>
    setLog((prev) => [{ ts: 1_000 + prev.length, text }, ...prev].slice(0, 20));

  const handleResolve = (decisions: HitlDecision[]) => {
    append(
      `resolve: [${decisions.map((d) => `${d.requirement_id}:${d.action}/${d.choice || "—"}`).join(", ")}]`,
    );
    setResetCount((c) => c + 1);
  };

  return (
    <div style={{ padding: 24, fontFamily: "system-ui, sans-serif", color: "var(--fg)" }}>
      <h1 style={{ marginTop: 0 }}>HITL dialog · UI sandbox</h1>
      <p style={{ color: "var(--fg-faint)", marginTop: -8 }}>
        Drives <code>HitlDialog</code> with canned scenarios so layout iterations don't need
        a paused agent. Pick a scenario; the dialog renders inline below.
      </p>

      <div style={{ display: "flex", gap: 16, alignItems: "flex-start", marginTop: 24 }}>
        {/* Scenario picker */}
        <div style={{ minWidth: 280, flexShrink: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Scenarios</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {SCENARIOS.map((s) => (
              <button
                key={s.key}
                onClick={() => {
                  setScenarioKey(s.key);
                  setResetCount((c) => c + 1);
                  setLog([]);
                }}
                style={{
                  textAlign: "left",
                  padding: "8px 10px",
                  borderRadius: 6,
                  border:
                    s.key === scenarioKey
                      ? "1px solid var(--accent, #4a9eff)"
                      : "1px solid var(--border, #333)",
                  background: s.key === scenarioKey ? "var(--bg-elev, #1a1f2e)" : "transparent",
                  color: "inherit",
                  cursor: "pointer",
                  fontSize: 13,
                }}
              >
                <div style={{ fontWeight: 600 }}>{s.label}</div>
                <div style={{ color: "var(--fg-faint)", fontSize: 12, marginTop: 2 }}>
                  {s.description}
                </div>
                <div style={{ marginTop: 4, fontSize: 11, color: "var(--fg-faint)" }}>
                  shortcut: <strong>{s.withShortcut ? "visible" : "hidden"}</strong>
                </div>
              </button>
            ))}
          </div>
          <button
            onClick={() => {
              setResetCount((c) => c + 1);
              setLog([]);
            }}
            style={{
              marginTop: 12,
              padding: "6px 10px",
              borderRadius: 6,
              border: "1px solid var(--border, #333)",
              background: "transparent",
              color: "inherit",
              cursor: "pointer",
              fontSize: 12,
            }}
          >
            Reset dialog
          </button>
        </div>

        {/* The dialog itself */}
        <div style={{ flexGrow: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
            Dialog · mode <code>{scenario.currentMode}</code>
          </div>
          <HitlDialog
            key={`${scenario.key}-${resetCount}`}
            requirements={scenario.requirements}
            onResolve={handleResolve}
            currentMode={scenario.currentMode}
            onAcceptEditsThisRun={() =>
              append("onAcceptEditsThisRun fired — parent should `/accept on` + arm revert")
            }
          />

          <div style={{ marginTop: 24 }}>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Event log</div>
            <div
              style={{
                background: "var(--bg-elev, #15171f)",
                border: "1px solid var(--border, #333)",
                borderRadius: 6,
                padding: 10,
                fontFamily: "ui-monospace, Menlo, monospace",
                fontSize: 12,
                minHeight: 100,
                color: "var(--fg-faint)",
              }}
            >
              {log.length === 0 && <em>click a button to see callbacks…</em>}
              {log.map((entry) => (
                <div key={entry.ts}>{entry.text}</div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
