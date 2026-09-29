/**
 * Editing a document in place, and saving it as a version.
 *
 * **Save is explicit, and one save is one version.** The store is
 * append-only and the version list is on screen beside this, so an
 * autosaving editor would put forty versions into an afternoon's
 * typing and make that list useless. Nothing is written until Save.
 *
 * **Cancel is safe.** It puts back the text that was loaded, not the
 * text on screen, so the answer to "did I just lose that?" is always
 * no — and it asks first when there is something to lose.
 *
 * **An archived document opens read-only.** Archiving is what this
 * app offers instead of deleting something the lineage depends on,
 * and that offer only means anything if the archived thing stops
 * changing. The editor is not the place to discover that: the button
 * says so before it is pressed.
 */
import { EditorContent, useEditor } from "@tiptap/react";
import { useCallback, useEffect, useRef, useState } from "react";

import { MenuIcon } from "../MenuIcon";
import { Tooltip } from "../Tooltip";
import { DOCUMENT_EXTENSIONS, markdownOf, preserveWrapping } from "./documentMarkdown";

export interface DocumentEditorProps {
  /** The markdown as it stands. */
  content: string;
  /** Frozen: archived documents do not take a new version. */
  frozen?: boolean;
  /** Saving is in flight; the buttons say so and do nothing twice. */
  busy?: boolean;
  /** Write a new version. Rejecting leaves the editor open and dirty. */
  onSave: (markdown: string) => Promise<void> | void;
  onCancel: () => void;
}

/** What the toolbar can do, in the order it does it. */
const TOOLS = [
  { key: "bold", label: "Bold", glyph: "B", mark: "bold" },
  { key: "italic", label: "Italic", glyph: "I", mark: "italic" },
  { key: "code", label: "Code", glyph: "<>", mark: "code" },
] as const;

interface BlockTool {
  key: string;
  label: string;
  glyph: string;
  node: string;
  /** Only headings need one, and a union without it does not narrow. */
  attrs?: Record<string, unknown>;
}

const BLOCKS: BlockTool[] = [
  { key: "h2", label: "Heading", glyph: "H2", node: "heading", attrs: { level: 2 } },
  { key: "h3", label: "Subheading", glyph: "H3", node: "heading", attrs: { level: 3 } },
  { key: "bulletList", label: "Bulleted list", glyph: "•", node: "bulletList" },
  { key: "orderedList", label: "Numbered list", glyph: "1.", node: "orderedList" },
  { key: "blockquote", label: "Quote", glyph: "❝", node: "blockquote" },
  { key: "codeBlock", label: "Code block", glyph: "{ }", node: "codeBlock" },
];

export function DocumentEditor({
  content,
  frozen = false,
  busy = false,
  onSave,
  onCancel,
}: DocumentEditorProps) {
  // The text this editor was opened on. Cancel goes back to *this*,
  // and Save diffs against it so untouched paragraphs keep the
  // wrapping they arrived with.
  const opened = useRef(content);
  const [dirty, setDirty] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");

  const editor = useEditor({
    extensions: DOCUMENT_EXTENSIONS,
    content,
    editable: !frozen,
    // Focused on open, at the start. Somebody who pressed Edit is
    // about to type, and a click to begin is a click that says
    // nothing.
    autofocus: frozen ? false : "start",
    // Dirty is computed from the text, not raised by the event.
    // ProseMirror emits an update while it is still settling the
    // document it was handed, so a flag set here is true before the
    // first keystroke — which makes Save offer to write a version
    // identical to the one on screen, and Cancel ask whether to
    // throw away work nobody has done.
    //
    // Comparing also gets undo right: typing a word and taking it
    // back leaves the document unchanged, and no event-based flag
    // ever notices that.
    onUpdate: ({ editor: live }) => {
      const now = preserveWrapping(opened.current, markdownOf(live));
      setDirty(now.trim() !== opened.current.trim());
    },
  });

  // A version landing underneath — the agent answering a comment,
  // usually — replaces what is on screen only while nothing has been
  // typed into it. Overwriting somebody's unsaved paragraph with a
  // newer one is the one thing this must not do.
  useEffect(() => {
    if (!editor || dirty) return;
    if (content === opened.current) return;
    opened.current = content;
    editor.commands.setContent(content, { emitUpdate: false });
  }, [content, dirty, editor]);

  useEffect(() => {
    editor?.setEditable(!frozen);
  }, [editor, frozen]);

  const save = useCallback(async () => {
    if (!editor || busy || frozen) return;
    setError("");
    const next = preserveWrapping(opened.current, markdownOf(editor));
    if (next.trim() === opened.current.trim()) {
      // Nothing changed. Writing a version that says the same as the
      // one before it is noise in a list people read to find out
      // what changed.
      onCancel();
      return;
    }
    try {
      await onSave(next);
    } catch (e) {
      // Left open and still dirty: the words are in this box and
      // nowhere else, and closing would be the only way to lose them.
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [editor, busy, frozen, onSave, onCancel]);

  const cancel = useCallback(() => {
    if (dirty && !confirming) {
      setConfirming(true);
      return;
    }
    editor?.commands.setContent(opened.current, { emitUpdate: false });
    setDirty(false);
    setConfirming(false);
    onCancel();
  }, [dirty, confirming, editor, onCancel]);

  if (!editor) return null;

  return (
    <div className="doc-editor">
      {!frozen && (
        <div className="doc-editor-bar" role="toolbar" aria-label="Formatting">
          {TOOLS.map((tool) => (
            <Tooltip key={tool.key} text={tool.label}>
              <button
                type="button"
                className={editor.isActive(tool.mark) ? "doc-tool is-on" : "doc-tool"}
                aria-label={tool.label}
                aria-pressed={editor.isActive(tool.mark)}
                onClick={() =>
                  editor.chain().focus().toggleMark(tool.mark).run()
                }
              >
                {tool.glyph}
              </button>
            </Tooltip>
          ))}
          <span className="doc-editor-bar-gap" />
          {BLOCKS.map((block) => (
            <Tooltip key={block.key} text={block.label}>
              <button
                type="button"
                className={
                  editor.isActive(block.node, block.attrs) ? "doc-tool is-on" : "doc-tool"
                }
                aria-label={block.label}
                aria-pressed={editor.isActive(block.node, block.attrs)}
                onClick={() =>
                  editor
                    .chain()
                    .focus()
                    .toggleNode(block.node, "paragraph", block.attrs)
                    .run()
                }
              >
                {block.glyph}
              </button>
            </Tooltip>
          ))}
        </div>
      )}

      <EditorContent className="doc-editor-body" editor={editor} />

      {error && <p className="msg-error doc-editor-error">{error}</p>}

      <div className="doc-editor-foot">
        {frozen ? (
          <>
            <MenuIcon name="archive" />
            <span className="kb-hint">
              This document is archived. Restore it to make changes — everything built on
              it goes on resolving either way.
            </span>
          </>
        ) : confirming ? (
          <>
            <span className="kb-hint">Throw away what you have written?</span>
            <button type="button" className="btn btn-sm btn-danger" onClick={cancel}>
              Discard
            </button>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setConfirming(false)}
            >
              Keep editing
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={busy || !dirty}
              onClick={() => void save()}
            >
              {busy ? "Saving…" : "Save as a new version"}
            </button>
            <button type="button" className="btn btn-sm" onClick={cancel}>
              Cancel
            </button>
            {/* Said before saving, not after. A version is a thing
                other documents can be built on, so it is worth
                knowing that is what the button makes. */}
            <span className="kb-hint">
              {dirty ? "Saving writes a new version." : "No changes yet."}
            </span>
          </>
        )}
      </div>
    </div>
  );
}
