# igni-service

What a service needs to be part of an igni deployment, behind one object.
Its own key, its registration, people's tokens, the directory,
notifications, erasure and export, sign-in to its own pages, and the
extension it ships — without a single igni address, token request or
signature in the service's code.

Building a service and its extension for the first time? Follow
[Your first extension](../../docs/YOUR_FIRST_EXTENSION.md) from an empty
directory to a working extension and service; this README is the
reference.

```python
from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from igni_service import (
    ExportSection, IgniService, IgniSettings, Notification, Person, Subject, Target,
)


class NewComment(BaseModel):
    doc: str
    text: str
    watchers: list[str] = []


def create_app(igni: IgniService | None = None) -> FastAPI:
    """The service. In a test `igni` is `FakeIgni().service()`; in production it is
    read from the environment (*Deploying*, below)."""
    igni = igni or IgniService.from_settings(IgniSettings())
    app = FastAPI(lifespan=igni.lifespan)
    igni.install(app)  # igni's calls, sign-in to this service's pages, calls from the extension's page

    # What this service keeps about people: their comments, by organisation and person.
    comments: dict[tuple[str, str], list[NewComment]] = {}

    @igni.on_erase
    async def erase(subject: Subject) -> None:
        comments.pop((subject.org_id, subject.user_id), None)

    @igni.on_export
    async def export(subject: Subject) -> list[ExportSection]:
        mine = comments.get((subject.org_id, subject.user_id), [])
        return [ExportSection(name="Comments", rows=[{"doc": c.doc, "text": c.text} for c in mine])]

    @app.post("/comments")
    async def comment(body: NewComment, person: Person = Depends(igni.person)) -> NewComment:
        mine = comments.setdefault((person.org_id, person.user_id), [])
        mine.append(body)
        await igni.host(person.org_id).notify(
            Notification(
                kind="comment",
                title=body.doc,
                body=f"{person.display_name}: {body.text}",
                actor=person.user_id,
                target=Target(page="acme.example", params={"doc": body.doc}),
                event_key=f"comment:{person.user_id}:{len(mine)}",
                recipients=body.watchers,
            )
        )
        return body

    @app.get("/mentions")
    async def mentions(query: str, person: Person = Depends(igni.person)) -> list[str]:
        people = await igni.host(person.org_id).directory.members(query=query)
        return [p.email for p in people if p.is_active]

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(person: Person = Depends(igni.web.person)) -> str:
        return f"<h1>Settings for {person.display_name}</h1>"

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(person: Person = Depends(igni.web.super_admin)) -> str:
        return "<h1>Every organisation</h1>"

    @app.get("/organisation/settings", response_class=HTMLResponse)
    async def org_settings(person: Person = Depends(igni.web.org_admin)) -> str:
        return f"<h1>{person.org_name}</h1>"

    return app
```

Run it with `uvicorn --factory service:create_app`. A factory rather than a
module-level `igni = IgniService.from_settings(...)`, so a test hands it
igni in-process instead (*Testing a service*, below):

<!-- continues -->
```python
import httpx

from igni_service.testing import FakeIgni

fake = FakeIgni()
fake.add_member("org-1", "u-ada", email="ada@acme.test", name="Ada")
fake.add_member("org-1", "u-max", email="max@acme.test", name="Max")
app = create_app(fake.service())

ada = {"Authorization": f"Bearer {fake.person_token('u-ada', org_id='org-1')}"}
igni_itself = {"Authorization": f"Bearer {fake.igni_token('org-1')}"}
transport = httpx.ASGITransport(app=app)
async with httpx.AsyncClient(transport=transport, base_url=fake.service_url) as http:
    sent = await http.post(
        "/comments", json={"doc": "runbook", "text": "Looks good", "watchers": ["u-max"]}, headers=ada
    )
    assert sent.status_code == 200, sent.text
    assert [d.user_id for d in fake.deliveries] == ["u-max"]

    exported = await http.get("/v1/igni/subjects/u-ada/export", headers=igni_itself)
    assert exported.json()["sections"][0]["rows"] == [{"doc": "runbook", "text": "Looks good"}]
    assert (await http.delete("/v1/igni/subjects/u-ada", headers=igni_itself)).status_code == 204
```

## Installing

> **Until the SDKs are on PyPI: build from a checkout.** `igni-service`
> and `igni-extension` are not published yet, so `uv add igni-service`
> finds nothing. Build both wheels from an igni checkout and install
> from them:
>
> ```bash
> git clone https://github.com/ignite-ember/igni.git
> uv build --wheel --out-dir ./sdk-wheels igni/packages/igni-service
> uv build --wheel --out-dir ./sdk-wheels igni/packages/igni-extension
> uv add igni-service --find-links ./sdk-wheels      # or: pip install --find-links ./sdk-wheels igni-service
> ```
>
> The bundle builder resolves the extension's dependencies the same way,
> so it needs the same directory:
> `igni-service-bundle --extension extension --out extension-bundle.tar.gz --find-links ./sdk-wheels`.
> It says so when it cannot find `igni-extension`.

Once published, from PyPI:

```bash
uv add igni-service        # or: pip install igni-service
```

Each release's wheels and sdists are also attached to igni's GitHub
release (`sdk-v<version>`), for a build that cannot reach PyPI. The
versions, and working against an igni checkout instead, are in igni's
`docs/PUBLISHING_THE_SDKS.md`.

## What happens on its own

`igni.lifespan` runs in the background, retrying with backoff while igni
is unreachable, so the service starts and serves either way:

- **Its key.** Generated on first start (RS256), sealed with the key
  encryption key, kept in the key store. Every replica signs with the same
  one; replicas starting together agree on one.
- **Registration.** On every start, with the key's public half. A restart
  finds the same registration.
- **The thumbprint** is logged in a banner, with the registration's state
  and what comes next: while it is `pending`, a super admin compares the
  thumbprint in igni's portal (*Services*) and approves the service;
  once `approved`, an org admin switches its extension on for a group;
  `revoked`, nothing works until a super admin decides otherwise.
- **The extension bundle** is uploaded, signed, whenever igni holds
  different bytes — after each asset it declares that igni does not hold
  yet, read from `extension-bundle.assets/` beside it. A new version of an
  approved service that asks for more than the approved one is **held**
  for a super admin, and the service logs igni's reason; machines keep the
  approved version until then.
- **Its log lines show under uvicorn.** uvicorn configures only its own
  loggers, so `igni.lifespan` gives `igni_service` a stderr handler at
  INFO when neither it nor the root logger has one. Configure logging
  yourself and it is left as you configured it.

Approval alone installs nothing anywhere. A person's igni installs the
extension when **both** hold: a super admin has approved the service, and
an org admin of their organisation has switched the extension on for
their group (each person is in exactly one). An org admin can only switch
on an approved service's extension.

While a super admin has not approved it, calls that need a service token
(the directory, notifications, events) raise `ServiceNotApproved`; once
revoked, `ServiceRevoked`. Both are `IgniUnavailable`, so a handler written
for that still catches them.

## People

- `Depends(igni.person)` — a person calling the service with the token igni
  gave them (from the extension on their machine). A `Person`: `user_id`,
  `org_id`, `org_name`, `org_role`, `email`, `name`, `super_admin`. Every
  check on the token is made here; a refused token is a 401, and a token
  that cannot be checked because igni is unreachable is a 503. The 401's
  body is always `Not authenticated`; its `WWW-Authenticate` says why, as
  RFC 6750 has it — bare `Bearer` when no token was sent,
  `Bearer error="invalid_token", error_description="the token has
  expired"` (or *is for another audience; this service's is …*, *the
  wrong kind of token*, *is not a JWT*, *is not signed by igni server's
  keys*, *was revoked*) — and the service logs the same at INFO.
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
  request header, such as a WebSocket greeting. Raises `TokenRefused`
  (its `reason` says why) or `IgniUnavailable`, so you answer in your own
  protocol.
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

## Apps and their spaces

An app — and each of its end users, a **space** — can call a service too,
with a token igni exchanged for the service's URL (`token_use` `app` or
`space`). A service takes people only unless it says more:

```python
from fastapi import Depends, FastAPI

from igni_service import (
    App, Caller, CallerKind, IgniService, IgniSettings, MemoryKeyStore, Person, Space,
)

igni = IgniService(
    IgniSettings(
        igni_server_url="https://igni.bank.internal",
        public_base_url="https://notes.bank.internal",
        service_name="Notes",
    ),
    key_store=MemoryKeyStore(),
    accepted_callers={CallerKind.PERSON, CallerKind.APP, CallerKind.SPACE},
)
app = FastAPI(lifespan=igni.lifespan)


@app.get("/v1/notes")
async def notes(caller: Caller = Depends(igni.caller)) -> list[str]:
    match caller:
        case Person(user_id=user_id):
            return [f"notes of {user_id}"]
        case App(app_id=app_id):
            return [f"notes the app {app_id} keeps"]
        case Space(app_id=app_id, external_id=user):
            return [f"notes of {user} in {app_id}"]
    return []
```

`Depends(igni.caller)` answers a typed `Person`, `App` or `Space` (with its
`app_id` and the app's `external_id` for its user) for the kinds the
service accepts, and a 401 for any other; `igni.person` stays people only
whatever the service accepts, so a route meant for people never takes an
app for one. `igni.caller_of(token)` is the same outside a request, and
`FakeIgni.app_token` / `space_token` make tokens for tests.

## What a VM needs: VM profile requests

> **Which call answers what.** An app: `app.vm_profile.request(wanted)`
> asks, `app.vm_profile.status()` says what became of the request
> (`pending`, `approved`, `rejected` with a reason), and
> `app.vm_profile.effective()` is what its VM gets now. A service:
> `igni.host(org_id).extension_vm_profile()` is what became of its
> extension's request in that organisation. The older names —
> `request_vm_profile`, `vm_profile_request`, `app.vm_profile()` and
> `host.vm_profile()` — still work and raise a `DeprecationWarning`
> naming the new call.

In igni's cloud every producer — a person, an app, an app's space — runs
in a microVM of its own, built from a **VM profile**: memory, vCPUs, idle
before a pause, its volume, and the **packages and setup script** baked
into the profile's read-only layer. Those run **as root** when the layer
is built, so the rule is: **a VM runs nothing as root that an org admin
has not approved.** An app or an extension that needs more — a headless
browser, a native library, more memory — **asks**, and igni keeps the
request **pending** until an org admin of that organisation approves it
on the portal's *VM profiles* page, where they see exactly what would run.

| Who asks | How | What approval does |
|---|---|---|
| an **app**, as itself | `app.vm_profile.request(...)`, with the app's own token | makes a profile from the request and assigns it to the app (its spaces' VMs get it too) |
| an **extension** | its manifest's `vm` section (see *The extension bundle*) | makes a profile from the request, which the admin gives to the groups and apps that use the extension |

A service is not an app: a service token names a service and an
organisation, never an app, so a service cannot ask for an app's VM. A
service reads what became of its own extension's request in an
organisation with `await igni.host(org_id).extension_vm_profile()` — `request.state`
(`pending`, `approved`, `rejected`, with the admin's `reason`), the
`profile` the approval made, and how many groups and apps were `assigned`
it.

The same request sent again changes nothing and keeps its state. A
**changed** request (anything in it — a package, the script, the memory)
is pending again, and **the VM keeps the profile it has until the admin
approves the change**. Every value is still subject to the platform's
caps: a request over them is refused (`VmProfileRefused`, status 400, in
igni's words). A field left out takes the deployment's Standard value.

### An app asking

An org admin registers the app on the portal (*Apps*) by pasting the
app's **public** key; the private half never leaves the app.

```python
from igni_service import IgniApp, MemoryKeyStore, VmPackages, VmProfileRequest
from igni_service.testing import FakeIgni

fake = FakeIgni()  # igni, in-process, for this example; below, what production writes
APP_ID = "app-review-bot"
app = fake.app(APP_ID, org_id="org-1")
# In production:
#   app = IgniApp(APP_ID, igni_server_url="https://igni.bank.internal", key_store=store)

async with app:
    print(await app.public_jwks())                   # once, for the admin to paste
    record = await app.vm_profile.request(VmProfileRequest(packages=VmPackages(apt=["jq"])))
    assert record.state == "pending"
    assert (await app.vm_profile.effective()).profile_name == "Standard"  # until approved

    fake.approve_vm_profile("org-1", app=APP_ID)     # the org admin, on the portal
    status = await app.vm_profile.status()
    assert status is not None and status.state == "approved"
    assert (await app.vm_profile.effective()).packages.apt == ["jq"]
```

`store` is any `KeyStore` — `SqlKeyStore` on the app's own database, as a
service's.

### Worked example: a headless browser (Chromium + Playwright MCP)

Playwright normally **downloads Chromium from the internet** (`npx
playwright install chromium`, or when a launch finds none). An
air-gapped installation cannot: the browser must come from **inside the
network**. Installing the npm packages downloads no browser (neither
`@playwright/mcp` nor `playwright` has an install script), so the
request below brings Chromium itself and never runs `playwright install`.

Where Chromium comes from is the part to get right. Fleet's VMs run
Ubuntu 24.04 (noble), and there `apt` `chromium` and `chromium-browser`
are only transitional packages for the **snap**, which does not work in
the chroot the layer is built in, nor in a Firecracker VM; Ubuntu's
archive has no real Chromium `.deb`. So either the bank's mirror carries
a real Chromium `.deb` (then name it under `apt`), or — as below — the
setup script places a Playwright Chromium build (`chrome-linux.zip`)
from inside the network.

How Fleet builds the layer (`image/profile/build_layer.sh` in
igni__cloudvm): the `apt` packages from the customer's mirror, with
`--no-install-recommends` (and `nodejs npm` added when there are npm
packages); pip and npm through the mirror or offline caches; then the
setup script as root in the chroot, with only `APT_MIRROR` in its
environment. A strict air-gapped build (`OFFLINE=1`) runs all of it with
no network at all, so the script can only read files — such as a
`file://` mirror directory, which is mounted at the same path.

```python
from igni_service import VmPackages, VmProfileRequest

# Runs as root in the layer's chroot, after the packages. Chromium comes
# from inside the network — never from Playwright's CDN.
SETUP = r"""#!/bin/sh
set -eu
case "${APT_MIRROR:-}" in
  file://*)  # an offline mirror directory: the only way under OFFLINE=1
    archive="${APT_MIRROR#file://}/playwright/chrome-linux.zip" ;;
  *)         # the bank's own artefact store
    archive=/tmp/chrome-linux.zip
    curl -fsSL -o "$archive" https://artifacts.bank.internal/playwright/chrome-linux.zip ;;
esac
mkdir -p /opt/chromium
unzip -q -o "$archive" -d /opt/chromium
# Where Playwright MCP's default channel, chrome, looks for the browser.
mkdir -p /opt/google/chrome
ln -sf /opt/chromium/chrome-linux/chrome /opt/google/chrome/chrome
"""

BROWSER = VmProfileRequest(
    memory_mib=4096,           # Chromium peaks well above igni's own needs
    normal_memory_mib=2048,
    vcpus=2,
    packages=VmPackages(
        # From the bank's Ubuntu mirror: what Chromium needs to run, and
        # what the setup script needs to place it.
        apt=[
            "fonts-liberation", "libnss3", "libatk-bridge2.0-0t64", "libgbm1",
            "libasound2t64", "libxkbcommon0", "libxcomposite1", "libxdamage1",
            "libxrandr2", "libpango-1.0-0", "libcups2t64",
            "ca-certificates", "curl", "unzip",
        ],
        # From the bank's npm mirror. Pinned: the admin approves this version.
        npm=["@playwright/mcp@0.0.84"],
    ),
    setup_script=SETUP,
)
```

An app sends it with `await app.vm_profile.request(BROWSER)`.

The agent then runs Playwright MCP as `playwright-mcp --headless
--isolated` (stdio). The flags are Playwright MCP's own: `--headless` is
required — it runs headed by default and the VM has no display;
`--isolated` keeps the browser profile in memory; add `--no-sandbox` if
the agent runs as root. Instead of the symlink, `--executable-path
/opt/chromium/chrome-linux/chrome` (or `PLAYWRIGHT_MCP_EXECUTABLE_PATH`)
names the browser directly. Replace `artifacts.bank.internal` with the
bank's own store; the admin reviewing the request sees that URL. An
extension asks for the same in its manifest:

```json
"vm": {
  "packages": {"apt": ["fonts-liberation", "libnss3", "…", "unzip"], "npm": ["@playwright/mcp@0.0.84"]},
  "setupScript": "#!/bin/sh\nset -eu\n…",
  "memoryMib": 4096,
  "normalMemoryMib": 2048,
  "vcpus": 2
}
```

### No admin calls here

This SDK cannot approve, reject, define or assign profiles, and has no
helper for those portal calls: they take an org admin's portal session
(`/v1/portal/...`), and neither a service token nor an app token is one —
nor is a person's token for a service, whose audience is the service's
URL, not igni's. Automation that holds an admin's credentials calls the
portal API directly (igni server's `docs/service-identity.md` §12).

In tests, `fake.app(app_id, org_id=...)` is an `IgniApp` wired to the
fake; `fake.approve_vm_profile(org_id, app=...)` (or `extension=...`) and
`fake.reject_vm_profile(..., reason=...)` play the org admin;
`fake.vm_profiles.caps` are the platform's caps. The request has to exist
first, as on the portal: an app's once it has asked
(`app.vm_profile.request`), an extension's once its bundle is on the fake
(`await fake.upload(bundle, igni)`) and the service has asked after it
(`await igni.host(org_id).extension_vm_profile()`). Approving one that
does not exist raises `NoVmProfileRequest` (a `LookupError`) saying which.

## Somebody removed

A person removed from an organisation (or deactivated, erased, their
sessions withdrawn), an app or a space deleted: nothing they hold works
from that moment, not for the ten minutes their token would still live
(igni's service-identity §21). While `igni.lifespan` runs, the service
follows the revocations of every organisation it serves on one cursor
and one long poll (`GET /v1/revocations?scope=served`, `If-None-Match`
answered with 304 when nothing is new), and refuses with a 401 a token
whose subject, kind and organisation a revocation names and whose
`iat_ms` is at or before its `not_before_ms`. Someone added back signs
in again, after it.

It reads them with a token for no organisation (`client_credentials`
with `scope=revocations`, good for these calls only), so it follows from
start-up.

The check **fails closed**: tokens are answered with a 503 until the
revocations have been read once, and whenever they go unrefreshed for
`IGNI_REVOCATIONS_MAX_STALENESS` seconds (30 by default). A service that
never runs `igni.lifespan` checks nothing. In tests,
`FakeIgni.publish_revocation("u-ada", org_id=…)` (`org_id=None` for every
organisation, `kind="app"` or `"space"` for those) publishes one.

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
- `agent = host.agent(producer)` — a person's or an app's agent
  (`Producer.person(user_id)`, `Producer.app(app_id)`), reached two ways.
  See *Reaching a producer's agent* below.
- `await host.send_event(producer, type, payload, event_key=..., expires_at=...)`
  — `host.agent(producer).send_event(...)`, kept as it was.
- `host.directory` — `await organisation()` (name, slug, `tenant_number`
  to derive per-organisation stores from, domains), `await
  members(query=..., limit=..., include_former=...)`, `await
  person(user_id)` (`None` for somebody never in the organisation),
  `await resolve([...])` (ids or addresses → members, keyed by both).
- `await host.extension_vm_profile()` — what became of this service's
  extension's VM profile request here (see *What a VM needs* above).

## Reaching a producer's agent

A service reaches a producer's igni — on their desktop, or in their
Fleet VM, woken for it — two ways, through igni server
(service-identity §15, igni's `docs/CLOUD.md` §6):

- **A message, for the agent.** igni posts it exactly as sent, as this
  service's extension's message: it starts a run, or waits for the run
  going. No extension code is involved.
- **An event, for the extension.** igni hands it to the extension's
  `@ext.on_service_event(type)` handler and posts nothing; the handler
  does what it wants through its host calls, and the agent runs only if
  it posts. An event no handler took reads `not_handled`.

Send a message when you know what the agent should read. Send an event
when the extension should decide — look something up in the producer's
igni, choose a chat, post or not.

```python
from igni_service import Attachment, Producer
from igni_service.testing import FakeIgni

fake = FakeIgni()  # igni, in-process: what a service holds in production is its IgniService
fake.add_member("org-1", "u-marieke", email="marieke@acme.test")
igni, org_id = fake.service(), "org-1"
pdf = b"%PDF-1.7 the claim form"
with open("damage.jpg", "wb") as photo:
    photo.write(b"\xff\xd8\xff a photo")

agent = igni.host(org_id).agent(Producer.person("u-marieke"))

# A message: posted as sent, in the chat of its thread.
await agent.send_message(
    "Claim CL-20431 arrived; assess the damage.",
    attachments=[Attachment.from_path("damage.jpg"), Attachment.from_bytes("form.pdf", pdf)],
    thread="claim:CL-20431",
    event_key="claim:CL-20431:new",
)

# A chat of its own, and what can be done in it.
chat = await agent.new_chat("Claim CL-20431", thread="claim:CL-20431")
await chat.send_message("The adjuster replied: see the report.")
await chat.queue("When you get to it, check the invoice.")     # the next run; starts nothing
await chat.show(                                                 # json-render; never sent to the model
    {"root": "r", "elements": {"r": {"type": "Text", "props": {"text": "Paid."}}}}
)
await chat.rename("Claim CL-20431 (paid)")
await chat.cancel()                                              # a working run; never one waiting on an approval

# An event: the extension's to act on.
accepted = await agent.send_event("index.ready", {"repo": "acme/api"})
status = await agent.status(accepted.event_id)   # pending, delivered, not_handled, expired, dropped, failed
```

- **Threads.** `thread` is this service's own key for a conversation.
  The first item of a thread opens a chat for it in the producer's igni
  (titled by `title`, else `"<extension>: <thread>"`), and every later
  one goes there. A message without one goes to this extension's own
  chat. Nothing a service sends lands in the chat the person has open.
  `agent.chat(thread)` is a handle on a thread's chat; `new_chat` without
  a thread makes one up.
- **Files.** `Attachment.from_bytes(filename, data)` or
  `Attachment.from_path(path)`: any type igni's composer takes, at most
  5 MiB each, at most 10 per message. They are uploaded before the
  message and kept by igni server only as long as the message; the
  producer's igni imports them into the chat as a person's upload would
  be. A message with files that arrives during a run waits for the run
  to end.
- **What became of it.** `await agent.status(event_id)`: `pending`;
  `delivered` (a message posted, or an event a handler took);
  `not_handled` (an event no handler took, with `not_handled_reason`);
  `expired` (14 days at most); `dropped` (the producer no longer has the
  extension); `failed` (the platform gave up). Every send answers an
  `EventAccepted` with its `event_id`; the same `event_key` for one
  producer is stored once, so a retry is exact with one.
- **Grants.** Beyond a plain event, the extension declares what its
  service may send, in its manifest — `"permissions": {"service":
  ["messages", "attachments", "chats"]}` — and the super admin approves it
  with the service. igni refuses the rest with `EventRefused` (403):
  `service.messages` for a message, `service.attachments` for files,
  `service.chats` for a thread, `new_chat` and a chat's actions.

What a service can do in a chat, against what igni can:

| In igni | From a service | Why not, where not |
|---|---|---|
| Post a message that starts or resumes a run | yes, `send_message` | |
| Queue a message for the next turn | yes, `chat.queue` | |
| Attach files | yes, any type, 5 MiB each | |
| Open a new chat, with a title | yes, `new_chat` | one per thread |
| Rename a chat | its own chats only | a person's chats are theirs to name |
| Show a view in the chat | yes, `chat.show`, its own chats | |
| Replace a view | not yet | show a new one |
| Cancel the run | its own chats, and not a run waiting on an approval | stopping that run would decide the approval |
| Cancel a sub-agent | no | its run ids never leave igni |
| Answer an approval, approve or dismiss a plan | **never** | a decision that is a person's |
| Read the chat, search it, its status or context | no | a service gets an item's status, not the chat's content |
| Truncate, compact, discard an interrupted run, extract learnings | no | they rewrite a person's history or memory |
| Open a chat on screen, move the UI | no | a service never moves what a person sees; it notifies |
| Switch model or permission mode, run a slash command | no | each changes what the agent may do |
| Post into a chat it did not open | no | its extension may, through its own host calls |

## Erasure and export

`igni.router` (mounted by `igni.install(app)`) answers igni's two calls
about one person, each with a token igni signs for this service
(`token_use` `igni`, the organisation in `org`):

| igni calls | Your handler | Answer |
|---|---|---|
| `DELETE /v1/igni/subjects/{user_id}` | `@igni.on_erase` | 204, whether or not anything was there |
| `GET /v1/igni/subjects/{user_id}/export` | `@igni.on_export` | 200, `{"sections": [{"name": …, "rows": [{…}]}]}` |

Without a handler the route answers 501, which igni reports as
unconfirmed under the service's name. In a test, `fake.igni_token(org_id)`
is the token igni sends (the opening example calls both).

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
from sqlalchemy.ext.asyncio import create_async_engine

from igni_service import IgniService, IgniSettings, KeyCipher, SqlKeyStore, SqlWebSessionStore

engine = create_async_engine("sqlite+aiosqlite:///service.db")  # the service's own database
cipher = KeyCipher(KeyCipher.generate())  # in production: KeyCipher(IGNI_KEY_ENCRYPTION_KEY)
igni = IgniService(
    IgniSettings(
        igni_server_url="https://igni.bank.internal",
        public_base_url="https://notes.bank.internal",
        service_name="Notes",
    ),
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
3.12 on macOS 26 and later (arm64), Linux x86_64 and Windows x86_64. The
same inputs give the same bytes. Needs `uv` and `pip`.

**Assets** — what the extension runs that is not Python, a database, a
JVM, a model — are declared in the manifest's `assets`, each pinned by
`size` and `sha256` with the `source` to fetch it from, for `any`
platform or one per platform. The builder fetches each into
`extension-bundle.assets/<sha256>` beside the archive, refusing bytes
that are not the pinned ones, and keeps one already there. They are not
inside the archive: the service uploads each separately, signed, and a
machine downloads only its own platform's. igni lays them out read-only
for the extension's process (`ext.assets` in `igni-extension`). The
wire is service-identity §8.2.

Every tool in the manifest's `permissions.tools` states MCP's four hints
(`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) —
igni's permission modes decide on them, and igni server refuses a bundle
that leaves one out — so the builder refuses it first.

**State the hints once, in the code.** An extension written with
`igni-extension` already states them on every `@ext.tool(hints=...)`.
Leave `permissions.tools` out of `manifest.json` and the builder writes
it into the bundle's manifest from the code (it runs
`python -m igni_extension.manifest` on the extension, offline, from the
wheelhouse it just built); your own file is left as it is. A manifest
that does list them is checked against the code instead: a listed tool
the code does not register, or a hint that differs, stops the build. A
tool the code registers and the manifest does not list is said in a
note: it is not offered to the agent, which is what you want for a tool
only the extension's page calls.

**Size.** The wheelhouse carries every dependency for all three
platforms; for an extension on `igni-extension` that is about 30 MB,
nearly all of it the MCP SDK's own dependencies three times over
(`cryptography`, `pydantic-core`, `pywin32`). `--platform linux-x86_64`
(repeatable: `macos-arm64`, `linux-x86_64`, `windows-x86_64`) builds for
some only — about 10 MB for Linux alone — and a machine on any other
platform then cannot install it, so use it only for a deployment whose
machines are all on those.

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

`FakeIgni()` approves nothing and needs nothing approved:
`require_approval=False` by default, so a service registering with it
gets service tokens at once. `FakeIgni(require_approval=True)` plays a
deployment whose super admin has not approved it yet — service tokens
raise `ServiceNotApproved` — until the test calls `fake.approve()`;
`fake.revoke()` plays them revoking it. `fake.hold_uploads = "reason"`
plays igni holding a new version for a super admin.

```python
import httpx
from fastapi import Depends, FastAPI

from igni_service import Notification, Person
from igni_service.testing import FakeIgni

fake = FakeIgni()
fake.add_member("org-1", "u-ada", email="ada@acme.test", name="Ada")
igni = fake.service(fake.settings(public_base_url="https://service.test"))  # wired to the fake
app = FastAPI()
igni.install(app)


@app.get("/settings")
async def settings(person: Person = Depends(igni.web.person)) -> dict[str, str]:
    return {"hello": person.name}


token = fake.person_token("u-ada", org_id="org-1")   # what the extension would send
igni_call = fake.igni_token("org-1")                 # what igni sends for erasure/export

transport = httpx.ASGITransport(app=app)
async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as browser:
    login = await browser.get(igni.web.login_path)   # a person opening the service's pages
    back = fake.authorize(login.headers["location"], user_id="u-ada", org_id="org-1")  # the portal
    await browser.get(back)                          # the service keeps a session
    assert (await browser.get("/settings")).json() == {"hello": "Ada"}
    await browser.post(igni.web.logout_path)

assert len(fake.revoked) == 1                        # the refresh token /auth/logout revoked

await igni.host("org-1").notify(Notification(kind="comment", title="Runbook", recipients=["u-ada"]))
assert [d.user_id for d in fake.deliveries] == ["u-ada"]
```

`await fake.upload(bundle, igni)` uploads an extension as the service does
at start-up — signed, over the fake's wire — from a built
`extension-bundle.tar.gz` or straight from the extension's directory, for
a test that needs it on igni (its VM profile request, say) without a
lifespan or a wheelhouse.

What a service sent producers' agents is `fake.events` (every kind) and
`fake.agents.uploads`; `fake.agents.settle(event_id, "delivered")` plays
the producer's igni taking one, so `agent.status` reads it, and
`fake.agents.grants = {"messages"}` plays an extension that declares
less.
