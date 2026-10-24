# igni-extension

An igni extension's process, as its author writes it: tools igni can
call, its own service reached as the signed-in person, and the host it
runs in — without an address, a token or a protocol message in the
extension's code.

```python
from igni_extension import IgniExtension, LocalNotification, NotSignedIn, Target

ext = IgniExtension("acme.example")


@ext.tool
async def summarise(slug: str) -> str:
    """Summarise one of the organisation's documents."""
    try:
        response = await ext.service.get(f"/documents/{slug}/summary")
    except NotSignedIn:
        return "Sign in to igni to read documents."
    response.raise_for_status()
    return response.json()["summary"]


@ext.tool
async def export(slug: str) -> str:
    """Export a document to PDF and say so when it is ready."""
    export_id = await render_pdf(slug)
    await ext.host.notifications.notify(
        LocalNotification(
            kind="export_ready",
            title=f"{slug}.pdf",
            body="Your export is ready",
            target=Target(page="acme.example", params={"export": export_id}),
            event_key=f"export:{export_id}",
        )
    )
    return export_id


def main() -> None:   # the console script the manifest's `python.entry` names
    ext.run()
```

## Its service: `ext.service`

An `httpx.AsyncClient` for the extension's own service, as the person
signed in to igni. Paths are relative to the service. The person's token
comes from the igni host, is attached to every request, and is renewed
before it expires — the extension never sees the person's igni session.
When nobody is signed in to igni, a request raises `NotSignedIn`.

## The host: `ext.host`

`ext.host` is igni as the extension sees it. Today it has one object:

- `await ext.host.notifications.notify(LocalNotification(...))` — tell
  the person at this machine something: an export is ready, an index
  finished. It never leaves the machine. igni shows it beside the
  notifications other people's actions caused, opens `target` when it is
  clicked, and shows a repeated `event_key` once. Sent before igni has
  connected, it is delivered as soon as igni does.

To tell *other* people something, the extension's service uses
`host.notify` in `igni-service`.

## Testing an extension

```python
from igni_extension.testing import FakeHostTokenEndpoint, HostHarness

host_tokens = FakeHostTokenEndpoint()
ext = IgniExtension(
    "acme.example",
    environ=host_tokens.environment(),
    token_transport=host_tokens.transport,
    service_transport=httpx.MockTransport(my_fake_service),
)

async with HostHarness(ext) as host:              # igni's side of the MCP session
    await host.session.call_tool("export", {"slug": "runbook"})
    [notification] = await host.received(1)
```

`FakeHostTokenEndpoint` hands out tokens the way the igni host does, and
answers as igni does when nobody is signed in (`host_tokens.sign_out()`).
`HostHarness` connects to the extension over the same MCP session
`ext.run()` serves, and records what it tells the host.
