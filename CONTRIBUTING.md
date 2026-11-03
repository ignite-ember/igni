# Contributing to igni

Thank you for helping. igni is maintained by Ignite Ember and is Apache-2.0.
Bug reports, documentation fixes and pull requests are all welcome.

**Security problems are not issues.** Please report them privately, as
[SECURITY.md](SECURITY.md) describes — by email or through GitHub's private
vulnerability reporting ("Report a vulnerability" under the Security tab) —
and not in a public issue or pull request. Every copy of igni is installed
and upgraded by its owner, so a public report is exploitable against every
installation that has not upgraded yet.

## Security advisories

A fixed vulnerability is published as a **GitHub Security Advisory on
`ignite-ember/igni`** (the Security tab, then Advisories), with a CVE
identifier assigned through GitHub, which is a CVE Numbering Authority for
what it hosts, so the identifier is requested from the advisory itself.

1. A report arrives through [SECURITY.md](SECURITY.md): by email, or by
   private vulnerability reporting, which opens a draft advisory directly. An
   emailed report is copied into a draft advisory by us, and the draft is the
   working record from then on, private to the reporter and the maintainers.
2. The assessment fills in the draft: the affected versions (as
   `igni --version` prints them, as a range, and which clients), severity as
   a CVSS vector, the CWE, and the credit the reporter wants.
3. The fix is made in the advisory's temporary private fork, so it is not
   public before it ships, and released as a new version of every client.
4. A CVE is requested from the draft once the facts are settled.
5. The advisory is published after the fixed release is available, never
   before, and within the disclosure bound SECURITY.md states; if a fix
   cannot ship inside that bound, we ask the reporter for longer and say why,
   as SECURITY.md says. It names the fixed version, and the release notes
   name the advisory.

Watching the repository for security alerts is how to hear of one. Because
only the macOS desktop app updates itself, the advisory is what tells
everyone else to upgrade. The SDKs published from this repository
(`igni-service`, `igni-extension` and the `@ignite-ember` npm packages) are
covered by the same advisories. The paid modules are not public
repositories, so their customers are told directly instead. The policy is
igni server's, and so is the way it meets the Cyber Resilience Act's
reporting duties (Art. 14, Annex I Part II(4)), described in igni server's vulnerability management document.

## Before you start

For anything larger than a small fix, open an issue first and say what you
want to change and why. It saves you writing something we cannot take, for
example because it would make igni call out of the customer's network:
`tests/test_nothing_phones_home.py` and
`tests/test_an_air_gapped_install_makes_no_outbound_call.py` fail the build
if it does, and a new outbound call is off by default.

## Setting up

You need Python 3.12 (the version in `.python-version`),
[uv](https://docs.astral.sh/uv/), and Node 20 if you work on the clients.

```bash
git clone https://github.com/ignite-ember/igni.git
cd igni
uv sync --extra dev
uv run igni              # the CLI from your checkout
```

[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) covers the layout of the
repository and the clients in more detail.

## Checks

Run these before you open a pull request. CI (`.github/workflows/ci.yml`)
runs the same ones, and a pull request is merged only when they pass.

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/ember_code/
uv run pytest tests/ -q          # the whole Python suite
make packages-check              # the Python SDKs under packages/, each in its own env
```

If you touch the web UI (`clients/web`, which the desktop app and both IDE
plugins load):

```bash
cd clients/web
npm ci
npx tsc -b --noEmit
npx vitest run
npx playwright test              # the browser suite
```

The Python suite runs every test as if inside a customer's network: a test
that reaches the internet fails unless it carries the `reaches_the_internet`
marker. It also fails the run if a test leaves files in the repository root.

CI measures coverage too, and fails below a floor that only goes up: line and
branch coverage of `src/ember_code` (`--cov-fail-under` in the `test` job),
and the web suite's `coverage.thresholds` in `clients/web/vite.config.ts`
(`npx vitest run --coverage`). To see where you stand:

```bash
uv run pytest tests/ -q --cov=ember_code --cov-branch --cov-report=term-missing
cd clients/web && npx vitest run --coverage
```

## What a change needs

- **A test.** New behaviour comes with tests that exercise it, and a bug fix
  with the test that fails without the fix. A test that passes either way
  proves nothing.
- **The docs a reader would otherwise find wrong.** `docs/` is checked: every
  repository path it quotes must exist and every config key it documents
  must be a real setting (`tests/test_the_docs_name_real_things.py`).
- **The code standards.** [CODE_STANDARDS.md](CODE_STANDARDS.md) describes
  how the code here is written; review holds a change to it.

## Pull requests

- Keep one change per pull request, and say in the description what it
  changes and how you checked it.
- Write commit messages that say what changed for the person using igni and
  why, in sentences: a subject line, then a body with what was wrong, what
  the change does and how it was verified. `git log` has plenty of examples.
- Sign off every commit (see below).

## Developer Certificate of Origin

We do not ask for a contributor licence agreement. Instead, each commit
carries a `Signed-off-by` line, which certifies that you wrote the change or
otherwise have the right to submit it under the project's licence, as set
out in the [Developer Certificate of Origin 1.1](https://developercertificate.org/):

```
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` adds it for you, using your `user.name` and `user.email`
(`git config --global format.signOff true` does not cover `git commit`; use
`-s` or an alias).

Every pull request runs the DCO check (`.github/workflows/dco.yml`), which
fails on any commit in it without a sign-off and names that commit. To fix a
missed sign-off:

```bash
# the last commit only
git commit --amend -s --no-edit
# every commit on your branch
git rebase --signoff main
git push --force-with-lease
```

The force-push replaces your branch's commits with signed-off copies; the
check runs again on the new ones. Commits already on `main` before the check
existed are not re-checked.

Contributions are accepted under Apache-2.0, the licence of this repository
(its section 5, "inbound = outbound"). This is igni server's policy too.

## Conduct

Be respectful and assume good faith. We may remove comments, and block
people, that are abusive or harassing.
