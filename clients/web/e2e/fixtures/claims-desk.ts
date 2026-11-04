/**
 * "Claims desk" (`acme.claims`) — a made-up extension for the landing
 * page's Extensions shots.
 *
 * Two halves, both fakes of what a real install hands the client:
 *
 *   - **The manifest**, as `list_extensions` describes it: one page
 *     with a sidebar entry, one tools-menu entry, one status chip.
 *   - **The bundle**, served from its own origin by a tiny HTTP
 *     server, exactly as the backend serves an installed extension's
 *     UI. The client frames it with its real `ExtensionFrame`; the
 *     page greets the bridge, takes igni's theme tokens from `hello`
 *     and `theme`, and asks for its queue through a real bridge call
 *     (`ext.acme.claims.queue`) that the fixture backend answers.
 *
 * Nothing here is drawn outside the frame: what the shots show of the
 * extension is this page, inside the client's own framing.
 */

import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";

export const CLAIMS_ID = "acme.claims";
export const CLAIMS_NAME = "Claims desk";
export const CLAIMS_PAGE = "claims-queue";

/** One row of the queue. Names and places are made up. */
export interface ClaimRow {
  no: string;
  type: string;
  amount: number;
  status: "Agent drafted" | "Needs a person" | "Waiting on documents";
  assignee: string;
  received: string;
}

export const CLAIMS: ClaimRow[] = [
  { no: "CL-20418", type: "Water damage", amount: 6850, status: "Agent drafted", assignee: "Sanne de Vries", received: "Today 09:12" },
  { no: "CL-20421", type: "Vehicle, third party", amount: 3120, status: "Needs a person", assignee: "Daan Visser", received: "Today 08:47" },
  { no: "CL-20425", type: "Storm damage, roof", amount: 1780, status: "Agent drafted", assignee: "Sanne de Vries", received: "Yesterday" },
  { no: "CL-20427", type: "Theft, e-bike", amount: 2290, status: "Needs a person", assignee: "Femke Jansen", received: "Yesterday" },
  { no: "CL-20429", type: "Glass breakage", amount: 640, status: "Agent drafted", assignee: "Femke Jansen", received: "Yesterday" },
  { no: "CL-20431", type: "Water damage", amount: 2400, status: "Needs a person", assignee: "Unassigned", received: "Mon" },
  { no: "CL-20433", type: "Fire, kitchen", amount: 14900, status: "Needs a person", assignee: "Daan Visser", received: "Mon" },
  { no: "CL-20436", type: "Liability, dog bite", amount: 2050, status: "Waiting on documents", assignee: "Ruben Smit", received: "Mon" },
];

/** The claim the detail pane opens on. */
export const DETAIL = {
  no: "CL-20418",
  type: "Water damage",
  address: "Keizersgracht 112-2, Amsterdam",
  policyholder: "Marieke Bakker",
  policy: "Woonhuis Plus · WP-7731-0042",
  amount: 6850,
  summary:
    "Leak from the upstairs neighbour's bathroom on 21 October. Kitchen ceiling and " +
    "parquet damaged. Cover applies under the home policy; the excess is €250.",
  missing: ["Plumber's invoice for the repair", "Photo of the damaged parquet", "Neighbour's insurer and policy number"],
  draft:
    "Beste mevrouw Bakker, dank voor uw melding. Uw schade valt onder uw woonhuisverzekering. " +
    "Om de uitkering af te ronden hebben we nog de factuur van de loodgieter nodig…",
};

/** `list_extensions`, for this one extension. `origin` is filled in
 *  once the bundle server is listening. */
export function claimsManifest(origin: string) {
  return {
    id: CLAIMS_ID,
    name: CLAIMS_NAME,
    version: "1.4.0",
    state: "READY",
    reason: "",
    origin,
    allow: ["ext.acme.claims.*"],
    pages: [{ kind: CLAIMS_PAGE, label: CLAIMS_NAME, sidebar: true, entry: "index.html" }],
    menu: [{ label: CLAIMS_NAME, command: "/claims", desc: "claims queue & drafts" }],
    indicators: [
      { id: "claims-open", label: "Claims", source: "indicator", opens: CLAIMS_PAGE, intervalSeconds: 30 },
    ],
    settings: [],
    values: {},
    web_url: null,
    service_url: null,
    tools: [
      { name: "claims_list_queue", hints: "read-only, local, idempotent" },
      { name: "claims_get_claim", hints: "read-only, local, idempotent" },
      { name: "claims_assign", hints: "local" },
    ],
  };
}

const PAGE_HTML = String.raw`<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claims desk</title>
<style>
  :root {
    --bg: #faf9f5; --bg-surface: #ffffff; --bg-raised: #ffffff; --bg-inset: #f0eee6;
    --fg: #1f1e1c; --fg-muted: #5f5d57; --fg-faint: #8d8a82;
    --border: #e3e0d6; --border-soft: #ece9e0;
    --success: #2f8a4c; --warning: #b7791f; --danger: #c0392b;
    --accent-blue: #3b6fd8; --accent-blue-text: #2f5bb8;
    --accent-green-text: #2f7a45; --accent-orange-text: #a3591a; --accent-red-text: #b0322a;
    --ember-gradient: linear-gradient(135deg, #ff7a2f, #e8452c);
    --radius-sm: 6px; --radius: 10px; --radius-lg: 14px; --radius-pill: 999px;
    --font-ui: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    --font-mono: ui-monospace, Menlo, monospace;
    --text-xs: 11px; --text-sm: 12px; --text-md: 13px; --text-lg: 14px; --text-xl: 17px; --text-2xl: 20px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    background: var(--bg); color: var(--fg); font-family: var(--font-ui);
    font-size: var(--text-md); -webkit-font-smoothing: antialiased;
    visibility: hidden;
  }
  body.ready { visibility: visible; }
  .wrap { display: flex; flex-direction: column; height: 100%; padding: 10px 26px 0; gap: 14px; }
  header { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; }
  h1 { margin: 0; font-size: var(--text-2xl); font-weight: 600; letter-spacing: -0.01em; }
  .sub { color: var(--fg-muted); font-size: var(--text-sm); margin-top: 3px; }
  .tabs { display: flex; gap: 6px; }
  .tab { border: 1px solid var(--border); background: var(--bg-surface); color: var(--fg-muted);
         padding: 5px 11px; border-radius: var(--radius-pill); font-size: var(--text-sm); }
  .tab.on { color: var(--fg); border-color: var(--fg-faint); font-weight: 600; }
  .tab b { font-weight: 600; margin-left: 4px; color: var(--accent-orange-text); }
  .body { display: grid; grid-template-columns: minmax(0, 1fr) 330px; gap: 16px; min-height: 0; flex: 1; }
  .card { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; }
  table { width: 100%; border-collapse: collapse; font-size: var(--text-md); }
  th { text-align: left; font-weight: 500; color: var(--fg-faint); font-size: var(--text-xs);
       text-transform: uppercase; letter-spacing: 0.06em; padding: 11px 14px; border-bottom: 1px solid var(--border); }
  td { padding: 10px 14px; border-bottom: 1px solid var(--border-soft); white-space: nowrap; }
  tr:last-child td { border-bottom: 0; }
  tr.sel td { background: var(--bg-inset); }
  td.no { font-family: var(--font-mono); font-size: var(--text-sm); }
  td.amt { text-align: right; font-variant-numeric: tabular-nums; }
  th.amt { text-align: right; }
  td.who { color: var(--fg-muted); }
  td.who.none { color: var(--fg-faint); font-style: italic; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 9px; border-radius: var(--radius-pill);
          font-size: var(--text-sm); font-weight: 500; }
  .pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .pill.drafted { color: var(--accent-blue-text); background: color-mix(in srgb, var(--accent-blue) 13%, transparent); }
  .pill.person { color: var(--accent-orange-text); background: color-mix(in srgb, var(--warning) 16%, transparent); }
  .pill.docs { color: var(--fg-muted); background: color-mix(in srgb, var(--fg-faint) 16%, transparent); }
  .detail { padding: 16px 18px; display: flex; flex-direction: column; gap: 12px; overflow: hidden; }
  .detail .top { display: flex; justify-content: space-between; align-items: baseline; }
  .detail .no { font-family: var(--font-mono); font-size: var(--text-sm); color: var(--fg-muted); }
  .detail h2 { margin: 2px 0 0; font-size: var(--text-xl); font-weight: 600; }
  .detail .amount { font-size: var(--text-xl); font-weight: 600; font-variant-numeric: tabular-nums; }
  dl { margin: 0; display: grid; grid-template-columns: 92px 1fr; gap: 5px 10px; font-size: var(--text-sm); }
  dt { color: var(--fg-faint); }
  dd { margin: 0; }
  .label { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.06em; color: var(--fg-faint); margin-bottom: 4px; }
  .detail p { margin: 0; font-size: var(--text-sm); line-height: 1.5; color: var(--fg); }
  ul { margin: 0; padding-left: 18px; font-size: var(--text-sm); line-height: 1.6; }
  .draft { border: 1px dashed var(--border); border-radius: var(--radius); padding: 9px 11px; font-size: var(--text-sm);
           color: var(--fg-muted); line-height: 1.45; background: var(--bg-inset); }
  .actions { display: flex; gap: 8px; margin-top: auto; padding-top: 4px; }
  button { font: inherit; font-size: var(--text-md); border-radius: var(--radius-pill); padding: 7px 14px; cursor: pointer;
           border: 1px solid var(--border); background: var(--bg-surface); color: var(--fg); }
  button.primary { background: var(--ember-gradient); color: #fff; border: 0; font-weight: 600; }
  @media (max-width: 760px) {
    .wrap { padding: 8px 12px 0; gap: 10px; }
    .body { grid-template-columns: 1fr; overflow: auto; }
    .tabs .tab:not(.on) { display: none; }
    th.hide, td.hide { display: none; }
    td, th { padding: 8px 9px; }
    .detail { padding: 13px 14px; gap: 9px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Claims desk</h1>
      <div class="sub" id="sub">Open claims · Team Particulier Amsterdam</div>
    </div>
    <div class="tabs">
      <span class="tab on">Needs a person<b id="needs">–</b></span>
      <span class="tab">Agent drafted</span>
      <span class="tab">All open</span>
    </div>
  </header>
  <div class="body">
    <div class="card queue">
      <table>
        <thead><tr><th>Claim</th><th>Type</th><th class="amt">Amount</th><th>Status</th><th class="hide">Assigned to</th><th class="hide">Received</th></tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <aside class="card detail" id="detail"></aside>
  </div>
</div>
<script>
(function () {
  var nonce = null;
  var eur = function (n) { return "€" + n.toLocaleString("en-GB"); };
  var pill = { "Agent drafted": "drafted", "Needs a person": "person", "Waiting on documents": "docs" };
  var esc = function (s) { return String(s).replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; }); };

  function tokens(t) {
    for (var k in (t || {})) document.documentElement.style.setProperty(k, t[k]);
  }

  function render(data) {
    var rows = data.claims.map(function (c) {
      return '<tr class="' + (c.no === data.detail.no ? "sel" : "") + '">' +
        '<td class="no">' + c.no + '</td><td>' + esc(c.type) + '</td><td class="amt">' + eur(c.amount) + '</td>' +
        '<td><span class="pill ' + pill[c.status] + '">' + c.status + '</span></td>' +
        '<td class="who hide' + (c.assignee === "Unassigned" ? " none" : "") + '">' + esc(c.assignee) + '</td>' +
        '<td class="hide" style="color:var(--fg-faint)">' + c.received + '</td></tr>';
    }).join("");
    document.getElementById("rows").innerHTML = rows;
    document.getElementById("needs").textContent = data.claims.filter(function (c) { return c.status === "Needs a person"; }).length;
    var d = data.detail;
    document.getElementById("detail").innerHTML =
      '<div class="top"><span class="no">' + d.no + '</span><span class="pill drafted">Agent drafted</span></div>' +
      '<div style="display:flex;justify-content:space-between;align-items:baseline"><h2>' + esc(d.type) + '</h2><span class="amount">' + eur(d.amount) + '</span></div>' +
      '<dl><dt>Address</dt><dd>' + esc(d.address) + '</dd><dt>Policyholder</dt><dd>' + esc(d.policyholder) + '</dd><dt>Policy</dt><dd>' + esc(d.policy) + '</dd></dl>' +
      '<div><div class="label">Agent summary</div><p>' + esc(d.summary) + '</p></div>' +
      '<div><div class="label">Missing from the file</div><ul>' + d.missing.map(function (m) { return "<li>" + esc(m) + "</li>"; }).join("") + '</ul></div>' +
      '<div class="draft">' + esc(d.draft) + '</div>' +
      '<div class="actions"><button class="primary" data-action="draft">Draft reply</button><button>Assign</button></div>';
    document.body.classList.add("ready");
  }

  window.addEventListener("message", function (event) {
    var m = event.data;
    if (!m || m.igni !== 1) return;
    if (m.kind === "hello") {
      nonce = m.nonce;
      tokens(m.tokens);
      document.documentElement.dataset.theme = m.theme;
      parent.postMessage({ igni: 1, kind: "call", nonce: nonce, id: "q1", method: "ext.acme.claims.queue", args: {} }, "*");
    } else if (m.kind === "theme") {
      tokens(m.tokens);
      document.documentElement.dataset.theme = m.theme;
    } else if (m.kind === "result" && m.id === "q1" && m.ok) {
      render(m.value);
    }
  });
})();
</script>
</body>
</html>
`;

/** Serve the bundle from its own origin. */
export async function serveClaimsBundle(): Promise<{ origin: string; close(): Promise<void> }> {
  const server: Server = createServer((req, res) => {
    const path = (req.url || "/").split("?")[0];
    if (path === "/" || path === "/index.html") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(PAGE_HTML);
      return;
    }
    res.writeHead(404);
    res.end();
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const port = (server.address() as AddressInfo).port;
  return {
    origin: `http://127.0.0.1:${port}`,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}
