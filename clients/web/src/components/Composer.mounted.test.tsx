// @vitest-environment jsdom
/**
 * The composer, mounted.
 *
 * `Composer.test.ts` covers the pure helpers it exports and never renders
 * the component, which left the one surface every interaction goes through
 * at 5.9% coverage (F77). This file mounts the whole `Composer` — its real
 * `EditableInput`, a real `WindowModel` — against a stubbed `IgniClient`
 * and drives it the way a person does: put text in the editor and fire the
 * `input` event the browser would, press keys, paste, click.
 *
 * Every test asserts something observable from outside: what reached
 * `onSubmit` / `onStop` / `onTool` / `onPickModel` / `onPickMode`, which
 * RPCs the client was asked for and with what, and what the DOM shows.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

// ``clientState`` reads localStorage when it loads; Node 25's stub
// storage masks jsdom's, so install a real one first (same shim as
// ``clientState.test.tsx``).
vi.hoisted(() => {
  const data = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return data.size;
    },
    clear() {
      data.clear();
    },
    getItem(key: string) {
      return data.has(key) ? (data.get(key) as string) : null;
    },
    key(i: number) {
      return Array.from(data.keys())[i] ?? null;
    },
    removeItem(key: string) {
      data.delete(key);
    },
    setItem(key: string, value: string) {
      data.set(key, String(value));
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    writable: true,
    value: shim,
  });
});

// The host's indexed code search is an IDE feature; in a browser window it
// answers nothing and the composer falls back to the backend's
// ``search_code``, which is the path these tests drive.
const hostSearch = vi.hoisted(() => vi.fn(async (_snippet: string) => null as unknown));
vi.mock("../lib/host", () => ({ host: { searchCode: hostSearch } }));

import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import type React from "react";

import { WindowModel } from "../clientState";
import type { IgniClient } from "../protocol/client";
import { Composer, type SlashCommand, type ToolEntry } from "./Composer";

type Rpc = (method: string, params?: Record<string, unknown>) => Promise<unknown>;

interface Stub {
  client: IgniClient;
  rpc: ReturnType<typeof vi.fn<Rpc>>;
  completeFiles: ReturnType<typeof vi.fn<(q: string, limit?: number) => Promise<{ matches: string[]; total: number }>>>;
}

/** An `IgniClient` that answers the RPCs the composer makes. */
function stubClient(answers: Record<string, (params: Record<string, unknown>) => unknown> = {}): Stub {
  // The window model's revision: every write the backend accepts is a
  // newer one, as on the real backend. (A constant here makes a later
  // write's confirmation look stale, and the view falls back to an older
  // draft — a stub artefact, not composer behaviour.)
  let rev = 0;
  const rpc = vi.fn<Rpc>(async (method, params = {}) => {
    if (answers[method]) return answers[method](params);
    if (method === "window_model") return { rev: ++rev };
    throw new Error(`unexpected rpc ${method}`);
  });
  const completeFiles = vi.fn(async (_q: string, _limit?: number) => ({ matches: [] as string[], total: 0 }));
  const client = { rpc, completeFiles, clientId: "conn-test" } as unknown as IgniClient;
  return { client, rpc, completeFiles };
}

const TOOLS: ToolEntry[] = [
  { label: "Compact", command: "/compact", desc: "Summarize old context" },
  { label: "Sessions", command: "/sessions", desc: "List sessions" },
];

type Props = React.ComponentProps<typeof Composer>;

function mount(over: Partial<Props> = {}, stub: Stub = stubClient()) {
  const windowModel = new WindowModel(stub.client, "w-test", 0);
  const props: Props = {
    client: stub.client,
    connected: true,
    processing: false,
    skills: [],
    backendCommands: null,
    tools: TOOLS,
    windowModel,
    onPickModel: vi.fn(),
    onTool: vi.fn(),
    onTyping: vi.fn(),
    onSubmit: vi.fn(),
    onStop: vi.fn(),
    onPickMode: vi.fn(),
    ...over,
  };
  const view = render(<Composer {...props} />);
  const editor = view.container.querySelector('[role="textbox"]') as HTMLElement;
  const sendButton = () => view.container.querySelector(".send-split-go") as HTMLButtonElement | null;
  return { ...view, props, editor, windowModel, stub, sendButton };
}

/** Do what the browser does when a person types: the editor's DOM now
 *  holds `value`, the caret sits at its end, and `input` fires. */
function typeInto(editor: HTMLElement, value: string) {
  act(() => {
    editor.textContent = value;
    const node = editor.firstChild;
    const range = document.createRange();
    if (node) range.setStart(node, (node.textContent || "").length);
    else range.setStart(editor, 0);
    range.collapse(true);
    const sel = window.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
    fireEvent.input(editor);
  });
}

function press(editor: HTMLElement, key: string, init: Partial<KeyboardEventInit> = {}): boolean {
  let notPrevented = true;
  act(() => {
    notPrevented = fireEvent.keyDown(editor, { key, ...init });
  });
  return notPrevented;
}

/** Let pending promise chains (an awaited RPC, then a setState) finish. */
async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  });
}

/** requestAnimationFrame callbacks the composer schedules. */
async function nextFrame() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 20));
  });
}

afterEach(() => {
  cleanup();
  hostSearch.mockClear();
});

describe("sending", () => {
  it("Enter sends the trimmed text and empties the editor", () => {
    const { editor, props, windowModel } = mount();
    typeInto(editor, "  explain this module  ");

    const notPrevented = press(editor, "Enter");

    expect(notPrevented).toBe(false); // the newline is not inserted
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith("explain this module");
    expect(windowModel.get("composer")).toMatchObject({ text: "", mode: "chat" });
    expect(props.onTyping).toHaveBeenLastCalledWith("");
  });

  it("the send button sends what is in the editor", () => {
    const { editor, props, sendButton } = mount();
    typeInto(editor, "hello");
    expect(sendButton()!.disabled).toBe(false);

    act(() => sendButton()!.click());

    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith("hello");
  });

  it("Shift+Enter does not send and leaves the newline to the editor", () => {
    const { editor, props } = mount();
    typeInto(editor, "line one");

    const notPrevented = press(editor, "Enter", { shiftKey: true });

    expect(notPrevented).toBe(true);
    expect(props.onSubmit).not.toHaveBeenCalled();
  });

  it("whitespace alone is not a message", () => {
    const { editor, props, sendButton } = mount();
    typeInto(editor, "   ");

    press(editor, "Enter");

    expect(props.onSubmit).not.toHaveBeenCalled();
    expect(sendButton()!.disabled).toBe(true);
  });

  it("reports the draft as it is typed, so other views can mirror it", () => {
    const { editor, props, windowModel } = mount();
    typeInto(editor, "draft text");

    expect(props.onTyping).toHaveBeenLastCalledWith("draft text");
    expect(windowModel.get("composer").text).toBe("draft text");
  });
});

describe("while a turn is running", () => {
  it("shows Stop instead of Send, and Stop interrupts", () => {
    const { container, props, sendButton } = mount({ processing: true });

    expect(sendButton()).toBeNull();
    const stop = container.querySelector(".send-btn.stop") as HTMLButtonElement;
    expect(stop).not.toBeNull();
    act(() => stop.click());

    expect(props.onStop).toHaveBeenCalledOnce();
    expect(props.onSubmit).not.toHaveBeenCalled();
  });

  it("switches back to Send when the turn ends", () => {
    const { container, rerender, props, sendButton } = mount({ processing: true });
    rerender(<Composer {...props} processing={false} />);

    expect(container.querySelector(".send-btn.stop")).toBeNull();
    expect(sendButton()).not.toBeNull();
  });
});

describe("disabled states", () => {
  it("without a backend the editor is not editable and nothing can be sent", () => {
    const { editor, sendButton } = mount({ connected: false });

    expect(editor.getAttribute("contenteditable")).toBe("false");
    expect(editor.getAttribute("data-placeholder")).toBe("Connecting to backend…");
    expect(sendButton()!.disabled).toBe(true);
  });

  it("with a backend, Send waits for text", () => {
    const { editor, sendButton } = mount();

    expect(editor.getAttribute("contenteditable")).toBe("true");
    expect(editor.getAttribute("data-placeholder")).toBe(
      "Message igni — / commands, @ files, $ shell",
    );
    expect(sendButton()!.disabled).toBe(true);
  });

  it("names the product a group rebranded it to", () => {
    const { editor } = mount({ brandName: "Acme AI" });

    expect(editor.getAttribute("data-placeholder")).toContain("Message Acme AI");
  });
});

describe("@ file completion", () => {
  function withFiles(matches: string[], total = matches.length) {
    const stub = stubClient();
    stub.completeFiles.mockImplementation(async (_q, limit = 200) => ({
      matches: matches.slice(0, limit),
      total,
    }));
    return stub;
  }

  it("asks the backend for files matching the token under the caret", async () => {
    const stub = withFiles(["src/app.ts", "src/api.ts"]);
    const { editor, container } = mount({}, stub);

    typeInto(editor, "look at @src/a");
    await flush();

    expect(stub.completeFiles).toHaveBeenLastCalledWith("src/a", 200);
    const items = Array.from(container.querySelectorAll(".popup-item .cmd")).map((e) => e.textContent);
    expect(items).toEqual(["src/app.ts", "src/api.ts"]);
  });

  it("an @ inside a word is not a mention", async () => {
    const stub = withFiles(["x.ts"]);
    const { editor, container } = mount({}, stub);

    typeInto(editor, "mail me@host");
    await flush();

    expect(stub.completeFiles).not.toHaveBeenCalled();
    expect(container.querySelector(".popup-menu")).toBeNull();
  });

  it("arrows move the selection and Enter inserts the file as a pill instead of sending", async () => {
    const stub = withFiles(["src/app.ts", "src/api.ts"]);
    const { editor, container, props, windowModel } = mount({}, stub);
    typeInto(editor, "look at @src/a");
    await flush();

    press(editor, "ArrowDown");
    expect(container.querySelector(".popup-item.active .cmd")!.textContent).toBe("src/api.ts");
    press(editor, "Enter");
    await nextFrame();

    expect(props.onSubmit).not.toHaveBeenCalled();
    expect(windowModel.get("composer").text).toBe("look at @src/api.ts ");
    expect(container.querySelector(".popup-menu")).toBeNull();
    expect(editor.querySelector("[data-pill-path]")!.getAttribute("data-pill-path")).toBe("src/api.ts");

    press(editor, "Enter");
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith("look at @src/api.ts");
  });

  it("clicking an entry inserts it", async () => {
    const stub = withFiles(["README.md"]);
    const { editor, container, windowModel } = mount({}, stub);
    typeInto(editor, "@READ");
    await flush();

    act(() => {
      fireEvent.mouseDown(container.querySelector(".popup-item")!);
    });

    expect(windowModel.get("composer").text).toBe("@README.md ");
  });

  it("says how many more there are and loads the next page on scroll", async () => {
    const all = Array.from({ length: 450 }, (_, i) => `f${i}.ts`);
    const stub = withFiles(all, 450);
    const { editor, container } = mount({}, stub);
    typeInto(editor, "@f");
    await flush();

    expect(container.querySelectorAll(".popup-item")).toHaveLength(200);
    expect(container.querySelector(".popup-footer")!.textContent).toContain("Showing 200 of 450");

    const popup = container.querySelector(".popup-menu") as HTMLElement;
    // jsdom lays nothing out, so every size is 0: "near the bottom".
    fireEvent.scroll(popup);
    await flush();

    expect(stub.completeFiles).toHaveBeenLastCalledWith("f", 400);
    expect(container.querySelectorAll(".popup-item")).toHaveLength(400);
  });

  it("Escape closes the menu without leaving chat mode", async () => {
    const stub = withFiles(["a.ts"]);
    const { editor, container, windowModel } = mount({}, stub);
    typeInto(editor, "@a");
    await flush();
    expect(container.querySelector(".popup-menu")).not.toBeNull();

    const notPrevented = press(editor, "Escape");

    expect(notPrevented).toBe(false);
    expect(container.querySelector(".popup-menu")).toBeNull();
    expect(windowModel.get("composer")).toMatchObject({ text: "@a", mode: "chat" });
  });

  it("a failed lookup closes the menu rather than leaving a stale one", async () => {
    const stub = withFiles(["a.ts"]);
    const { editor, container } = mount({}, stub);
    typeInto(editor, "@a");
    await flush();
    stub.completeFiles.mockRejectedValueOnce(new Error("index not ready"));

    typeInto(editor, "@ab");
    await flush();

    expect(container.querySelector(".popup-menu")).toBeNull();
  });
});

describe("$ shell mode", () => {
  it("typing $ consumes it into a badge and sends the body as a shell command", () => {
    const { editor, container, props, windowModel } = mount();

    typeInto(editor, "$");
    expect(windowModel.get("composer")).toMatchObject({ text: "", mode: "shell" });
    expect(container.querySelector(".mode-badge")!.textContent).toBe("$ shell");
    expect(editor.textContent).toBe("");
    expect(editor.className).toContain("mode-shell");
    expect(editor.getAttribute("data-placeholder")).toContain("Shell command");

    typeInto(editor, "ls -la");
    expect(props.onTyping).toHaveBeenLastCalledWith("$ ls -la");
    press(editor, "Enter");

    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith("$ ls -la");
    expect(windowModel.get("composer").mode).toBe("chat");
    expect(container.querySelector(".mode-badge")).toBeNull();
  });

  it("Backspace on the last character leaves shell mode in one press", () => {
    const { editor, container, windowModel } = mount();
    typeInto(editor, "$");
    typeInto(editor, "l");

    const notPrevented = press(editor, "Backspace");

    expect(notPrevented).toBe(false);
    expect(windowModel.get("composer")).toMatchObject({ text: "", mode: "chat" });
    expect(container.querySelector(".mode-badge")).toBeNull();
  });

  it("Backspace with more text deletes normally and stays in shell mode", () => {
    const { editor, windowModel } = mount();
    typeInto(editor, "$");
    typeInto(editor, "ls");

    const notPrevented = press(editor, "Backspace");

    expect(notPrevented).toBe(true);
    expect(windowModel.get("composer").mode).toBe("shell");
  });

  it("Escape abandons the shell command", () => {
    const { editor, windowModel, props } = mount();
    typeInto(editor, "$");
    typeInto(editor, "rm -rf build");

    press(editor, "Escape");

    expect(windowModel.get("composer")).toMatchObject({ text: "", mode: "chat" });
    expect(props.onSubmit).not.toHaveBeenCalled();
  });

  it("Escape in plain chat with no menu is left for the app (cancel run)", () => {
    const { editor } = mount();
    typeInto(editor, "hi");

    expect(press(editor, "Escape")).toBe(true);
  });
});

describe("/ command mode", () => {
  const backend: SlashCommand[] = [
    { name: "/help", description: "backend help" },
    { name: "/plugin", description: "backend plugin" },
    { name: "/plugins", description: "backend plugins" },
    { name: "/review", description: "Review a PR" },
  ];

  it("typing / opens the command menu from the backend's list, with the GUI's wording", () => {
    const { editor, container, windowModel } = mount({ backendCommands: backend });

    typeInto(editor, "/");

    expect(windowModel.get("composer").mode).toBe("command");
    expect(container.querySelector(".mode-badge.command")!.textContent).toBe("/ command");
    const rows = Array.from(container.querySelectorAll(".popup-item")).map((r) => [
      r.querySelector(".cmd")!.textContent,
      r.querySelector(".desc")?.textContent,
    ]);
    expect(rows).toContainEqual(["/help", "Show available commands"]);
    expect(rows).toContainEqual(["/review", "Review a PR"]);
    // Client-only intercept still offered; a hardcoded name the backend
    // does not know is not.
    expect(rows.map((r) => r[0])).toContain("/workflows");
    expect(rows.map((r) => r[0])).not.toContain("/bug");
  });

  it("Enter on a fully typed command runs it in one press", () => {
    const { editor, props } = mount({ backendCommands: backend });
    typeInto(editor, "/");
    typeInto(editor, "plugin");

    press(editor, "Enter");

    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith("/plugin");
  });

  it("Tab completes the highlighted command without sending", () => {
    const { editor, props, windowModel } = mount({ backendCommands: backend });
    typeInto(editor, "/");
    typeInto(editor, "rev");

    press(editor, "Tab");

    expect(props.onSubmit).not.toHaveBeenCalled();
    expect(windowModel.get("composer")).toMatchObject({ text: "review ", mode: "command" });
  });

  it("skills are offered beside the commands", () => {
    const { editor, container } = mount({
      skills: [{ name: "/deploy-docs", description: "Publish the docs" }],
    });
    typeInto(editor, "/");
    typeInto(editor, "depl");

    const rows = Array.from(container.querySelectorAll(".popup-item .cmd")).map((e) => e.textContent);
    expect(rows).toEqual(["/deploy-docs"]);
  });

  it("a query that matches nothing closes the menu", () => {
    const { editor, container } = mount({ backendCommands: backend });
    typeInto(editor, "/");
    typeInto(editor, "zzz");

    expect(container.querySelector(".popup-menu")).toBeNull();
  });
});

describe("input history", () => {
  it("ArrowUp recalls earlier messages, in their mode, and ArrowDown returns to the draft", () => {
    const { editor, windowModel } = mount();
    typeInto(editor, "first");
    press(editor, "Enter");
    typeInto(editor, "$");
    typeInto(editor, "make test");
    press(editor, "Enter");

    typeInto(editor, "half-written");
    press(editor, "ArrowUp");
    expect(windowModel.get("composer")).toMatchObject({ text: "make test", mode: "shell" });
    press(editor, "ArrowUp");
    expect(windowModel.get("composer")).toMatchObject({ text: "first", mode: "chat" });
    press(editor, "ArrowDown");
    expect(windowModel.get("composer")).toMatchObject({ text: "make test", mode: "shell" });
    press(editor, "ArrowDown");
    expect(windowModel.get("composer")).toMatchObject({ text: "half-written", mode: "chat" });
  });

  it("sending the same message twice keeps one history entry", () => {
    const { editor, windowModel } = mount();
    for (const t of ["again", "again"]) {
      typeInto(editor, t);
      press(editor, "Enter");
    }

    press(editor, "ArrowUp");
    press(editor, "ArrowUp");

    expect(windowModel.get("composer").text).toBe("again");
  });
});

describe("pasting code", () => {
  const SNIPPET = "def hello():\n    print('hi')\n    return 42\n";

  function paste(editor: HTMLElement, text: string, files: File[] = []) {
    let notPrevented = true;
    act(() => {
      notPrevented = fireEvent.paste(editor, {
        clipboardData: { getData: () => text, files },
      });
    });
    return notPrevented;
  }

  it("a snippet found in the repo becomes a code pill, and is sent as a code-paste block", async () => {
    const stub = stubClient({
      search_code: () => ({
        matches: [
          { path: "src/hello.py", line: 9, end_line: 11, preview: "def hello():" },
          { path: "src/hello.py", line: 12, preview: "" },
          { path: "tests/test_hello.py", line: 3, preview: "" },
        ],
      }),
    });
    const { editor, props, windowModel } = mount({}, stub);
    typeInto(editor, "why does");

    const notPrevented = paste(editor, SNIPPET);
    await flush();
    await nextFrame();

    expect(notPrevented).toBe(false);
    expect(hostSearch).toHaveBeenCalledWith(SNIPPET);
    expect(stub.rpc).toHaveBeenCalledWith("search_code", { snippet: SNIPPET });
    expect(windowModel.get("composer").text).toMatch(/@code:c1/);
    expect(windowModel.get("composer").text).not.toContain("print('hi')");
    const pill = editor.querySelector("[data-pill-code]") as HTMLElement;
    expect(pill.getAttribute("data-pill-label")).toBe("hello.py 9-12 +1");

    press(editor, "Enter");

    const sent = (props.onSubmit as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(sent).toContain(`[code-paste src/hello.py:9 lines=9-12]\n${SNIPPET}\n[/code-paste]`);
  });

  it("a snippet the repo does not contain stays as plain text", async () => {
    const stub = stubClient({ search_code: () => ({ matches: [] }) });
    const { editor, props, windowModel } = mount({}, stub);

    paste(editor, SNIPPET);
    await flush();

    expect(windowModel.get("composer").text).toBe(SNIPPET);
    expect(editor.querySelector("[data-pill-code]")).toBeNull();
    press(editor, "Enter");
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(SNIPPET.trim());
  });

  it("a short or single-line paste is left to the browser and searches nothing", () => {
    const stub = stubClient();
    const { editor } = mount({}, stub);

    expect(paste(editor, "just a word")).toBe(true);
    expect(stub.rpc).not.toHaveBeenCalledWith("search_code", expect.anything());
  });

  it("a failed search keeps the pasted text", async () => {
    const stub = stubClient({
      search_code: () => {
        throw new Error("rg missing");
      },
    });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { editor, windowModel } = mount({}, stub);

    paste(editor, SNIPPET);
    await flush();

    expect(windowModel.get("composer").text).toBe(SNIPPET);
    warn.mockRestore();
  });

  it("pasting a file uploads it and references it as @path", async () => {
    const stub = stubClient({
      upload_attachment: (p) => ({ path: `.igni/attachments/s1/${p.filename}`, size: 3 }),
    });
    const { editor, windowModel } = mount({}, stub);
    typeInto(editor, "see");

    paste(editor, "", [new File(["abc"], "notes.txt")]);
    await waitFor(() =>
      expect(windowModel.get("composer").text).toBe("see @.igni/attachments/s1/notes.txt "),
    );

    expect(stub.rpc).toHaveBeenCalledWith("upload_attachment", {
      filename: "notes.txt",
      content_base64: btoa("abc"),
    });
  });
});

describe("attachments", () => {
  it("a dropped file is uploaded; the message waits for it", async () => {
    let finish: (v: unknown) => void = () => {};
    const stub = stubClient({
      upload_attachment: () => new Promise((r) => (finish = r)),
    });
    const { editor, container, props, windowModel } = mount({}, stub);
    typeInto(editor, "with file");

    act(() => {
      fireEvent.drop(container.querySelector(".composer-zone")!, {
        dataTransfer: { files: [new File(["x"], "a.png")], types: ["Files"] },
      });
    });
    await waitFor(() => expect(container.querySelector(".composer-chip-attach.uploading")).not.toBeNull());
    expect(container.querySelector(".composer-chip-name")!.textContent).toBe("a.png");

    press(editor, "Enter");
    expect(props.onSubmit).not.toHaveBeenCalled();

    await act(async () => finish({ path: "att/a.png", size: 1 }));
    await waitFor(() => expect(container.querySelector(".composer-chip-attach")).toBeNull());
    expect(windowModel.get("composer").text).toBe("with file @att/a.png ");
  });

  it("dragging files over the composer highlights it", () => {
    const { container } = mount();
    const zone = container.querySelector(".composer-zone")!;

    act(() => {
      fireEvent.dragOver(zone, { dataTransfer: { types: ["Files"], dropEffect: "none" } });
    });
    expect(zone.className).toContain("drag-hot");

    act(() => {
      fireEvent.dragLeave(zone);
    });
    expect(zone.className).not.toContain("drag-hot");
  });

  it("a file over 5 MB is refused without an upload", async () => {
    const stub = stubClient();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { container } = mount({}, stub);
    const big = new File(["x"], "big.bin");
    Object.defineProperty(big, "size", { value: 6 * 1024 * 1024 });

    act(() => {
      fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [big] } });
    });
    await waitFor(() => expect(container.querySelector(".composer-chip-attach")).toBeNull());

    expect(stub.rpc).not.toHaveBeenCalledWith("upload_attachment", expect.anything());
    warn.mockRestore();
  });

  it("the + button opens the file picker", () => {
    const { container } = mount();
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const click = vi.spyOn(input, "click").mockImplementation(() => {});

    act(() => (container.querySelector(".attach-btn") as HTMLButtonElement).click());

    expect(click).toHaveBeenCalledOnce();
  });
});

describe("the bar's menus", () => {
  it("the / button lists tools and runs the one picked", () => {
    const { container, props } = mount();

    act(() => (container.querySelector(".slash-btn") as HTMLButtonElement).click());
    const items = Array.from(container.querySelectorAll(".popup-item"));
    expect(items.map((i) => i.querySelector(".cmd")!.textContent)).toEqual(["Compact", "Sessions"]);
    act(() => (items[1] as HTMLElement).click());

    expect(props.onTool).toHaveBeenCalledExactlyOnceWith("/sessions");
    expect(container.querySelector(".popup-menu")).toBeNull();
  });

  it("the model chip lists the registry, marks the current model, and picks one", async () => {
    const stub = stubClient({
      get_model_registry: () => ({ registry: { "zeta-7b": {}, "alpha-27b": {} }, default: "zeta-7b" }),
    });
    const { container, props } = mount({ model: "zeta-7b" }, stub);

    act(() => (container.querySelector(".composer-model") as HTMLButtonElement).click());
    await flush();

    const rows = Array.from(container.querySelectorAll(".model-menu .popup-item"));
    expect(rows.map((r) => r.querySelector(".cmd")!.textContent)).toEqual(["alpha-27b", "zeta-7b"]);
    expect(rows[1].className).toContain("active");
    act(() => (rows[0] as HTMLElement).click());

    expect(props.onPickModel).toHaveBeenCalledExactlyOnceWith("alpha-27b");
    expect(container.querySelector(".model-menu")).toBeNull();
  });

  it("the /model command opens the same picker, and Escape closes it", async () => {
    const stub = stubClient({
      get_model_registry: () => ({ registry: { m1: {} }, default: "m1" }),
    });
    const { container } = mount({ modelMenuSignal: { n: 1 } }, stub);
    await flush();
    expect(container.querySelector(".model-menu")).not.toBeNull();

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });

    expect(container.querySelector(".model-menu")).toBeNull();
  });

  it("no model chip without a model; an unavailable registry opens nothing", async () => {
    const stub = stubClient({
      get_model_registry: () => {
        throw new Error("offline");
      },
    });
    const bare = mount({}, stub);
    expect(bare.container.querySelector(".composer-model")).toBeNull();
    bare.unmount();

    const { container } = mount({ model: "m" }, stub);
    act(() => (container.querySelector(".composer-model") as HTMLButtonElement).click());
    await flush();
    expect(container.querySelector(".model-menu")).toBeNull();
  });

  it("the mode half of Send shows the current mode and switches to another", () => {
    const { container, props } = mount({ permissionMode: "acceptEdits" });
    const trigger = container.querySelector(".send-split-mode") as HTMLButtonElement;
    expect(trigger.textContent).toContain("Auto-edit");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");

    act(() => trigger.click());
    const items = Array.from(container.querySelectorAll('[role="menuitem"]'));
    expect(items.map((i) => i.querySelector(".cmd")!.textContent)).toEqual([
      "Execute",
      "Plan",
      "Auto-edit",
      "Bypass",
    ]);

    // Picking the mode already in force is not a change.
    act(() => (items[2] as HTMLElement).click());
    expect(props.onPickMode).not.toHaveBeenCalled();

    act(() => trigger.click());
    act(() => (container.querySelectorAll('[role="menuitem"]')[1] as HTMLElement).click());
    expect(props.onPickMode).toHaveBeenCalledExactlyOnceWith("plan");
    expect(container.querySelector('[role="menu"]')).toBeNull();
  });
});

describe("embedding", () => {
  it("renders the approval form inside the composer box", () => {
    const { container } = mount({ hitlSlot: <div data-testid="hitl">Approve?</div> });

    expect(container.querySelector(".composer .composer-hitl [data-testid=hitl]")).not.toBeNull();
  });

  it("hands the app a focus function, and takes it back on unmount", async () => {
    const focusRef: React.MutableRefObject<(() => void) | null> = { current: null };
    const { editor, unmount } = mount({ focusRef });
    expect(focusRef.current).toBeTypeOf("function");

    act(() => focusRef.current!());
    await nextFrame();
    expect(document.activeElement).toBe(editor);

    unmount();
    expect(focusRef.current).toBeNull();
  });

  it("shows a draft another view put in the model", () => {
    const { editor, windowModel } = mount();

    act(() => windowModel.write("composer", { text: "from an extension", mode: "chat", attachments: [] }));

    expect(editor.textContent).toBe("from an extension");
  });
});
