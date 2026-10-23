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
 * **What it had to hand-roll, which is the finding**
 *
 * - **The split.** `ColumnHandle` exists in the app, is 180 lines,
 *   and is not published. Every two-pane extension writes pointer
 *   capture, clamping and a keyboard-operable separator again.
 * - **Block markdown.** The floor publishes `MarkdownInline`, which
 *   is *designed* to flatten block structure for previews. A document
 *   surface needs the opposite, so this renders a deliberately small
 *   subset itself and the result does not look like igni's prose.
 * - **An unsaved-work guard.** `close()` is one-way and `HostMessage`
 *   has no "about to close", so there is no way to stop the host
 *   navigating away from a dirty draft. The best available is to warn
 *   after the fact, which is not a guard.
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
  useMemo,
  useRef,
  useState,
  type ReactNode,
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
  Monogram,
  Row,
  Scroll,
  Spinner,
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
        The split. Hand-rolled: `ColumnHandle` is 180 lines in the app
        and is not published, so the width, the pointer capture and
        the keyboard-operable separator are all in panel.css and
        below. This is the single biggest thing the floor is missing
        for a two-pane shape.
      */}
      <div className="split">
        <Scroll className="split-list">
          <ReleaseList
            releases={releases}
            selected={selected}
            onPick={pick}
            loading={load.state === "loading"}
          />
        </Scroll>

        <Separator />

        <div className="split-main">
          {selected === null ? (
            <Empty>Pick a release to see its notes.</Empty>
          ) : (
            // Remounting on a different version is what resets the
            // editor. Holding the draft above this would show the
            // previous release's text against the new one's revision,
            // and the first save would overwrite it.
            <NoteView key={selected} client={client} version={selected} />
          )}
        </div>
      </div>
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

/**
 * The drag handle between the panes.
 *
 * A `separator` with `aria-valuenow`, because a split that can only
 * be moved with a pointer is a split half the users cannot move. The
 * width lives in a CSS variable rather than React state so dragging
 * does not re-render the document on every pointer move — which is
 * the reason the app's own `ColumnHandle` does the same thing, and
 * the reason copying its 180 lines into every extension is the wrong
 * answer.
 */
function Separator() {
  const held = useRef(false);

  const setWidth = (px: number) => {
    const clamped = Math.min(520, Math.max(180, px));
    document.documentElement.style.setProperty("--split-w", `${clamped}px`);
  };

  const current = () =>
    Number.parseInt(
      getComputedStyle(document.documentElement).getPropertyValue("--split-w"),
      10,
    ) || 280;

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize the release list"
      aria-valuenow={current()}
      aria-valuemin={180}
      aria-valuemax={520}
      tabIndex={0}
      className="split-handle"
      onPointerDown={(event) => {
        held.current = true;
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (held.current) setWidth(event.clientX);
      }}
      onPointerUp={(event) => {
        held.current = false;
        event.currentTarget.releasePointerCapture(event.pointerId);
      }}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft") setWidth(current() - 16);
        else if (event.key === "ArrowRight") setWidth(current() + 16);
        else return;
        event.preventDefault();
      }}
    />
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
          <Markdown source={draft} />
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

/**
 * Block markdown, badly, because the floor does not publish it.
 *
 * `MarkdownInline` is the only markdown the floor exports and it is
 * *designed* to destroy block structure — headings lose their size,
 * lists become middots, images return null. That is right for a
 * two-line preview and exactly wrong for a document.
 *
 * So this renders a deliberately small subset: headings, paragraphs,
 * bullets and fenced code. It is not a markdown renderer and should
 * not grow into one — what it is, is the shape of the gap. An
 * extension that wants real prose today bundles remark and then
 * eyeballs igni's typography, because the prose styling lives in
 * `theme.css` and is not in the floor either.
 */
function Markdown({ source }: { source: string }): ReactNode {
  const blocks = useMemo(() => source.split(/\n{2,}/), [source]);

  return (
    <div className="prose">
      {blocks.map((block, index) => {
        const trimmed = block.trim();
        if (trimmed.length === 0) return null;

        const heading = /^(#{1,4})\s+(.*)$/.exec(trimmed);
        if (heading) {
          const level = Math.min(4, heading[1].length) as 1 | 2 | 3 | 4;
          return (
            <Heading key={index} level={level}>
              {heading[2]}
            </Heading>
          );
        }

        if (trimmed.startsWith("```")) {
          return (
            <pre key={index} className="prose-code">
              {trimmed.replace(/^```[\w-]*\n?/, "").replace(/```$/, "")}
            </pre>
          );
        }

        const lines = trimmed.split("\n");
        if (lines.every((line) => /^[-*]\s+/.test(line))) {
          return (
            <ul key={index} className="prose-list">
              {lines.map((line, item) => (
                <li key={item}>{line.replace(/^[-*]\s+/, "")}</li>
              ))}
            </ul>
          );
        }

        return <Text key={index}>{trimmed}</Text>;
      })}
    </div>
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
