# igni-service

What a service needs to be part of an igni deployment, behind one object.
Its own key, its registration, people's tokens, the directory,
notifications, erasure and export, sign-in to its own pages, and the
extension it ships — without a single igni address, token request or
signature in the service's code.

```python
from fastapi import Depends, FastAPI
from igni_service import (
    ExportSection, IgniService, IgniSettings, Notification, Person, Subject, Target,
)

igni = IgniService.from_settings(IgniSettings())
app = FastAPI(lifespan=igni.lifespan)
igni.install(app)  # igni's calls, sign-in to this service's pages, calls from the extension's page


@igni.on_erase
async def erase(subject: Subject) -> None:
    await documents.redact(org_id=subject.org_id, user_id=subject.user_id, email=subject.email)


@igni.on_export
async def export(subject: Subject) -> list[ExportSection]:
    return [ExportSection(name="Documents", rows=await documents.owned_by(subject.user_id))]


@app.post("/comments")
async def comment(body: NewComment, person: Person = Depends(igni.person)) -> Comment:
    saved = await comments.add(person, body)
    host = igni.host(person.org_id)
    await host.notify(
        Notification(
            kind="comment",
            title=saved.document_title,
            body=f"{person.display_name}: {saved.text}",
            actor=person.user_id,
            target=Target(page="acme.example", params={"doc": saved.doc, "comment": saved.id}),
            event_key=f"comment:{saved.id}",
            recipients=saved.watchers,
        )
    )
    return saved


@app.get("/mentions")
async def mentions(query: str, person: Person = Depends(igni.person)) -> list[str]:
    people = await igni.host(person.org_id).directory.members(query=query)
    return [p.email for p in people if p.is_active]


@app.get("/settings")
async def settings_page(person: Person = Depends(igni.web.person)) -> Page: ...


@app.get("/admin")
async def admin_page(person: Person = Depends(igni.web.super_admin)) -> Page: ...


@app.get("/organisation/settings")
async def org_settings(person: Person = Depends(igni.web.org_admin)) -> Page: ...
```

## Installing

From igni's package index, not PyPI. Name the index and pin the package
to it, so nothing called `igni-service` is ever taken from anywhere else:

```toml
[[tool.uv.index]]
name = "igni"
url = "https://us-central1-python.pkg.dev/ember-prod-491411/ember-prod-python/simple/"
explicit = true

[tool.uv.sources]
igni-service = { index = "igni" }
```

```bash
export UV_INDEX_IGNI_USERNAME=oauth2accesstoken
export UV_INDEX_IGNI_PASSWORD="$(gcloud auth print-access-token)"   # an hour
uv add igni-service
```

Your Google account needs read access to the index (igni's developers'
group has it). The versions, the other ways to authenticate, and working
against an igni checkout instead are in igni's
`docs/PUBLISHING_THE_SDKS.md`.

## What happens on its own

`igni.lifespan` runs in the background, retrying with backoff while igni
is unreachable, so the service starts and serves either way:

- **Its key.** Generated on first start (RS256), sealed with the key
  encryption key, kept in the key store. Every replica signs with the same
  one; replicas starting together agree on one.
- **Registration.** On every start, with the key's public half. A restart
  finds the same registration.
- **The thumbprint** is logged in a banner. A super admin compares it in
  igni's portal before approving the service.
- **The extension bundle** is uploaded, signed, whenever igni holds
  different bytes.

## People

- `Depends(igni.person)` — a person calling the service with the token igni
  gave them (from the extension on their machine). A `Person`: `user_id`,
  `org_id`, `org_name`, `org_role`, `email`, `name`, `super_admin`. Every
  check on the token is made here; a refused token is a 401, and a token
  that cannot be checked because igni is unreachable is a 503.
- `Depends(igni.org_admin)` — the same, for an admin of the organisation
  the token is for (`org_role == "admin"`); anybody else is a 403. Being a
  super admin does not make somebody an organisation's admin.
- `Depends(igni.optional_person)` — the same, or `None` when no token was
  sent: for routes that serve everybody and do more for somebody signed in.
  A token that was sent and is refused is still a 401.
- `Depends(igni.token)` / `Depends(igni.optional_token)` — the bearer token
  itself, unchecked (401 / `None` when none was sent), for a route that
  checks it later with `person_of` — one that turns an unusable token into
  an anonymous reader, say.
- `await igni.person_of(token)` — the same check for a token held outside a
  request header, such as a WebSocket greeting. Raises `TokenRefused` or
  `IgniUnavailable`, so you answer in your own protocol.
- `Depends(igni.web.person)` — a person signed in to the service's own
  pages. A page that needs somebody sends a stranger to sign in and back;
  an API call gets a 401. Their tokens stay on the server and are renewed
  before they expire; once igni refuses to renew, they are signed out.
  `igni.web.login_path` and `igni.web.logout_path` are what a page links
  and posts to. Signing out ends the session here and revokes its refresh
  token at igni, so the sign-in is over at igni too; if igni cannot be
  reached the person is still signed out here, and the refresh token simply
  expires.
- `Depends(igni.web.super_admin)` — the same, for super admins only.
- `Depends(igni.web.org_admin)` — the same, for admins of the organisation
  the person signed in to; 403 for anybody else, super admins included.
- `Depends(igni.web.signed_in)` — the person, or `None`: for a page
  anybody may read that says more to somebody signed in. Never sends
  anybody to sign in; a session igni cannot renew counts as nobody.

## Calls from the extension's page

`igni.install(app)` puts igni's routes on the app — `igni.router` (erasure,
export, a deleted organisation) and `igni.web.router` (sign-in) — and
`BearerCors`, which lets the extension's page call the service from igni's
window with `bridge.service.fetch`. Nothing to configure:

- A route that depends on `igni.person`, `igni.optional_person`,
  `igni.org_admin`, `igni.token` or `igni.optional_token` — directly,
  through a router's `dependencies`, or through the service's own
  dependency built on them — answers a call that
  carries `Authorization: Bearer` to any origin (`Access-Control-Allow-Origin: *`).
  The token is the whole of the authority; a refused token's 401 is
  readable too, so the page fetches a new one.
- Credentials are never allowed. A browser sends no cookies on such a call
  and shows a page nothing from one that sent them, so the service's
  website sessions (`igni.web.*`) are out of reach cross-origin.
- A route on `igni.web.*`, igni's own calls, and a route that takes no
  token give no cross-origin yes; a preflight for the sign-in routes or
  igni's calls is not answered with one either.
- A preflight is answered with `GET, HEAD, POST, PUT, PATCH, DELETE` and the
  headers `Authorization` and `Content-Type`, and only when the call will
  carry `Authorization`.

WebSockets are not CORS: a page's socket proves itself with its token in
the first frame (`igni.person_of`).

## igni, for one organisation

`host = igni.host(org_id)`:

- `await host.notify(*notifications)` — tell other people something.
  Recipients are user ids or addresses; igni keeps the active members of
  the organisation among them, never tells the actor, and tells each
  person about an `event_key` once. Any number of notifications go in as
  few calls as igni accepts, long recipient lists are split under the
  same `event_key`, and a call igni could not take is retried — exactly,
  when there is an `event_key`. Raises `IgniUnavailable` when igni stays
  away and `NotificationRefused` when igni says no; catch `IgniError` if
  notifying is best effort for you.
- `host.directory` — `await organisation()` (name, slug, `tenant_number`
  to derive per-organisation stores from, domains), `await
  members(query=..., limit=..., include_former=...)`, `await
  person(user_id)` (`None` for somebody never in the organisation),
  `await resolve([...])` (ids or addresses → members, keyed by both).

## Erasure and export

`@igni.on_erase` and `@igni.on_export` receive a `Subject`: the person's
`user_id` and `org_id`, and their `email` and `name` when igni's directory
can say. Erasure must be idempotent; igni repeats it until every service
confirms. A service without a handler answers that it has none, rather
than claiming an erasure it did not do — a service that holds nothing
about anybody registers a handler that does nothing.

## A deleted organisation

`@igni.on_organisation_deleted` receives the id of an organisation igni
deleted (`DELETE /v1/igni/organisations/{org_id}`, mounted by
`igni.router`). Stop routing to its data; keeping or dropping the data
itself is the service's decision. It must be idempotent: igni keeps
telling a service until it confirms, so a service that was down at the
time hears about it when it is back. A token for one organisation is
refused for another's path, and a service without a handler answers 501
— unconfirmed, so igni keeps asking.

In tests, `fake.delete_organisation(org_id)` removes the organisation from
the fake's directory and returns the call igni owes the service; `await
deletion.send(http)` delivers it.

## Deploying

| Variable | What |
|---|---|
| `IGNI_SERVER_URL` | Where this service reaches igni server (`IGNI_SERVER_BASE_URL` also works). |
| `PUBLIC_BASE_URL` | This service's own public address, on a hostname of its own. |
| `SERVICE_NAME` | What the service is called in igni's portal. |
| `IGNI_KEY_ENCRYPTION_KEY` | A Fernet key; `python -c "from igni_service import KeyCipher; print(KeyCipher.generate())"`. Changing it is a new identity, approved again. |
| `IGNI_KEY_DATABASE_URL` | An async SQLAlchemy URL for the key and sign-in sessions. |
| `IGNI_EXTENSION_BUNDLE` | The extension bundle this image carries. |

### Where the key and sessions are kept

`IgniService.from_settings` keeps both in `IGNI_KEY_DATABASE_URL`, in
tables of the SDK's own (`igni_service_key`, `igni_web_session`,
`igni_web_login`), created on first use. Rows are sealed with the key
encryption key; a session row is found by a hash of its cookie, so a copy
of the table signs nobody in.

To keep them in the service's own database instead, hand the stores over:

```python
engine = create_async_engine(settings.database_url)
cipher = KeyCipher(settings.key_encryption_key)
igni = IgniService(
    IgniSettings(),
    key_store=SqlKeyStore(engine, cipher),
    web_store=SqlWebSessionStore(engine, cipher),
)
```

A service that owns its schema in Alembic adds `IgniTables.metadata` to
`target_metadata` (`[Base.metadata, IgniTables.metadata]`) and passes
`create_table=False` to both stores. Any other place to keep them is a
`KeyStore` or `WebSessionStore` — small protocols in this package.

## The extension bundle

```
igni-service-bundle --extension extension --out extension-bundle.tar.gz
```

Builds what the service uploads: `manifest.json` (declaring the `python`
packaging), the extension's `README.md`, `plugin/` and `ui/`, and a
wheelhouse with the extension's wheel and every dependency's for CPython
3.12 on macOS arm64, Linux x86_64 and Windows x86_64. The same inputs give
the same bytes. Needs `uv` and `pip`.

Every tool in the manifest's `permissions.tools` states MCP's four hints
(`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) —
igni's permission modes decide on them, and igni server refuses a bundle
that leaves one out — so the builder refuses it first.

A dependency with no usable wheel stops the build, by name and platform.
One that is pure Python and publishes only an sdist is declared in the
extension's `pyproject.toml` and built from its sdist:

```toml
[tool.igni.bundle]
sdist-only = ["wikipedia"]
```

## Testing a service

`igni_service.testing.FakeIgni` is igni in-process: it signs tokens,
serves its keys, takes registrations and bundles, issues service tokens,
answers the directory, plays the portal's sign-in, revokes sign-ins, and
records notifications.

```python
from igni_service.testing import FakeIgni

fake = FakeIgni()
fake.add_member("org-1", "u-ada", email="ada@acme.test", name="Ada")
igni = fake.service()                                # IgniService wired to the fake

token = fake.person_token("u-ada", org_id="org-1")   # what the extension would send
igni_call = fake.igni_token("org-1")                 # what igni sends for erasure/export
back = fake.authorize(login_redirect, user_id="u-ada", org_id="org-1")  # the portal

assert [d.user_id for d in fake.deliveries] == ["u-ada"]
assert fake.revoked == [refresh_token]                # what /auth/logout revoked
```
