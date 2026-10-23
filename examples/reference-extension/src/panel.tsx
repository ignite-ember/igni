/**
 * The reference extension's whole UI.
 *
 * A compliance scanner: list policy violations, filter them, open
 * one, accept it with a justification, and follow a scan while it
 * runs. Chosen because it exercises the parts of the floor a real
 * extension touches — a table, a filter, a dialog with a form, a
 * long-running job, an empty state, and every way each of those can
 * fail.
 *
 * Read it as the answer to "what does an extension look like", and
 * as a standing check on the floor: anything this file has to
 * hand-roll is something the floor is missing. What it hand-rolls
 * today is a live region and an error boundary, both tracked.
 *
 * **It is one shape, and a forgiving one.** A single view, mostly
 * reading, one pane, no prose. Each of those avoids a known gap: one
 * view never needs an addressable internal location, mostly-reading
 * never needs an unsaved-work guard, one pane never needs a split or
 * a page shell, no prose never needs a block markdown renderer.
 * Those gaps are real; this file is not evidence against them.
 *
 * ## What does not work yet
 *
 * This compiles, is type-checked in CI, and is written against the
 * real published surface. It cannot run, for two reasons that are
 * tracked and neither of which is about this file:
 *
 *   1. `@igni/extension-bridge` and `@igni/ui-react` are not
 *      published packages — they are a tsconfig alias inside
 *      `clients/web`.
 *   2. Nothing installs an extension. `ExtensionRegistry` takes a
 *      validated manifest and `ExtensionRpc.bind` takes a service,
 *      but nothing starts a sidecar or registers an endpoint, and
 *      nothing renders `ExtensionFrame`.
 *
 * The third reason is gone: `ext.acme.scanner.*` is a real
 * namespace now. Each call below is parsed, checked against this
 * extension's `permissions.rpc`, and routed to its service as an MCP
 * tool call — see EXTENSIBILITY §"A third party calling its own
 * service". `tests/test_an_extension_can_call_its_own_service.py`
 * asserts that all four methods this file uses are granted by the
 * manifest beside it, so narrowing that grant fails there rather
 * than in somebody's browser.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createRoot } from "react-dom/client";

import { connect, type Bridge } from "@igni/extension-bridge";
import {
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

import {
  messageOf,
  ScannerClient,
  SEVERITIES,
  type ScanStatus,
  type Severity,
  type Violation,
} from "./scanner";

import "./igni-ui.css";

/** How often to ask, while something is worth asking about. */
const POLL_MS = 2_000;

/** How long to back off after a failed poll, before trying again. */
const POLL_RETRY_MS = 10_000;

/** Severity maps onto the floor's four tones, not onto our colours. */
const TONE = {
  critical: "danger",
  high: "danger",
  medium: "warning",
  low: "neutral",
} as const;

/**
 * What the panel knows, as a union.
 *
 * Three states, not a nullable list. `violations === null` meant
 * "loading", which left nowhere to put "the backend refused", so the
 * first version showed an empty table forever when the RPC was
 * denied — the single most likely failure for a new extension,
 * rendered as success.
 */
type Load =
  | { state: "loading" }
  | { state: "failed"; message: string }
  | { state: "ready"; violations: Violation[]; scan: ScanStatus };

export function CompliancePanel({ client }: { client: ScannerClient }) {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [severity, setSeverity] = useState<Severity | "all">("all");
  const [open, setOpen] = useState<Violation | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");

  /**
   * The row whose Review button opened the dialog.
   *
   * Closing a dialog without putting focus back leaves it on
   * `document.body`: the next Tab starts from the top of the page,
   * and a screen reader is returned to nothing in particular. The
   * floor restores focus on Escape and on Cancel, because it still
   * owns the element that opened it — but accepting removes that row
   * from the table, so there is nothing to go back to and this
   * component has to choose a destination.
   */
  const reopen = useRef<HTMLButtonElement | null>(null);

  /**
   * One round trip for both facts.
   *
   * `allSettled`, not `all`: the list and the status are independent,
   * and `all` rejects on the first failure, so a backend that can
   * list but cannot report status showed nothing at all. Partial is
   * the normal state of a system with two endpoints.
   */
  const refresh = useCallback(async () => {
    const [violations, scan] = await Promise.allSettled([
      client.violations(),
      client.status(),
    ]);

    setLoad((previous) => {
      const kept = previous.state === "ready" ? previous : null;

      if (violations.status === "rejected") {
        // Nothing to show and nothing kept: this is the first load,
        // or the last one failed too. Say why.
        if (!kept) {
          return { state: "failed", message: messageOf(violations.reason) };
        }
        // There is a list on screen from a moment ago. Replacing it
        // with an error because one poll failed makes a flaky
        // connection look like a broken one.
        return kept;
      }

      return {
        state: "ready",
        violations: violations.value,
        scan:
          scan.status === "fulfilled"
            ? scan.value
            : // The list arrived and the status did not. "Idle" would
              // be a lie that stops the poll; this is the truth.
              { state: "failed", message: messageOf(scan.reason) },
      };
    });

    if (violations.status === "rejected") throw violations.reason;
  }, [client]);

  /**
   * Poll, scheduling the next one only once this one is done.
   *
   * `setInterval` fires on a clock whatever is in flight, so a
   * backend answering slower than the interval gets a growing queue
   * of overlapping requests, and their responses can land out of
   * order — the list flickers between two versions of itself. A
   * self-scheduling timeout cannot overlap by construction.
   *
   * It also backs off. A refused RPC is refused every two seconds
   * forever, which is a lot of denials in a host's audit log for a
   * panel nobody is looking at.
   */
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      let delay = POLL_MS;
      try {
        await refresh();
      } catch {
        // `refresh` has already put the failure on screen. The throw
        // is only how it tells this loop to slow down.
        delay = POLL_RETRY_MS;
      }
      if (alive) timer = setTimeout(() => void tick(), delay);
    };

    void tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [refresh]);

  const violations = load.state === "ready" ? load.violations : [];
  const shown = useMemo(
    () =>
      violations.filter((v) => severity === "all" || v.severity === severity),
    [violations, severity],
  );

  const scan = load.state === "ready" ? load.scan : null;
  const running = scan?.state === "running";

  /**
   * Starting a scan can fail, and used to fail silently.
   *
   * It was `onClick={() => void igni.rpc("…scan")}` — a floating
   * promise whose rejection went to the console. The button looked
   * like it worked, nothing happened, and the only clue was in a
   * devtools pane an extension's user does not have open.
   */
  const startScan = async () => {
    setBusy(true);
    setActionError("");
    try {
      await client.startScan();
      await refresh().catch(() => undefined);
    } catch (cause) {
      setActionError(messageOf(cause));
    } finally {
      setBusy(false);
    }
  };

  const acceptViolation = async (id: string, justification: string) => {
    setBusy(true);
    setActionError("");
    try {
      await client.accept(id, justification);
      setOpen(null);
      await refresh().catch(() => undefined);
      // The row that was focused no longer exists. Without this,
      // focus is on `document.body`.
      reopen.current?.focus();
    } catch (cause) {
      // Deliberately leaves the dialog open with its text intact.
      // Closing it would throw away what they wrote, and what they
      // wrote is the part that took effort.
      setActionError(messageOf(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <Row>
        <Heading level={2}>Compliance</Heading>
        {running ? <Spinner label="Scanning repository" /> : null}
      </Row>

      <Toolbar>
        <Select
          value={severity}
          aria-label="Filter by severity"
          onChange={(e) => setSeverity(e.target.value as Severity | "all")}
        >
          <option value="all">All severities</option>
          {SEVERITIES.map((s) => (
            <option key={s} value={s}>
              {s[0].toUpperCase() + s.slice(1)}
            </option>
          ))}
        </Select>
        <Button
          variant="primary"
          disabled={running || busy || load.state === "failed"}
          onClick={() => void startScan()}
        >
          Scan repository
        </Button>
      </Toolbar>

      {actionError ? (
        // `role="alert"` because this appears in response to
        // something the user just did, and they may not be looking
        // at this corner of the panel when it does.
        <Text role="alert" tone="muted">
          {actionError}
        </Text>
      ) : null}

      <ScanProgress scan={scan} />

      {/*
        Counts change without anything moving focus, so they are
        announced rather than only drawn. `aria-live="polite"` waits
        for a pause instead of interrupting, which is right for a
        number that updates every two seconds.

        This is `ui-visually-hidden` and a `<p>` because the floor
        publishes no live region. It is on the list.
      */}
      <p aria-live="polite" className="ui-visually-hidden">
        {load.state === "ready"
          ? `${shown.length} violation${shown.length === 1 ? "" : "s"} shown`
          : ""}
      </p>

      <Body
        load={load}
        shown={shown}
        severity={severity}
        busy={busy}
        onReview={(violation, button) => {
          reopen.current = button;
          setActionError("");
          setOpen(violation);
        }}
        onScan={() => void startScan()}
        onClearFilter={() => setSeverity("all")}
      />

      {open ? (
        <AcceptDialog
          // Remounts on a different violation, which is what resets
          // the textarea. Keeping the draft in this component meant
          // opening a second violation showed the first one's
          // justification, ready to submit against the wrong row.
          key={open.id}
          violation={open}
          busy={busy}
          onCancel={() => setOpen(null)}
          onAccept={(justification) =>
            void acceptViolation(open.id, justification)
          }
        />
      ) : null}
    </Card>
  );
}

/** The bar, or nothing. Split out so the panel reads as a list. */
function ScanProgress({ scan }: { scan: ScanStatus | null }) {
  if (scan?.state === "running") {
    return (
      <>
        {/* No percent means indeterminate, which slides rather than
            sits — a bar parked at a fixed width reads as stuck. */}
        <Progress
          value={scan.percent ?? undefined}
          label={`Scanning: ${scan.step}`}
        />
        <Text tone="muted">{scan.step}</Text>
      </>
    );
  }
  if (scan?.state === "failed") {
    return (
      <Text role="alert" tone="muted">
        The last scan failed: {scan.message}
      </Text>
    );
  }
  return null;
}

/**
 * Loading, failed, empty, or the table.
 *
 * One component so the four are visibly exclusive. Written as nested
 * ternaries in the panel, the empty case was wrong: it offered "Run a
 * scan" when the list was non-empty and the *filter* had hidden
 * everything, so the suggested fix for "no violations at this
 * severity" was to spend two minutes rescanning the repository.
 */
function Body({
  load,
  shown,
  severity,
  busy,
  onReview,
  onScan,
  onClearFilter,
}: {
  load: Load;
  shown: Violation[];
  severity: Severity | "all";
  busy: boolean;
  onReview: (violation: Violation, button: HTMLButtonElement) => void;
  onScan: () => void;
  onClearFilter: () => void;
}) {
  if (load.state === "loading") {
    return <Text tone="muted">Loading…</Text>;
  }

  if (load.state === "failed") {
    return (
      <Empty action={<Button size="sm" onClick={onScan}>Try again</Button>}>
        {load.message}
      </Empty>
    );
  }

  if (shown.length === 0) {
    const filtered = load.violations.length > 0;
    return (
      <Empty
        action={
          filtered ? (
            <Button size="sm" onClick={onClearFilter}>
              Show all severities
            </Button>
          ) : (
            <Button size="sm" disabled={busy} onClick={onScan}>
              Run a scan
            </Button>
          )
        }
      >
        {filtered
          ? `No ${severity} violations. ${load.violations.length} at other severities.`
          : "No violations found."}
      </Empty>
    );
  }

  return <ViolationTable rows={shown} onReview={onReview} />;
}

/**
 * The table.
 *
 * `Table` is a bare `<table>` — the floor publishes no `Thead`/`Tr`/
 * `Td`, so the markup inside is ours. Per EXTENSIBILITY §8 that
 * markup is not in the floor, which makes this the part most likely
 * to need revisiting, and the `<caption>` and `scope` attributes
 * below are the part most likely to be left out when it is.
 */
function ViolationTable({
  rows,
  onReview,
}: {
  rows: Violation[];
  onReview: (violation: Violation, button: HTMLButtonElement) => void;
}) {
  return (
    <Table>
      {/* Names the table for anyone arriving at it out of context —
          a screen reader's table list, or a jump by landmark. */}
      <caption className="ui-visually-hidden">
        Policy violations, most severe first
      </caption>
      <thead>
        <tr>
          <th scope="col">Severity</th>
          <th scope="col">Rule</th>
          <th scope="col">Location</th>
          {/* Empty, so it is announced as a column with no name
              rather than inheriting the one before it. */}
          <th scope="col">
            <span className="ui-visually-hidden">Actions</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((violation) => (
          <tr key={violation.id}>
            {/* `scope="row"` makes the rule the row's name, so the
                cells after it are read as belonging to that rule
                rather than as four loose values. */}
            <td>
              <Badge tone={TONE[violation.severity]}>
                {violation.severity}
              </Badge>
            </td>
            <th scope="row">{violation.rule}</th>
            <td>
              {violation.path}:{violation.line}
            </td>
            <td>
              {/*
                A button, not a clickable row: a `<tr>` with an
                `onClick` is not reachable by keyboard.

                The accessible name carries the rule. Twelve buttons
                all named "Review" are twelve identical entries in a
                screen reader's list of buttons, and no way to tell
                which row any of them belongs to.
              */}
              <Button
                size="xs"
                aria-label={`Review ${violation.rule}`}
                onClick={(e) => onReview(violation, e.currentTarget)}
              >
                Review
              </Button>
            </td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

/**
 * Accepting one violation.
 *
 * Its own component because the draft justification is its own
 * state, and state that belongs to a dialog should not outlive it.
 * The validation runs here too: a parent that owns the text has to
 * own the rules about the text, and then the dialog is a view onto
 * somebody else's form.
 */
function AcceptDialog({
  violation,
  busy,
  onCancel,
  onAccept,
}: {
  violation: Violation;
  busy: boolean;
  onCancel: () => void;
  onAccept: (justification: string) => void;
}): ReactNode {
  const [justification, setJustification] = useState("");
  const [error, setError] = useState("");

  const submit = () => {
    if (justification.trim().length < 10) {
      setError(
        "Say why, in a sentence somebody auditing this will understand.",
      );
      return;
    }
    setError("");
    onAccept(justification);
  };

  return (
    <Dialog
      title={violation.rule}
      sub={`${violation.path}:${violation.line}`}
      onClose={onCancel}
      actions={
        <>
          <Button onClick={onCancel}>Cancel</Button>
          <Button variant="primary" disabled={busy} onClick={submit}>
            {busy ? "Accepting…" : "Accept"}
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
          disabled={busy}
          onChange={(e) => {
            setJustification(e.target.value);
            // Clearing as they type, rather than on submit, so the
            // message goes away when the thing it complained about
            // is fixed.
            if (error) setError("");
          }}
        />
      </Field>
    </Dialog>
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
  const root = document.getElementById("root");
  if (!root) throw new Error("index.html has no #root");

  let igni: Bridge;
  try {
    igni = await connect();
  } catch (cause) {
    // Not framed by igni, or the host never greeted us. Say so
    // rather than show an empty panel. `textContent`, not
    // `innerHTML`: the message comes from a host this page does not
    // control.
    root.textContent = `Could not reach igni: ${messageOf(cause)}`;
    return;
  }

  createRoot(root).render(<CompliancePanel client={new ScannerClient(igni)} />);
}

void main();
