/**
 * Editing a document in place.
 *
 * **What is typed goes to the draft.** Not a version: the store is
 * append-only and the version list is on screen beside this, so an
 * editor that wrote one per save would put forty into an afternoon
 * and make that list useless. A version is what publishing the draft
 * makes, and nothing else makes one.
 *
 * **There is nothing to discard.** Cancel used to put back the text
 * that was loaded, which is safe while an edit is only yours and
 * wrong the moment somebody else is editing the same paragraph — the
 * draft is shared, so throwing it away throws away their work too.
 * Leaving the editor leaves the draft where it is.
 *
 * **Nothing here goes read-only while the draft saves.** It did, and
 * it cost the editor its focus every time: ``setEditable(false)``
 * blurs, so a save on a pause in typing put the cursor out of the
 * document a second after every word. Saving is a background fact,
 * not something that happens to the person typing.
 *
 * **The controls are in the header**, where Edit and Share were.
 * While somebody is editing, those two are not what they need and
 * the two that matter should not be at the bottom of a five-thousand
 * pixel document.
 *
 * **An archived document opens read-only.** Archiving is what this
 * app offers instead of deleting something the lineage depends on,
 * and that offer only means anything if the archived thing stops
 * changing. The editor is not the place to discover that: the button
 * says so before it is pressed.
 */
import Collaboration from "@tiptap/extension-collaboration";
import DragHandle from "@tiptap/extension-drag-handle-react";
import { EditorContent, useEditor } from "@tiptap/react";
import { BubbleMenu } from "@tiptap/react/menus";
import { useEffect, useMemo, useRef, useState } from "react";

import type { Editor } from "@tiptap/core";

import { MenuIcon } from "../MenuIcon";
import { Tooltip } from "../Tooltip";
import { DOCUMENT_EXTENSIONS, markdownOf, preserveWrapping } from "./documentMarkdown";
import { SELECTION_TOOLS, TOOL_GROUPS, type EditorTool } from "./editorTools";
import { SlashMenu } from "./SlashMenu";
import { slashCommands, type SlashState } from "./slashCommands";
import { DRAFT_FIELD, seedOnce } from "./useSharedDraft";

import type * as Y from "yjs";

export interface DocumentEditorProps {
  /** The markdown as it stands — the draft when there is one, and
   *  the current version when there is not.
   *
   *  When ``shared`` is given this is the *seed*: what the document
   *  should say if nobody has started writing it. Once somebody has,
   *  the shared document is the truth and this is ignored. */
  content: string;
  /** The draft as a thing two people can type into at once.
   *
   *  Present means every keystroke goes to everybody else with the
   *  document open, and theirs come back — so the editor's content
   *  comes from here rather than from ``content``. Absent means an
   *  ordinary editor on one machine, which is what a document nobody
   *  has shared gets. */
  shared?: Y.Doc | null;
  /** Frozen: archived documents do not take a new version. */
  frozen?: boolean;
  /** What is in the box, whenever it changes. The header holds the
   *  controls now, so it needs the text they act on — and the draft
   *  is written from here rather than from a Save button that no
   *  longer exists. */
  onChange?: (markdown: string, dirty: boolean) => void;
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
  shared = null,
  frozen = false,
  onChange,
}: DocumentEditorProps) {
  // The text this editor was opened on. Diffed against, so untouched
  // paragraphs keep the wrapping they arrived with.
  const opened = useRef(content);
  const [dirty, setDirty] = useState(false);
  /** What the slash menu should be showing, or nothing. */
  const [slash, setSlash] = useState<SlashState | null>(null);

  // Built once. An extension list rebuilt on every render would
  // rebuild the editor with it, which throws away the document and
  // the cursor somebody is in the middle of using.
  const extensions = useMemo(
    () => [
      ...DOCUMENT_EXTENSIONS,
      slashCommands({ onState: setSlash }),
      // Collaboration owns the document's history when it is on, so
      // undo has to come from it rather than from the starter kit —
      // two histories over one document undo each other's work.
      ...(shared ? [Collaboration.configure({ document: shared, field: DRAFT_FIELD })] : []),
    ],
    [shared],
  );

  const editor = useEditor(
    {
      extensions,
      // Not when collaborating. The shared document *is* the
      // content, and handing tiptap a second copy makes it insert
      // the whole thing again — once per window that opens.
      ...(shared ? {} : { content }),
      editable: !frozen,
      // Focused on open, at the start. Somebody who pressed Edit is
      // about to type, and a click to begin is a click that says
      // nothing.
      autofocus: frozen ? false : "start",
      // Dirty is computed from the text, not raised by the event.
      // ProseMirror emits an update while it is still settling the
      // document it was handed, so a flag set here is true before
      // the first keystroke — which would offer to save a draft
      // nobody has touched.
      //
      // Comparing also gets undo right: typing a word and taking it
      // back leaves the document unchanged, and no event-based flag
      // ever notices that.
      onUpdate: ({ editor: live }) => {
        const now = preserveWrapping(opened.current, markdownOf(live));
        const changed = now.trim() !== opened.current.trim();
        setDirty(changed);
        onChange?.(now, changed);
      },
    },
    // Rebuilt when the shared document arrives, and only then. The
    // extension list is what binds the editor to it, and an editor
    // built before it existed is bound to nothing.
    [shared],
  );

  // The first words, when there are none.
  //
  // A shared document that nobody has edited yet is empty, and the
  // text it should start from is the published version. Written
  // through the editor because the editor is what turns markdown
  // into nodes — and under the shared seeding id, so two windows
  // opening at the same moment write the same change rather than two
  // copies of the document. See ``seedOnce``.
  useEffect(() => {
    if (!editor || !shared || !content) return;
    seedOnce(shared, () => {
      editor.commands.setContent(content, { emitUpdate: false });
    });
  }, [editor, shared, content]);

  // Text landing underneath — the agent answering a comment,
  // usually — replaces what is on screen only while nothing has been
  // typed into it. Overwriting somebody's unsaved paragraph with a
  // newer one is the one thing this must not do.
  //
  // Solo only. A shared document gets the same news as a change
  // everybody receives, and writing it in from here as well would
  // put the whole text in a second time, on every screen.
  useEffect(() => {
    if (!editor || dirty || shared) return;
    if (content === opened.current) return;
    // Not while the cursor is in it. ``setContent`` rebuilds the
    // document and puts the cursor back at the start, which to
    // somebody mid-sentence is indistinguishable from the editor
    // throwing their place away.
    if (editor.isFocused) return;
    opened.current = content;
    editor.commands.setContent(content, { emitUpdate: false });
  }, [content, dirty, editor, shared]);

  useEffect(() => {
    editor?.setEditable(!frozen);
  }, [editor, frozen]);

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

      {frozen && (
        <div className="doc-editor-foot">
          <MenuIcon name="archive" />
          <span className="kb-hint">
            This document is archived. Restore it to make changes — everything built on
            it goes on resolving either way.
          </span>
        </div>
      )}
    </div>
  );
}
