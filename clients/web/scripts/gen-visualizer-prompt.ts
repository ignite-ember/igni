/**
 * Auto-generate the visualizer sub-agent's system prompt from the
 * json-render catalog.
 *
 * The visualizer streams UI progressively via json-render's SpecStream
 * protocol: each line of its content is a JSONL RFC-6902 JSON Patch
 * operation. The BE's orchestrate.py intercepts those lines, forwards
 * each patch to the FE as it arrives, and the FE reduces them into
 * one live-updating card.
 *
 * ``catalog.prompt({mode: "standalone"})`` produces the WHOLE
 * instruction set the LLM needs for that protocol — output format,
 * available components + prop schemas, actions, events, visibility,
 * dynamic props, state model, rules. We wrap that with a small
 * ember-specific header (frontmatter + role reminder + no-data rule)
 * and write the result to ``bundled_agents/visualizer.md``.
 *
 * Never hand-edit the generated file; edit either the catalog (for
 * schema) or this script (for wrapper prose).
 */

import { writeFileSync } from "node:fs";
import { resolve, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { catalog } from "../src/components/jsonRender/catalog";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(__dirname, "..", "..", "..", "src", "ember_code", "bundled_agents", "visualizer.md");
if (!OUT.endsWith(join("bundled_agents", "visualizer.md"))) {
  throw new Error(`Unexpected output path: ${OUT}`);
}

// ── Fixed wrapper ───────────────────────────────────────────────────

const FRONTMATTER = `---
name: visualizer
description: Renders structured data as UI (charts, tables, KPI cards, dashboards, forms) by emitting a json-render spec to the client. Sub-agent — other agents delegate to this one whenever they have something worth showing visually. Never invents data; renders only what the caller hands over.
tools: Visualize
color: magenta

tags:
  - visualization
  - ui
  - json-render
  - generative-ui
can_orchestrate: true
---`;

const INTRO = `You render structured data as UI via json-render specs. You have one tool: \`Visualize(spec, title)\`. Call it with a spec matching the catalog below, then reply in one short sentence confirming what you emitted.

**Never invent data.** If the caller hands you intent without data (e.g. "chart AAPL 2023 monthly closes" with no numbers), do NOT fabricate from your training knowledge. Emit a single \`Alert\` (tone: \`warning\`) explaining that the caller must supply the values. Charts read as authoritative; made-up numbers mislead the user.

Every spec is \`{ "root": "<id>", "elements": { "<id>": { "type": "<Catalog>", "props": {...}, "children"?: ["<id>"], "on"?: {...} } } }\`. \`children\` holds element ids (strings), NOT inline objects. Only use component types from the catalog below; anything else renders as an "Unknown component" placeholder.

The section below is auto-generated from the client's \`@json-render/react\` catalog. It's the exact contract the renderer enforces.`;

const AUTOGEN_MARKER_START = "<!-- AUTOGEN:CATALOG-PROMPT START — do not edit; regenerate via `npm run gen:visualizer-prompt` -->";
const AUTOGEN_MARKER_END = "<!-- AUTOGEN:CATALOG-PROMPT END -->";

// ── Generate + write ────────────────────────────────────────────────

// We call ``catalog.prompt({mode: "standalone"})`` and then STRIP the
// JSONL-streaming protocol prose from the top — we're tool-based, not
// streaming-based. What we keep is the AVAILABLE COMPONENTS + AVAILABLE
// ACTIONS + EVENTS + VISIBILITY + DYNAMIC PROPS + RULES sections, which
// are the actual schema the model needs.
const fullPrompt = catalog.prompt({ mode: "standalone" });
const componentsIdx = fullPrompt.search(/(^|\n)AVAILABLE COMPONENTS \(\d+\):/);
if (componentsIdx < 0) {
  throw new Error("AVAILABLE COMPONENTS heading not found — @json-render/core prompt format changed?");
}
const catalogPrompt = fullPrompt.slice(componentsIdx).trim();

const body = [
  FRONTMATTER,
  "",
  INTRO,
  "",
  AUTOGEN_MARKER_START,
  "",
  catalogPrompt,
  "",
  AUTOGEN_MARKER_END,
  "",
].join("\n");

writeFileSync(OUT, body, "utf8");

const lineCount = body.split("\n").length;
process.stdout.write(
  `Wrote ${OUT}\n  ${lineCount} lines, ${body.length} bytes (${catalog.componentNames.length} catalog components)\n`,
);
