/**
 * Release Notes — the second shape.
 *
 * The reference extension is a single view, mostly reading, one pane,
 * no prose. Each of those four words hides a gap in the floor, which
 * is why it cannot be the standing check it claims to be. This is the
 * opposite of all four: a two-pane browser over editable documents
 * with comments, and its job is to find what that costs.
 *
 * **What it proves works**
 *
 * - `setLocation` / `onParams` make an extension addressable. Picking
 *   a version writes it into the host's URL; a reload or a shared
 *   link comes back to the same note. Nothing else in the contract
 *   can do this — the frame cannot write the address bar and its
 *   `localStorage` dies with the ephemeral port it is served from.
 * - Selection has a published look. `.ui-item[aria-selected]` means
 *   the thing that makes a row look picked is the thing that makes it
 *   announce as picked.
 * - `AppShell` + `Scroll` make two independently scrolling panes
 *   possible at all. Without `min-height: 0` the left list grows the
 *   page instead of scrolling.
 *
 * **What it found**
 *
 * - **A published pair that could not be used as a pair.** `Tabs`
 *   generated its own id prefix and `TabPanel` required one, with
 *   nothing to let a caller learn it. Fixed.
 * - **The split** was forty lines here and a block of CSS. `Split`
 *   is published now, and this file uses it.
 * - **Block markdown** was a hand-rolled subset that did not look
 *   like igni's prose. `Markdown` is published now, and so is
 *   `.ui-prose`.
 *
 * **What it still has to hand-roll**
 *
 * - **An unsaved-work guard.** `close()` is one-way and `HostMessage`
 *   has no "about to close", so there is no way to stop the host
 *   navigating away from a dirty draft. The best available is to warn
 *   after the fact, which is not a guard. This is a gap in the
 *   envelope rather than in the component list, and it is the one
 *   left.
 *
 * ## What does not work yet
 *
 * One thing: the packages are a tsconfig alias rather than an
 * install, so this is type-checked in CI and not bundled here. The
 * backend beside it is a real MCP server and is driven by
 * ``tests/test_a_second_extension_shape_works.py``.
 */

import {
  useCallback,
  useEffect,
  useState,
} from "react";
import { createRoot } from "react-dom/client";

import { connect, type Bridge } from "@igni/extension-bridge";
import {
  AppShell,
  Badge,
  Banner,
  Button,
  Dialog,
  Empty,
  ErrorBoundary,
  Field,
  Heading,
  Item,
  List,
  LiveRegion,
  Markdown,
  Monogram,
  Row,
  Scroll,
  Spinner,
  Split,
  Tabs,
  TabPanel,
  Text,
  Textarea,
  ToastHost,
  Toolbar,
  useToast,
  type Tone,
} from "@igni/ui-react";

import {
  ConflictError,
  messageOf,
  NotesClient,
  type Comment,
  type Note,
  type Release,
  type ReleaseState,
} from "./notes";

import "./igni-ui.css";
import "./panel.css";

/** State maps onto the floor's four tones, not onto our colours. */
const TONE: Record<ReleaseState, Tone> = {
  draft: "neutral",
  published: "success",
  superseded: "warning",
};

/** How long after the last keystroke to save. */
const AUTOSAVE_MS = 1_500;

type Load =
  | { state: "loading" }
  | { state: "failed"; message: string }
  | { state: "ready"; releases: Release[] };

export function NotesPanel({
  client,
  igni,
}: {
  client: NotesClient;
  igni: Bridge;
}) {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [selected, setSelected] = useState<string | null>(
    typeof igni.params.version === "string" ? igni.params.version : null,
  );
  // Restored from the host's URL, which is the only place an
  // extension has that survives its backend restarting.
  const [asideWidth, setAsideWidth] = useState(() => {
    const raw = igni.params.width;
    const parsed = typeof raw === "string" ? Number.parseInt(raw, 10) : NaN;
    return Number.isFinite(parsed) ? parsed : 280;
  });

  useEffect(() => {
    let alive = true;
    client.releases().then(
      (releases) => {
        if (!alive) return;
        setLoad({ state: "ready", releases });
        // Nothing picked and nothing asked for: open the newest, so
        // the right pane is never an empty frame on first paint.
        setSelected((current) => current ?? releases[0]?.version ?? null);
      },
      (error: unknown) => {
        if (alive) setLoad({ state: "failed", message: messageOf(error) });
      },
    );
    return () => {
      alive = false;
    };
  }, [client]);

  /**
   * The host changed our parameters under us.
   *
   * A link to a particular release, resolved while this frame was
   * already mounted. Before `onParams` existed the host had to
   * rebuild the frame to deliver this, losing whatever was being
   * edited — which is the whole difference between a widget and a
   * page.
   */
  useEffect(
    () =>
      igni.onParams((params) => {
        if (typeof params.version === "string") setSelected(params.version);
      }),
    [igni],
  );

  const pick = useCallback(
    (version: string) => {
      setSelected(version);
      // Not a navigation — `navigate` would ask the host to go
      // somewhere else. This says "still here, and here is now this",
      // and the host folds it into its own URL so a reload comes back.
      igni.setLocation({ version });
    },
    [igni],
  );

  const releases = load.state === "ready" ? load.releases : [];

  return (
    <AppShell
      header={
        <Toolbar>
          <Heading level={2}>Release notes</Heading>
          {load.state === "loading" ? <Spinner label="Loading releases" /> : null}
        </Toolbar>
      }
    >
      {load.state === "failed" ? (
        <Banner tone="danger">{load.message}</Banner>
      ) : null}

      {/*
        The split was forty lines of this file and a block of CSS
        until the floor published one. What is left is the two panes
        and a name for the separator.

        The width is not persisted, deliberately: an extension is
        served from an ephemeral port, so its `localStorage` is wiped
        whenever the backend restarts. `onResize` hands it back and
        this one puts it in the host's URL, which does survive.
      */}
      <Split
        aside={
          <Scroll>
            <ReleaseList
              releases={releases}
              selected={selected}
              onPick={pick}
              loading={load.state === "loading"}
            />
          </Scroll>
        }
        label="Resize the release list"
        defaultWidth={asideWidth}
        onResize={(width) => {
          setAsideWidth(width);
          if (selected !== null) {
            igni.setLocation({ version: selected, width: String(width) });
          }
        }}
      >
        {selected === null ? (
          <Empty>Pick a release to see its notes.</Empty>
        ) : (
          // Remounting on a different version is what resets the
          // editor. Holding the draft above this would show the
          // previous release's text against the new one's revision,
          // and the first save would overwrite it.
          <NoteView key={selected} client={client} version={selected} />
        )}
      </Split>
    </AppShell>
  );
}

/**
 * The left pane.
 *
 * `aria-selected` rather than a class: the published rule is keyed on
 * the attribute, so what makes a row look picked is what makes it
 * announce as picked and the two cannot disagree.
 */
function ReleaseList({
  releases,
  selected,
  onPick,
  loading,
}: {
  releases: Release[];
  selected: string | null;
  onPick: (version: string) => void;
  loading: boolean;
}) {
  if (loading) return <Text tone="muted">Loading…</Text>;
  if (releases.length === 0) return <Empty>No releases yet.</Empty>;

  return (
    <List>
      {releases.map((release) => (
        <Item
          key={release.version}
          aria-selected={release.version === selected}
          onClick={() => onPick(release.version)}
        >
          <Row>
            <span>{release.version}</span>
            <Badge tone={TONE[release.state]}>{release.state}</Badge>
          </Row>
        </Item>
      ))}
    </List>
  );
}

type NoteState =
  | { state: "loading" }
  | { state: "failed"; message: string }
  | { state: "ready"; note: Note; comments: Comment[] };

/** One release's note, its comments, and the editor. */
function NoteView({
  client,
  version,
}: {
  client: NotesClient;
  version: string;
}) {
  const [loaded, setLoaded] = useState<NoteState>({ state: "loading" });
  const [draft, setDraft] = useState("");
  const [revision, setRevision] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [tab, setTab] = useState("write");
  const toast = useToast();

  useEffect(() => {
    let alive = true;
    Promise.all([client.note(version), client.comments(version)]).then(
      ([note, comments]) => {
        if (!alive) return;
        setLoaded({ state: "ready", note, comments });
        setDraft(note.body);
        setRevision(note.revision);
      },
      (error: unknown) => {
        if (alive) setLoaded({ state: "failed", message: messageOf(error) });
      },
    );
    return () => {
      alive = false;
    };
  }, [client, version]);

  const save = useCallback(
    async (body: string) => {
      setSaving(true);
      try {
        const saved = await client.save(version, body, revision);
        setRevision(saved.revision);
        setDirty(false);
        setConflict(null);
      } catch (cause) {
        if (cause instanceof ConflictError) {
          // The draft is deliberately left alone. Replacing it with
          // theirs would be the data loss this check exists to stop.
          setConflict(cause.theirRevision);
        } else {
          toast.show(messageOf(cause), { tone: "danger" });
        }
      } finally {
        setSaving(false);
      }
    },
    [client, revision, toast, version],
  );

  // Autosave, debounced. A document that only saves on a button is a
  // document somebody loses.
  useEffect(() => {
    if (!dirty || conflict !== null) return;
    const timer = setTimeout(() => void save(draft), AUTOSAVE_MS);
    return () => clearTimeout(timer);
  }, [draft, dirty, conflict, save]);

  /*
    The unsaved-work guard, and the shape of the gap.

    `beforeunload` covers a reload of *this frame*. It does not cover
    the host navigating away from the page that contains it, which is
    the common case: `close()` is one-way and `HostMessage` has no
    "about to close" for a guest to veto. So an extension can protect
    a draft from the browser and not from its host, which is the wrong
    way round.
  */
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  if (loaded.state === "loading") return <Text tone="muted">Loading…</Text>;
  if (loaded.state === "failed") return <Banner tone="danger">{loaded.message}</Banner>;

  const { note, comments } = loaded;

  return (
    <AppShell
      header={
        <Toolbar>
          <Heading level={3}>{version}</Heading>
          <SaveState dirty={dirty} saving={saving} />
          <Button
            variant="primary"
            disabled={dirty || saving || conflict !== null}
            onClick={() => setConfirming(true)}
          >
            Publish
          </Button>
        </Toolbar>
      }
    >
      {conflict === null ? null : (
        <Banner tone="danger">
          Somebody else saved this note while you were editing it. Your text is
          still here — copy what you need, then reload.
        </Banner>
      )}

      <LiveRegion>
        {saving ? "Saving" : dirty ? "Unsaved changes" : "Saved"}
      </LiveRegion>

      <Tabs
        // The strip and its panels have to agree on an id prefix.
        // `Tabs` will generate one, but then a `TabPanel` has no way
        // to learn it — so a caller with panels passes it.
        base={`note-${version}`}
        label={`Notes for ${version}`}
        active={tab}
        onChange={setTab}
        tabs={[
          { id: "write", label: "Write" },
          { id: "preview", label: "Preview" },
          { id: "comments", label: `Comments (${comments.length})` },
        ]}
      />

      <TabPanel base={`note-${version}`} id="write" active={tab}>
        <Field label="Notes" htmlFor="body" hint="Markdown. Saved as you type.">
          <Textarea
            id="body"
            className="note-editor"
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              setDirty(true);
            }}
          />
        </Field>
      </TabPanel>

      <TabPanel base={`note-${version}`} id="preview" active={tab}>
        <Scroll>
          <Markdown>{draft}</Markdown>
        </Scroll>
      </TabPanel>

      <TabPanel base={`note-${version}`} id="comments" active={tab}>
        <Scroll>
          <CommentList comments={comments} />
        </Scroll>
      </TabPanel>

      {confirming ? (
        <Dialog
          title={`Publish ${version}?`}
          sub="Everyone with access sees this version. The draft is kept."
          onClose={() => setConfirming(false)}
          actions={
            <>
              <Button onClick={() => setConfirming(false)}>Cancel</Button>
              <Button
                variant="primary"
                onClick={() => {
                  void client.publish(version).then(
                    () => {
                      setConfirming(false);
                      toast.show(`${version} published`, { tone: "success" });
                    },
                    (error: unknown) =>
                      toast.show(messageOf(error), { tone: "danger" }),
                  );
                }}
              >
                Publish
              </Button>
            </>
          }
        >
          <Text tone="muted">
            {note.body.trim().length === 0
              ? "This note is empty."
              : `${note.body.trim().split(/\s+/).length} words.`}
          </Text>
        </Dialog>
      ) : null}
    </AppShell>
  );
}

function SaveState({ dirty, saving }: { dirty: boolean; saving: boolean }) {
  if (saving) return <Text tone="muted">Saving…</Text>;
  return <Text tone="muted">{dirty ? "Unsaved changes" : "Saved"}</Text>;
}

function CommentList({ comments }: { comments: Comment[] }) {
  if (comments.length === 0) return <Empty>No comments yet.</Empty>;
  return (
    <List>
      {comments.map((comment) => (
        <div key={comment.id} className="ui-card comment">
          <Row>
            <Monogram name={comment.author} />
            <Text>{comment.author}</Text>
            {comment.resolved ? <Badge tone="success">resolved</Badge> : null}
          </Row>
          <Text>{comment.body}</Text>
        </div>
      ))}
    </List>
  );
}

async function main() {
  const root = document.getElementById("root");
  if (!root) throw new Error("index.html has no #root");

  let igni: Bridge;
  try {
    igni = await connect();
  } catch (cause) {
    root.textContent = `Could not reach igni: ${messageOf(cause)}`;
    return;
  }

  createRoot(root).render(
    <ErrorBoundary onError={(error) => console.error("[acme.notes]", error)}>
      <ToastHost>
        <NotesPanel client={new NotesClient(igni)} igni={igni} />
      </ToastHost>
    </ErrorBoundary>,
  );
}

void main();
