# Contributing to igni

Thank you for helping. igni is maintained by Ignite Ember and is Apache-2.0.
Bug reports, documentation fixes and pull requests are all welcome.

**Security problems are not issues.** Please report them privately, as
[SECURITY.md](SECURITY.md) describes — by email or through GitHub's private
vulnerability reporting ("Report a vulnerability" under the Security tab) —
and not in a public issue or pull request. Every copy of igni is installed
and upgraded by its owner, so a public report is exploitable against every
installation that has not upgraded yet.

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

`git commit -s` adds it for you, using your `user.name` and `user.email`. To
sign off commits you have already made on a branch:

```bash
git rebase --signoff main
```

Contributions are accepted under Apache-2.0, the licence of this repository
(its section 5, "inbound = outbound"). This is igni server's policy too; the
pull-request check that enforces it there is not yet enabled here, so a
reviewer asks for the sign-off instead.

## Conduct

Be respectful and assume good faith. We may remove comments, and block
people, that are abusive or harassing.
