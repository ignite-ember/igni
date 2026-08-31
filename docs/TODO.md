# Things worth doing, not yet done

Ideas that survived a conversation but not a schedule. Each entry says
what to build, and — more importantly — what evidence made it look
worth building, so a later reader can decide whether the reason still
holds.

---

## Run the group-config checks against a live server in CI

**What.** Wire `scripts/check_group_config.py` and
`scripts/check_group_session.py` into the release-readiness workflow,
against a throwaway ember-server stood up in the job: start Postgres and
the API, seed a Default group holding one entry of every kind, run both
scripts, tear it down. Both already exit non-zero on failure, so they
can gate a release as-is.

**Why.** These two scripts close the one class of bug that unit testing
here has never caught: **wiring between components**. Each subsystem's
loader has tests, and each of those tests passes by handing the loader a
directory. Nothing asserted that the session actually *hands it that
directory*. Twice now that gap has shipped a real fault:

- Group MCP servers never reached the session at all. `MCPClientManager`
  built its own loader and `apply_to_mcp` only merged plugin servers.
  Every MCP unit test passed. Booting a real `Session` found it in
  seconds.
- The ETag on `/me/group/pack` could never match, because it hashed
  `fetched_at` — which is `now()`. The revalidation tests passed; they
  compared the client's behaviour against a fixture, not against a
  server computing its own tag.

Both are invisible to a mock and obvious to a live boot. The scripts
exist because of them; running them by hand only helps on the days
somebody remembers to.

**Where it belongs.** `release-readiness.yml`, not `ci.yml`. Same
argument the existing onboarding smoke makes for itself: it needs real
infrastructure, takes minutes not seconds, and the thing it catches
drifts on its own schedule rather than per-PR. Nightly plus on-tag is
the right cadence.

**The awkward part.** The job needs both repos — ember-code for the
scripts, ember-server for the thing to point them at. Either check out
ember-server as a second path in the same job, or publish a server
container image the job can `docker run`. The image is cleaner and
matches how a customer actually gets the server; the two-checkout
version is faster to write. Start with the checkout, move to the image
if it gets fragile.

---

## Reshape where configuration lives

See **[CONFIG_LAYOUT_PLAN.md](CONFIG_LAYOUT_PLAN.md)** — four decisions
that turn out to be one change: rename `.ember` to `.igni`, stop the
server's pack landing in the project, let a project file of the same
name win, and move everything sensitive to `~/.igni`.

The plan's verification section is a stronger argument for the CI item
above than the one written there: the failures it describes are a loader
whose search order is right in a fixture and wrong in a real session, a
credential resolving from the wrong tier after the config split, and a
home-directory migration that by definition runs once on a machine that
already has data. None of those are reachable from unit tests.
