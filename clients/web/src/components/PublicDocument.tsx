/**
 * A document somebody was sent a link to, read without an account.
 *
 * The sign-in wall is the right answer to opening igni with nothing
 * to open. It is the wrong answer to a link: a public document was
 * published to be read, and demanding an account on the *sender's*
 * self-hosted server before a recipient may read it makes the link
 * not a link. Worse, the wall took the ``?doc=`` out of the address
 * bar on its way past, so signing in afterwards did not even land
 * them on the document.
 *
 * So an arrival with a slug lands here instead, inside the app rather
 * than somewhere else, with the shell it will have once they sign in.
 *
 * **Read, and nothing else.** No tree, no search, no composer, no
 * comments — those belong to people with access, and most of them
 * have nothing to act on for somebody with no account anyway. What is
 * here is the document and a way in.
 *
 * **Rendered from Markdown, not from stored HTML.** The server keeps
 * a rendered page per share and it is tempting to drop it in; doing
 * so would put a document author's HTML inside the signed-in app's
 * origin. The reader app at its own origin can afford that and this
 * cannot, so this renders the same Markdown the reader app does,
 * through the same component the chat uses.
 */
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { IgniClient } from "../protocol/client";

type Answer = {
  ok?: string;
  title?: string;
  content?: string;
  error?: string;
};

export function PublicDocument({
  client,
  slug,
  onSignIn,
}: {
  client: IgniClient;
  slug: string;
  /** Leave the document and sign in; the rest of igni is behind it. */
  onSignIn: () => void;
}) {
  const [answer, setAnswer] = useState<Answer | null>(null);

  useEffect(() => {
    let live = true;
    client
      .rpc<Answer>("public_document", { slug })
      .then((a) => live && setAnswer(a ?? { error: "That link did not open." }))
      .catch(
        (e) =>
          live &&
          setAnswer({ error: e instanceof Error ? e.message : String(e) }),
      );
    return () => {
      live = false;
    };
  }, [client, slug]);

  return (
    <div className="pubdoc">
      <header className="pubdoc-bar">
        <span className="pubdoc-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none">
            <path
              d="M12 2.5c2.2 3.8 5.2 4.9 5.2 9a5.2 5.2 0 0 1-10.4 0c0-4.1 3-5.2 5.2-9z"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinejoin="round"
            />
          </svg>
        </span>
        {/* Said plainly. Somebody reading this should know they are
            seeing a shared copy and not their own workspace. */}
        <span className="pubdoc-note">Shared with you · read only</span>
        <button className="btn btn-sm btn-primary pubdoc-in" onClick={onSignIn}>
          Sign in
        </button>
      </header>

      <main className="pubdoc-page">
        {answer === null && <p className="pubdoc-wait">Opening…</p>}

        {answer?.error && (
          <div className="pubdoc-shut">
            <h1 className="pubdoc-shut-title">This link did not open</h1>
            <p className="pubdoc-shut-why">{answer.error}</p>
            {/* The one useful next step: it may be shared with them
                by name rather than publicly, in which case signing in
                is exactly what opens it. */}
            <button className="btn btn-primary" onClick={onSignIn}>
              Sign in and try again
            </button>
          </div>
        )}

        {answer?.ok && (
          <article className="pubdoc-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {answer.content ?? ""}
            </ReactMarkdown>
          </article>
        )}
      </main>
    </div>
  );
}
