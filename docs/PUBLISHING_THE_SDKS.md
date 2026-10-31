# Publishing the SDKs

igni's SDKs are what services and extensions build on. Five packages,
released together to the public registries — PyPI and npmjs — from this
repository, which is public under Apache-2.0:

| Package | Registry | Source | For |
|---|---|---|---|
| `igni-service` | PyPI | `packages/igni-service` | a service: its key, registration, people's tokens, the directory, notifications, erasure and export, its extension bundle |
| `igni-extension` | PyPI | `packages/igni-extension` | an extension's process: tools over MCP, its service as the person, the host |
| `@ignite-ember/extension-bridge` | npmjs | `packages/extension-bridge` | an extension's page: `connect()`, RPC, navigation, theme |
| `@ignite-ember/ui-react` | npmjs | `packages/ui-react` | the React components under the compatibility floor |
| `@ignite-ember/ui-css` | npmjs | `clients/ui` | the stylesheet `@ignite-ember/ui-react` is drawn with |

`@ignite-ember/ui-css` is the fifth because `@ignite-ember/ui-react`'s README
tells every extension to install it beside it, and a component package whose
stylesheet cannot be installed is not installable either.

The npm scope is `@ignite-ember`, the organisation's name on npmjs; `@igni`
there is not ours. The Python names are the import names' (`igni_service`,
`igni_extension`).

Every release is also a GitHub Release of this repository, `sdk-v<version>`,
carrying the same wheels, sdists and npm tarballs and their `SHA256SUMS`: what
the offline bundle and an air-gapped build take instead of a registry.

## Versions

**One version for all five (lockstep), semver.** §8 of
[EXTENSIBILITY.md](EXTENSIBILITY.md) versions the floor independently of the
product, as semver, declared as a range — and the five packages *are* the
floor's code: the bridge's protocol is the host's, `@ignite-ember/ui-react` is drawn
with `@ignite-ember/ui-css`, `igni-service` builds the bundles `igni-extension` runs
in. Independent versions would ask every extension author to find a
combination that works together; one version is that combination, and "built
on SDK 0.3" means one thing. The cost is that a release bumps a package that
did not change — cheap, since a version is a few kilobytes and nobody has to
read a changelog for it.

- **Patch** (`0.3.1`): fixes; nothing an extension relies on changes.
- **Minor** (`0.4.0`): additions. While the major version is 0 a minor
  release may also break — semver's rule for 0.x — and says so in its notes.
- **From 1.0**, §8's rules: breaking only in a major, after the deprecation
  window.
- **Pre-releases**: `0.4.0-rc.1` (also `-alpha.N`, `-beta.N`). npm reads it as
  written; Python normalises it to `0.4.0rc1`, which is the same version.
  npm publishes a pre-release under the dist-tag `next`, so `npm install
  @ignite-ember/ui-react` never picks one; uv and pip skip pre-releases unless asked.

The versions live in the five manifests (`pyproject.toml`, `package.json`) and
nowhere else. `make sdk-version VERSION=0.4.0` sets all five (and the Python
packages' `uv.lock`); `make sdk-version-check` lists them and fails when they
disagree, or disagree with `VERSION=` when it is given.

**The tag is `sdk-v<version>`**: `sdk-v0.4.0`, `sdk-v0.4.0-rc.1`. One tag,
because there is one version; `sdk-` because `v*` tags already release igni
itself (`release.yml`), and the two must never trigger each other.

## TypeScript ships as source

`@ignite-ember/extension-bridge` and `@ignite-ember/ui-react` are published as their
TypeScript source — `main`, `types` and `exports` name `.ts` files — not as
compiled JavaScript. It is the decision `@ignite-ember/ui-react`'s README already
records ("Source, not a build"): an extension bundles its own React and runs
its own build, so a compiled copy would pin a JSX runtime and a target the
extension may not share.

It was checked against the consumer that exists, the extension starter, with
the packages installed into `node_modules` the way a registry install puts
them there and every alias and `paths` entry removed: `tsc --noEmit`, `vite
build` and `vitest run` all pass unchanged. Vite compiles TypeScript in
`node_modules` like the page's own, and the packages' peers (`react`,
`react-markdown`, …) resolve from the consumer's `node_modules` without the
alias table the source checkout needed.

What it asks of a consumer:

- A bundler that compiles TypeScript — Vite, esbuild, Rollup with a TS
  plugin. Next.js needs `transpilePackages: ["@ignite-ember/ui-react",
  "@ignite-ember/extension-bridge"]`; Jest a `transformIgnorePatterns` exception.
- `tsc` type-checks the packages' source under the consumer's own
  `compilerOptions` (`skipLibCheck` covers `.d.ts` only). The source is
  written to `strict`; a consumer with stricter flags than that
  (`exactOptionalPropertyTypes`, say) may see errors in it.

If a second kind of consumer turns up that cannot compile TypeScript, the
answer is to add a compiled `dist/` beside `src/` and point `exports` at it —
not to stop shipping the source.

## Building locally

```bash
make sdk-build
```

Checks the versions agree, then writes to `dist/sdk/`:

```
dist/sdk/python/igni_service-<v>-py3-none-any.whl  igni_service-<v>.tar.gz
dist/sdk/python/igni_extension-<v>-py3-none-any.whl  igni_extension-<v>.tar.gz
dist/sdk/npm/ignite-ember-extension-bridge-<v>.tgz  ignite-ember-ui-react-<v>.tgz  ignite-ember-ui-css-<v>.tgz
```

Python with `uv build --no-sources` (proving the packages build with no
checkout around them), npm with `npm pack` (each package's `files` list
leaves its tests out). Every package carries `LICENSE` and `NOTICE`. To check
what the registries would see:

```bash
uvx twine check --strict dist/sdk/python/*                 # PyPI's metadata and README checks
for t in dist/sdk/npm/*.tgz; do tar -tzf "$t"; done          # what each tarball carries
for t in dist/sdk/npm/*.tgz; do npm publish "$t" --dry-run --access public; done
```

Install a built one somewhere to try it: `uv pip install
dist/sdk/python/igni_service-*.whl`, `npm install
dist/sdk/npm/ignite-ember-ui-react-*.tgz`.

## The workflow

`.github/workflows/sdk-release.yml` does the publishing; a person only
versions, rehearses and tags. No publishing credential is stored anywhere:
PyPI, TestPyPI and npmjs trust the workflow itself, through OIDC (*trusted
publishing*), and only from the GitHub environment named for each.

| Job | Runs on | |
|---|---|---|
| Authorize | both | stops unless the actor is a repository admin (as `release.yml` does) |
| Resolve | both | a tag is a **release**, a manual run a **rehearsal**; checks a tag names the manifests' version (`make sdk-version-check VERSION=…`) and the five agree; picks the npm dist-tag (`next` for a pre-release, `latest` otherwise) |
| Verify Python / Verify npm | both | `make packages-check`; `clients/web`'s typecheck and vitest, which cover `packages/` — what ci.yml's `packages` and `web` jobs run, on the released commit |
| Build | both | `make sdk-build`, `twine check --strict`, `SHA256SUMS`, and each tarball's file list in the run summary; kept as a workflow artifact for 30 days |
| Rehearse on TestPyPI | rehearsal | environment `testpypi`: uploads the wheels and sdists to test.pypi.org (`pypa/gh-action-pypi-publish`, trusted publishing) |
| Rehearse npm (dry run) | rehearsal | `npm publish --dry-run --provenance --access public` for each tarball, under the dist-tag a release would use, and the tarballs as an artifact (`npm-tarballs-<v>`) |
| Publish to PyPI | release | environment `pypi`: uploads to pypi.org (trusted publishing; PEP 740 attestations) |
| Publish to npmjs | release | environment `npm`: `npm publish --provenance --access public --tag <dist-tag>` for each tarball (trusted publishing; `NPM_TOKEN` only until it is configured — below) |
| Attach to the GitHub Release | release | creates the `sdk-v<v>` release (a pre-release for `-rc.N` and the like; never marked *latest*, which the desktop app's updater reads) and uploads every file plus `SHA256SUMS` |

**Re-running is safe.** PyPI files already there are skipped
(`skip-existing`), an npm version already in the registry is skipped, and the
GitHub Release's files are replaced with the same bytes. A release that failed
half-way is finished by re-running the workflow. A published version is never
replaced — PyPI and npm refuse it — so fix forward with the next patch.

**Provenance.** npm records which workflow run built each version, and checks
the tarball's `repository.url` (`git+https://github.com/ignite-ember/igni.git`)
against the repository that ran it; the three `package.json`s carry it. PyPI
attaches the equivalent attestations on its own.

## One-time setup (founder)

Done once, by an owner of the `ignite-ember` GitHub organisation, before the
first rehearsal. Enable two-factor authentication on every account below;
PyPI and npm require it to publish.

### 1. PyPI and TestPyPI

TestPyPI is a separate service with separate accounts.

1. Create an account on [pypi.org](https://pypi.org/account/register/) and one
   on [test.pypi.org](https://test.pypi.org/account/register/), both with 2FA.
2. Optionally, request an organisation `ignite-ember` on PyPI (Your
   organisations → Create; a company organisation is reviewed by PyPI, which
   takes a while). Not needed to publish: the projects can be moved into it
   later.
3. On **pypi.org**, Your account → Publishing → *Add a new pending publisher*
   (GitHub), twice:

   | PyPI project name | Owner | Repository name | Workflow name | Environment name |
   |---|---|---|---|---|
   | `igni-service` | `ignite-ember` | `igni` | `sdk-release.yml` | `pypi` |
   | `igni-extension` | `ignite-ember` | `igni` | `sdk-release.yml` | `pypi` |

4. On **test.pypi.org**, the same two, with environment name `testpypi`.

A pending publisher becomes the project's trusted publisher on the first
upload, which also creates the project under your account. It does not
reserve the name until then, so rehearse soon after adding it.

### 2. npmjs

npm's trusted publishing is configured on a package's settings page, so a
package must exist before it can trust anything. The first publish therefore
uses a token, and the token is removed straight after.

1. Create an account on [npmjs.com](https://www.npmjs.com/signup), with 2FA.
2. Create the organisation **`ignite-ember`** (Add organization → the free
   plan, which is for public packages). The scope `@ignite-ember` is then
   yours.
3. For the first release, create a **granular access token** (Access Tokens
   → Generate New Token → Granular): permission *Read and write*, packages
   and scopes *`@ignite-ember`*, organisation *`ignite-ember`*, a short
   expiry (7 days), and *Bypass two-factor authentication* ticked so CI can
   use it. Store it as the secret **`NPM_TOKEN`** of the GitHub environment
   `npm` (below) — of the environment, not of the repository.
4. After the first release has published the three packages, on each
   package's page → Settings → *Trusted Publisher* → GitHub Actions:

   | Field | Value |
   |---|---|
   | Organization or user | `ignite-ember` |
   | Repository | `igni` |
   | Workflow filename | `sdk-release.yml` |
   | Environment name | `npm` |

   for `@ignite-ember/extension-bridge`, `@ignite-ember/ui-react` and
   `@ignite-ember/ui-css`. Then delete the `NPM_TOKEN` secret and revoke the
   token, and on each package → Settings → Publishing access choose *Require
   two-factor authentication and disallow tokens*. From then on the workflow
   publishes with its OIDC token alone, and nothing else can.

(The alternative to step 3 is to publish `0.1.0` of each package by hand,
from a laptop signed in with `npm login`, from the tarballs a rehearsal
uploads — then configure step 4 before the first tagged release. The
workflow skips a version that is already published.)

### 3. GitHub environments

In `ignite-ember/igni` → Settings → Environments, create three:

| Environment | Required reviewers | Deployment branches and tags | Secrets |
|---|---|---|---|
| `testpypi` | the admin | all (a rehearsal runs from any branch) | none |
| `pypi` | the admin | selected: tag pattern `sdk-v*` | none |
| `npm` | the admin | selected: tag pattern `sdk-v*` | `NPM_TOKEN`, until npm trusted publishing is configured |

The names must match the trusted publishers above exactly. A release's
`pypi` and `npm` jobs wait together, so one review approves both. Leave
*Prevent self-review* off while there is one admin.

## Rehearsing

From any branch whose manifests carry the version to rehearse:

1. Actions → **SDK release** → *Run workflow* on that branch.
2. Approve the `testpypi` deployment when the run asks.
3. Check the result:
   - <https://test.pypi.org/project/igni-service/> and
     `…/igni-extension/` show the version, with the README and project links
     rendered.
   - Install from TestPyPI, taking the dependencies from PyPI (TestPyPI does
     not have them):

     ```bash
     uv venv /tmp/sdk-try && uv pip install --python /tmp/sdk-try \
       --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ \
       --index-strategy unsafe-best-match igni-service==<v> igni-extension==<v>
     ```

   - The run's summary lists each npm tarball's files, and the
     `npm-tarballs-<v>` artifact holds them: `npm install ./ignite-ember-ui-react-<v>.tgz`
     in a scratch project.

TestPyPI, like PyPI, never takes the same file twice: a second rehearsal of
one version uploads nothing. Rehearse changed code under a new pre-release
(`make sdk-version VERSION=0.4.0-rc.2`).

## Releasing

1. On a branch: `make sdk-version VERSION=0.4.0`, commit, open a PR, merge.
2. Rehearse (above) on `main`.
3. Tag the merge commit and push the tag:

   ```bash
   git tag sdk-v0.4.0 && git push origin sdk-v0.4.0
   ```

4. Approve the `pypi` and `npm` deployments when the run asks.
5. Check <https://pypi.org/project/igni-service/>,
   <https://pypi.org/project/igni-extension/>,
   <https://www.npmjs.com/package/@ignite-ember/ui-react> (and the other two;
   the version page shows *Provenance*), and the `sdk-v0.4.0` GitHub Release.

A pre-release (`sdk-v0.4.0-rc.1`) goes the same way: npm's dist-tag `next`,
a GitHub pre-release, and PyPI, where installers skip it unless asked.

## The first release

In order — the consumers cannot resolve the SDKs before they are published.

1. **One-time setup**, above.
2. **Rehearse** `0.1.0`, the version all five carry today.
3. **Release** it: `git tag sdk-v0.1.0 && git push origin sdk-v0.1.0`.
4. **npm trusted publishing**: configure the three packages, delete
   `NPM_TOKEN` (one-time setup, step 2.4).
5. **Consumers' lockfiles**, which do not hold the SDKs from PyPI yet:

   ```bash
   # igni__code-index and igni__artifacts
   make lock && make test && git add uv.lock && git commit
   # optionally, their pages' npm locks
   make extension-ui-lock && git add extension/page/package-lock.json
   ```

   Until then their `IGNI_SDK=local` targets are what run, and their image
   builds stop at the first stage with a message saying exactly this.

## Installing them

**Python.** From PyPI, like anything else:

```bash
uv add igni-service        # a service
uv add igni-extension      # an extension's process
```

**npm.** From npmjs:

```bash
npm install @ignite-ember/extension-bridge @ignite-ember/ui-react @ignite-ember/ui-css
```

Nothing to configure, no token. A consumer's lockfile (`uv.lock`,
`package-lock.json`) records each file's hash, so a same-named package from
anywhere else cannot match it.

### Working against an igni checkout

The consumers keep a local override, `IGNI_SDK=local`, for changing an SDK
and its consumer together:

```bash
make test IGNI_SDK=local                       # igni__artifacts, igni__code-index (IGNI=<checkout>, default the sibling)
make install IGNI_SDK=local IGNI_REPO=<path>   # igni__extension-starter, then IGNI_SDK=local on every make
```

It installs everything else from the committed lockfiles (`uv sync --frozen
--no-install-package igni-…`), puts the checkout's SDKs over them —
editable for Python; copies for npm (`npm install --no-save
--install-links`) — and sets `UV_NO_SYNC` so `uv run` does not undo it. No
manifest or lockfile changes, and no registry is contacted for the SDKs, so it
works before a release and offline. Extension bundles built this way take
igni-extension from wheels of the checkout. Images always install from a
registry.

### Air-gapped and mirrored builds

Nothing here is needed to *run* igni's services or extensions: an image
carries its installed SDK and a bundle its wheelhouse. To *build* without
reaching PyPI or npmjs, use a mirror the build can reach (devpi, Nexus,
Artifactory, Verdaccio) holding the versions the lockfiles pin — the SDKs'
files are on PyPI and npmjs like the rest, and also attached to each
`sdk-v<version>` GitHub Release, checksummed, for a mirror that is filled by
hand:

```bash
gh release download sdk-v0.4.0 --repo ignite-ember/igni --dir sdk-0.4.0
(cd sdk-0.4.0 && sha256sum -c SHA256SUMS)
```

Point the image at the mirror:

```bash
docker build --build-arg PIP_INDEX_URL=https://mirror.example/pypi/simple/ …
```

The images install from `uv export` requirements, which carry hashes but no
URLs, so the mirror only has to serve the same files. For npm, `npm config
set registry <mirror>`; `package-lock.json`'s integrity hashes still hold.

## Not done

- **A changelog.** The release writes a run summary and a one-paragraph
  GitHub Release, not release notes.
- **A PyPI organisation.** The projects belong to the account that first
  publishes them until they are moved into one.
