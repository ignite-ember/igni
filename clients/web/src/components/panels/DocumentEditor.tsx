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
import DragHandle from "@tiptap/extension-drag-handle-react";
import { EditorContent, useEditor } from "@tiptap/react";
import { BubbleMenu } from "@tiptap/react/menus";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Editor } from "@tiptap/core";

import { MenuIcon } from "../MenuIcon";
import { Tooltip } from "../Tooltip";
import { DOCUMENT_EXTENSIONS, markdownOf, preserveWrapping } from "./documentMarkdown";
import { SELECTION_TOOLS, TOOL_GROUPS, type EditorTool } from "./editorTools";
import { SlashMenu } from "./SlashMenu";
import { slashCommands, type SlashState } from "./slashCommands";

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

/** One toolbar button, wherever it is drawn. */
function ToolButton({ tool, editor }: { tool: EditorTool; editor: Editor }) {
  const on = tool.isOn(editor);
  const usable = tool.can ? tool.can(editor) : true;
  return (
    <Tooltip text={tool.keys ? `${tool.label} · ${tool.keys}` : tool.label}>
      <button
        type="button"
        className={on ? "doc-tool is-on" : "doc-tool"}
        aria-label={tool.label}
        aria-pressed={on}
        disabled={!usable}
        // Pointer down, not click: clicking takes focus out of the
        // document first, and a mark then applies to a selection
        // that has already collapsed.
        onPointerDown={(e) => {
          e.preventDefault();
          tool.run(editor);
        }}
      >
        {tool.icon ? (
          <MenuIcon name={tool.icon} />
        ) : (
          <span className={`doc-tool-letter is-${tool.key}`}>{tool.glyph}</span>
        )}
      </button>
    </Tooltip>
  );
}

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
  /** What the slash menu should be showing, or nothing. */
  const [slash, setSlash] = useState<SlashState | null>(null);

  // Built once. An extension list rebuilt on every render would
  // rebuild the editor with it, which throws away the document and
  // the cursor somebody is in the middle of using.
  const extensions = useMemo(
    () => [...DOCUMENT_EXTENSIONS, slashCommands({ onState: setSlash })],
    [],
  );

  const editor = useEditor({
    extensions,
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
      {/* The whole of what the editor can do, at the top, while
          editing. A bubble over the selection is quicker once you
          know the tools; it is no way to find out what they are. */}
      {!frozen && (
        <div className="doc-editor-bar" role="toolbar" aria-label="Formatting">
          {TOOL_GROUPS.map((group, i) => (
            <div className="doc-tool-group" key={group.key}>
              {i > 0 && <span className="doc-bar-gap" aria-hidden="true" />}
              {group.tools.map((tool) => (
                <ToolButton key={tool.key} tool={tool} editor={editor} />
              ))}
            </div>
          ))}
        </div>
      )}

      {/* And the same marks over the selection, for once you know
          them. Marks and headings only: inserting a table into a
          three-word selection is not what the selection meant. */}
      {!frozen && (
        <BubbleMenu editor={editor} className="doc-bubble">
          {SELECTION_TOOLS.map((tool) => (
            <ToolButton key={tool.key} tool={tool} editor={editor} />
          ))}
        </BubbleMenu>
      )}

      {/* The grip for moving a block. Left of the text and only on
          the block under the pointer, so a document at rest is a
          document rather than a list of handles. */}
      {!frozen && (
        <DragHandle editor={editor}>
          <div className="doc-drag-grip" aria-hidden="true">
            ⠿
          </div>
        </DragHandle>
      )}

      <EditorContent className="doc-editor-body" editor={editor} />

      <SlashMenu state={slash} />

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
