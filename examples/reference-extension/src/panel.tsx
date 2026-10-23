/**
 * The reference extension's whole UI.
 *
 * A compliance scanner: list policy violations, filter them, open one,
 * accept it with a justification, and follow a scan while it runs.
 * Chosen because it exercises every part of the floor that a real
 * extension touches — a table, a filter, a dialog with a form, a
 * long-running job, and an empty state.
 *
 * Read it as the answer to "what does an extension look like", and as
 * a standing check on the floor: anything this file has to hand-roll
 * is something the floor is missing.
 *
 * ## What does not work yet
 *
 * This compiles and is written against the real published surface. It
 * cannot run, for three reasons that are tracked and none of which are
 * about this file:
 *
 *   1. `@igni/extension-bridge` and `@igni/ui-react` are not published
 *      packages — they are a tsconfig alias inside `clients/web`.
 *   2. Nothing renders `ExtensionFrame`, so no extension is mounted.
 *   3. `ext.acme.scanner.*` cannot be answered. `RpcMethod` is a closed
 *      enum and the router validates against it exhaustively, so an
 *      extension's own service has no namespace to reply on. Every
 *      `igni.rpc` call below would be refused.
 *
 * Until (3) lands, an extension's UI can only render igni's own data
 * back to the user.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";

import { connect, type Bridge } from "@igni/extension-bridge";
import {
  Actions,
  Badge,
  Button,
  Card,
  Dialog,
  Empty,
  Field,
  Heading,
  Progress,
  Row,
  Select,
  Spinner,
  Table,
  Text,
  Textarea,
  Toolbar,
} from "@igni/ui-react";

import "./igni-ui.css";

type Severity = "critical" | "high" | "medium" | "low";

interface Violation {
  id: string;
  rule: string;
  severity: Severity;
  path: string;
  line: number;
}

interface ScanStatus {
  running: boolean;
  /** 0–100, or null while the total is unknown. */
  percent: number | null;
  step: string;
}

/** Severity maps onto the floor's four tones, not onto our own colours. */
const TONE = {
  critical: "danger",
  high: "danger",
  medium: "warning",
  low: "neutral",
} as const;

function CompliancePanel({ igni }: { igni: Bridge }) {
  const [violations, setViolations] = useState<Violation[] | null>(null);
  const [severity, setSeverity] = useState<Severity | "all">("all");
  const [scan, setScan] = useState<ScanStatus | null>(null);
  const [open, setOpen] = useState<Violation | null>(null);
  const [justification, setJustification] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    const [rows, status] = await Promise.all([
      igni.rpc<Violation[]>("ext.acme.scanner.list"),
      igni.rpc<ScanStatus>("ext.acme.scanner.status"),
    ]);
    setViolations(rows);
    setScan(status);
  }, [igni]);

  // Polling, because the bridge has no push. `HostMessage` is
  // `hello | theme | result`, so a long-running job is followed by
  // asking. igni's own CodeIndex panel does the same thing, on the
  // same interval.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (alive) await refresh().catch(() => undefined);
    };
    void tick();
    const timer = setInterval(tick, 2_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [refresh]);

  const shown = useMemo(
    () => (violations ?? []).filter((v) => severity === "all" || v.severity === severity),
    [violations, severity],
  );

  async function accept() {
    if (!open) return;
    if (justification.trim().length < 10) {
      setError("Say why, in a sentence somebody auditing this will understand.");
      return;
    }
    await igni.rpc("ext.acme.scanner.accept", { id: open.id, justification });
    setOpen(null);
    setJustification("");
    setError("");
    await refresh();
  }

  return (
    <Card>
      <Row>
        <Heading level={2}>Compliance</Heading>
        {scan?.running ? <Spinner label="Scanning repository" /> : null}
      </Row>

      <Toolbar>
        <Select
          value={severity}
          aria-label="Filter by severity"
          onChange={(e) => setSeverity(e.target.value as Severity | "all")}
        >
          <option value="all">All severities</option>
          <option value="critical">Critical</option>
          <option value="high">High</option>
          <option value="medium">Medium</option>
          <option value="low">Low</option>
        </Select>
        <Button
          variant="primary"
          disabled={scan?.running}
          onClick={() => void igni.rpc("ext.acme.scanner.scan")}
        >
          Scan repository
        </Button>
      </Toolbar>

      {scan?.running ? (
        <>
          {/* No percent means indeterminate, which slides rather than
              sits — a bar parked at a fixed width reads as stuck. */}
          <Progress
            value={scan.percent ?? undefined}
            label={`Scanning: ${scan.step}`}
          />
          <Text tone="muted">{scan.step}</Text>
        </>
      ) : null}

      {violations === null ? (
        <Text tone="muted">Loading…</Text>
      ) : shown.length === 0 ? (
        <Empty
          action={
            <Button size="sm" onClick={() => void igni.rpc("ext.acme.scanner.scan")}>
              Run a scan
            </Button>
          }
        >
          {violations.length === 0
            ? "No violations found."
            : "No violations at this severity."}
        </Empty>
      ) : (
        // `Table` is a bare <table> — the floor publishes no Thead/Tr/Td,
        // so the markup inside is ours. Per EXTENSIBILITY §8 that markup
        // is not in the floor, which means this is the part most likely
        // to need revisiting.
        <Table>
          <thead>
            <tr>
              <th scope="col">Severity</th>
              <th scope="col">Rule</th>
              <th scope="col">Location</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {shown.map((v) => (
              <tr key={v.id}>
                <td>
                  <Badge tone={TONE[v.severity]}>{v.severity}</Badge>
                </td>
                <td>{v.rule}</td>
                <td>
                  {v.path}:{v.line}
                </td>
                <td>
                  {/* A button, not a clickable row: a <tr> with an
                      onClick is not reachable by keyboard. */}
                  <Button size="xs" onClick={() => setOpen(v)}>
                    Review
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}

      {open ? (
        <Dialog
          title={open.rule}
          sub={`${open.path}:${open.line}`}
          onClose={() => setOpen(null)}
          actions={
            <>
              <Button onClick={() => setOpen(null)}>Cancel</Button>
              <Button variant="primary" onClick={() => void accept()}>
                Accept
              </Button>
            </>
          }
        >
          <Field
            label="Justification"
            htmlFor="justification"
            hint="Recorded in the audit trail."
            error={error || undefined}
          >
            <Textarea
              id="justification"
              value={justification}
              onChange={(e) => setJustification(e.target.value)}
            />
          </Field>
        </Dialog>
      ) : null}
    </Card>
  );
}

/**
 * Connect first, render second.
 *
 * `connect()` resolves once the host has sent `hello`, which carries
 * the nonce, the launch params and the live theme tokens. Rendering
 * before it resolves means painting once in the fallback palette and
 * again in the real one.
 */
async function main() {
  let igni: Bridge;
  try {
    igni = await connect();
  } catch (cause) {
    // Not framed by igni, or the host never greeted us. Say so rather
    // than show an empty panel.
    document.body.textContent = `Could not reach igni: ${(cause as Error).message}`;
    return;
  }
  createRoot(document.getElementById("root")!).render(<CompliancePanel igni={igni} />);
}

void main();
