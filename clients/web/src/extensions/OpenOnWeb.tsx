/**
 * "Open on the web": an extension's home page, in the person's browser.
 *
 * An extension igni server introduced belongs to a service, and the
 * service's URL is its `client_uri` — the client's home page, as RFC
 * 7591 defines it. The backend reports it as `web_url`
 * (`ember_code.extensions.web_home`); a development install reports its
 * `serviceUrl` setting when one is set. Nothing is shown without one.
 *
 * A plain external anchor, the way igni opens every external link: the
 * Tauri shell sends `http(s)` clicks to the OS browser, and the IDE
 * webviews open `target="_blank"` links outside themselves. `noopener`
 * because the page opened is not ours.
 */
export function OpenOnWeb({
  url,
  name,
}: {
  url: string | null | undefined;
  /** The extension's name, for the accessible label. */
  name: string;
}) {
  if (!url) return null;
  return (
    <a
      className="ext-web-link"
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={`Open ${name} on the web`}
      title={url}
    >
      Open on the web
    </a>
  );
}
