#!/usr/bin/env node
/**
 * Minimal HTTP/HTTPS proxy used to verify that the IDE-side proxy
 * propagation actually puts the right env vars onto subprocesses.
 *
 * Handles:
 *
 *   - ``CONNECT host:port HTTP/1.1`` for HTTPS tunnelling. The
 *     client opens an end-to-end TLS connection through us; we
 *     just pipe bytes. This is the only thing uv / pip / curl
 *     actually use against ``https://github.com/...`` /
 *     ``https://pypi.org/...``.
 *
 *   - Plain ``GET http://...`` requests for HTTP-scheme traffic.
 *     Forwarded with the same headers, response streamed back.
 *
 * Logs every observed target host + method to ``--log-file`` so the
 * test harness can assert which traffic flowed through. Logs are
 * append-only newline-delimited JSON.
 *
 * Usage:
 *
 *   node scripts/test-proxy.mjs --port 0 --log-file /tmp/proxy.log
 *
 *   (port 0 binds to an OS-assigned ephemeral port; the bound port
 *   is printed on stdout as ``listening on 127.0.0.1:<port>``.)
 */

import * as http from "node:http";
import * as net from "node:net";
import * as fs from "node:fs";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, arg, i, all) => {
    if (arg.startsWith("--")) {
      const key = arg.slice(2);
      const val = all[i + 1] && !all[i + 1].startsWith("--") ? all[i + 1] : "";
      pairs.push([key, val]);
    }
    return pairs;
  }, []),
);

const PORT = Number(args.port ?? 0);
const LOG_FILE = args["log-file"] || null;
const HOST = "127.0.0.1";

function log(event) {
  const line = JSON.stringify({ ts: new Date().toISOString(), ...event });
  if (LOG_FILE) fs.appendFileSync(LOG_FILE, line + "\n");
  // Also echo to stderr so a human watching ``node ... | …`` can see
  // traffic in real time without disturbing the ``listening on …``
  // stdout line.
  process.stderr.write(line + "\n");
}

const server = http.createServer((req, res) => {
  // Plain HTTP request: forward verbatim. Few callers use this
  // path against package mirrors (which are all HTTPS), but the
  // proxy stays honest by handling it.
  const url = new URL(req.url);
  log({ kind: "http", method: req.method, host: url.host, path: url.pathname });
  const upstream = http.request(
    {
      host: url.hostname,
      port: url.port || 80,
      method: req.method,
      path: url.pathname + url.search,
      headers: req.headers,
    },
    (rres) => {
      res.writeHead(rres.statusCode, rres.headers);
      rres.pipe(res);
    },
  );
  upstream.on("error", (e) => {
    log({ kind: "http_error", error: String(e) });
    res.writeHead(502).end();
  });
  req.pipe(upstream);
});

// HTTPS path: client sends ``CONNECT host:443 HTTP/1.1``; we open a
// TCP socket to the target and pipe bytes both ways. The TLS
// handshake happens end-to-end between client and target — we
// never see the plaintext.
server.on("connect", (req, clientSocket, head) => {
  const [host, portStr] = req.url.split(":");
  const port = Number(portStr || 443);
  log({ kind: "connect", host, port });
  const upstream = net.connect(port, host, () => {
    clientSocket.write(
      "HTTP/1.1 200 Connection Established\r\n" +
        "Proxy-agent: ember-test-proxy/1.0\r\n\r\n",
    );
    upstream.write(head);
    upstream.pipe(clientSocket);
    clientSocket.pipe(upstream);
  });
  upstream.on("error", (e) => {
    log({ kind: "connect_error", host, port, error: String(e) });
    clientSocket.end();
  });
  clientSocket.on("error", () => upstream.destroy());
});

server.listen(PORT, HOST, () => {
  const { port } = server.address();
  // Single line so the parent shell can grep for it.
  console.log(`listening on ${HOST}:${port}`);
});

// Graceful shutdown — flush the log file and exit cleanly so the
// test harness can ``kill -TERM`` us at the end.
for (const sig of ["SIGTERM", "SIGINT"]) {
  process.on(sig, () => {
    log({ kind: "shutdown", signal: sig });
    server.close(() => process.exit(0));
    // Backup: force-exit if close hangs on a lingering connection.
    setTimeout(() => process.exit(0), 1_000).unref();
  });
}
