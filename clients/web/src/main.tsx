import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ChatScrollDemo } from "./dev/ChatScrollDemo";
import { HitlDemo } from "./dev/HitlDemo";
import { OrchestrateDemo } from "./dev/OrchestrateDemo";
import { PlanModeDemo } from "./dev/PlanModeDemo";
import { VisualizerStreamDemo } from "./dev/VisualizerStreamDemo";
import { WorkflowDemo } from "./dev/WorkflowDemo";
import { host } from "./lib/host";
import "./theme.css";
import "./workflow.css";

// Stamp ``data-host`` on <html> so host-conditional CSS
// (VSCode's ``--vscode-*`` bridge, JB/Tauri layout tweaks) has
// something to key off. Tauri also stamps this itself from its
// INIT_SCRIPT — running twice is harmless, the values agree —
// but VSCode and JCEF don't, so we do it here for them.
document.documentElement.dataset.host = host.kind;

// Demo URLs:
//   ?demo=team           — orchestrate / team-progress UI sandbox
//   ?demo=plan           — plan-mode (row 50) UI sandbox
//   ?demo=hitl           — HITL permission dialog variants
//   ?demo=chat-scroll    — headless Virtuoso scroll sandbox the
//                          chat-scroll e2e tests drive
//   ?demo=workflow       — workflow live-progress card sandbox
//                          (replays a canned event tape through
//                          reduceWorkflowEvent + <WorkflowRun/>)
// Anything else loads the real app.
const params = new URLSearchParams(window.location.search);
const demo = params.get("demo");

function pickRoot() {
  if (demo === "team") return <OrchestrateDemo />;
  if (demo === "plan") return <PlanModeDemo />;
  if (demo === "hitl") return <HitlDemo />;
  if (demo === "chat-scroll") return <ChatScrollDemo />;
  if (demo === "viz-stream") return <VisualizerStreamDemo />;
  if (demo === "workflow") return <WorkflowDemo />;
  return <App />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>{pickRoot()}</StrictMode>,
);
