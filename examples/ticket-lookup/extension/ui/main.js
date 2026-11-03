// The page's script. It must be a file: the page's CSP is
// `script-src 'self'`, so an inline <script> does not run.
//
// A minimal bridge, so this has no build step. For anything real, use
// @ignite-ember/extension-bridge, which is this with types.
const host = window.parent;
let nonce = null;
let next = 0;
const waiting = new Map();

window.addEventListener("message", (event) => {
  const message = event.data;
  if (event.source !== host || message?.igni !== 1) return;
  if (message.kind === "hello") {
    nonce = message.nonce;
    look();
    return;
  }
  if (message.kind === "result") {
    const waiter = waiting.get(message.id);
    if (!waiter) return;
    waiting.delete(message.id);
    message.ok ? waiter.resolve(message.value) : waiter.reject(new Error(message.error));
  }
});

function call(method, args = {}) {
  const id = String(++next);
  return new Promise((resolve, reject) => {
    waiting.set(id, { resolve, reject });
    host.postMessage({ igni: 1, kind: "call", nonce, id, method, args }, "*");
  });
}

const out = document.getElementById("out");

function look() {
  // Granted by permissions.rpc in manifest.json: ext.<extension id>.<tool>.
  call("ext.acme.tickets.lookup_ticket", { ticket_id: document.getElementById("q").value }).then(
    (value) => (out.textContent = JSON.stringify(value, null, 2)),
    (error) => (out.textContent = "error: " + error.message),
  );
}

document.getElementById("f").addEventListener("submit", (event) => {
  event.preventDefault();
  look();
});
