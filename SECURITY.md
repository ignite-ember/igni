# Reporting a security problem

Ignite Ember maintains igni. This file is where to send a security report,
what to put in it, and what happens next. The policy is the same one igni
server publishes; what differs below is what igni is — software that runs on
a developer's own machine — and therefore what a report about it looks like.

For how igni protects the machine it runs on (permissions, protected paths,
the backend's local WebSocket), read [docs/SECURITY.md](docs/SECURITY.md).
This file is about reporting a way around it.

## Where to send it

| | |
|---|---|
| Address | `dmytro.zezyk@ignite-ember.sh`, or GitHub's private vulnerability reporting on this repository ("Report a vulnerability" under the Security tab) |
| PGP key | A PGP key is not yet published. If a report must not travel in plain email, say so in a first message without the details, and we will agree a channel. |
| Acknowledgement | We acknowledge every report within **3 business days** (Europe/Amsterdam). |
| Assessment | We aim for a first assessment — whether we can reproduce it, how severe we think it is, and what we plan to do — within **10 business days**. Fix times then follow the targets in [docs/VULNERABILITY_MANAGEMENT.md](docs/VULNERABILITY_MANAGEMENT.md). |
| Disclosure | **Coordinated.** Details are disclosed **90 days after your report or when the fix ships, whichever is first** — later only if you agree to it. We ask you to keep them private until then, credit you in the advisory if you want to be credited, and if we need longer we ask you, and say why. |
| Safe harbour | If you act in good faith — you test only against your own installation or one you are authorised to test, avoid privacy violations, data destruction and service disruption, do not use more access than you need to show the problem, and give us a reasonable time to fix it before disclosure — we will not pursue or support legal action against you for your research, and we will say so publicly if anybody else does. |

We are a small vendor. The acknowledgement time is a commitment and the
assessment time is a target we hold ourselves to; if either slips, we tell
you rather than going quiet.

## Please do not open a public issue first

The source is public, so it is fair to ask why. Because every copy of igni
is **installed** — from PyPI, Homebrew, the VS Code and JetBrains
marketplaces, or a desktop installer — and each one is upgraded when its
owner gets round to it. A public report is exploitable against every
installation that has not upgraded yet, and there is no vendor-operated
service anybody can patch centrally to protect them. Only the macOS desktop
app updates itself.

So a fix is only half of a remediation: the other half is people knowing
they need to install it, which is what the advisory on this repository's
Security tab is for.

## What helps

* **the version.** `igni --version` prints it; the desktop app shows it in
  its About box; the IDE plugins carry the same number, because every
  client's version is copied from `pyproject.toml` at release and the
  release refuses to start if they disagree;
* **which client.** The CLI, the desktop app, the VS Code extension, the
  JetBrains plugin and the web UI share one backend, but not one
  launcher, one sandbox or one way of reaching that backend, and a finding
  in one is often not in the others;
* **a reproduction, not a description**, if you have one: a prompt, a
  file, a repository, a page or an MCP server that makes it happen. Most of
  what has been found in igni was found by running something rather than
  reading it;
* **what you did not check.** "This tool ignores the protected paths list,
  I did not look at the others" is more useful than a report that implies
  completeness.

## What you can expect us to check

* whether it is already known — a guard that exists and failed is a
  regression, which is more serious than a new finding;
* whether a rule was supposed to prevent it, and why the test for that
  rule did not catch it;
* whether the fix belongs in igni or in the configuration it was given.
  Some settings are deliberately the user's — a permission set to `allow`,
  a model endpoint, an MCP server they added — and saying so is not a
  deflection when the documentation says the same.

## Scope

**In scope:** everything this repository publishes —

* the `ignite-ember` package on PyPI (the `igni` CLI and backend), and its
  Homebrew formula;
* the desktop app (macOS, Windows, Linux installers on the GitHub
  release) and its updater;
* the VS Code extension and the JetBrains plugin;
* the shared web UI in `clients/web`;
* the SDKs: `igni-service` and `igni-extension` on PyPI,
  `@ignite-ember/extension-bridge`, `@ignite-ember/ui-react` and
  `@ignite-ember/ui-css` on npmjs;
* the release pipeline itself: a way to get a build, a signature or an
  attestation published that this repository's workflows did not produce.

**Prompt injection is in scope when it gets past a guard** — content in a
file, a web page, a tool result or an MCP response that makes igni run a
command, read or write a protected path, or send data somewhere, *without*
the permission prompt or the block that
[docs/SECURITY.md](docs/SECURITY.md) says applies. That a model can be
talked into proposing something is not a vulnerability; that igni carries it
out without asking is.

**igni server, and the paid modules** (CodeIndex, Artifacts, Fleet) have
their own repositories and the same policy; a report about them can come to
the same address.

**Out of scope**, and stated so a report is not wasted:

* **a vendor-operated service, because there is not one.** igni sends
  nothing to Ignite Ember. Its traffic goes to the model endpoints, MCP
  servers and igni server its user configures, and to GitHub and PyPI for
  installs and the macOS updater's feed;
* **the model providers and MCP servers a user configures.** A flaw in
  somebody else's API is theirs to fix; how igni handles what comes back
  from it is ours;
* **a machine you do not own or are not authorised to test;**
* **anything that needs an attacker who already runs code as the user.**
  igni runs with its user's privileges and does not defend against that
  user's own account — the backend's per-launch token protects the local
  WebSocket from web pages and other users, not from the same user;
* **an unfixed upstream CVE in a dependency.** Reported by our own
  scheduled scan already, listed in the release's SBOMs, and not
  remediable here until upstream ships — see the targets' note on unfixed
  findings.

## Verifying what you installed

Every release carries CycloneDX SBOMs, a SHA256SUMS file, GitHub build
provenance for every file in it, and — once the release key exists — a
cosign signature over the checksums.
[docs/VERIFYING_RELEASES.md](docs/VERIFYING_RELEASES.md) shows how to check
them, including on a machine with no network.
