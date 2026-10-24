# Publishing the SDKs

igni's SDKs are what services and extensions build on. Five packages, released
together to igni's private package index:

| Package | Ecosystem | Source | For |
|---|---|---|---|
| `igni-service` | Python | `packages/igni-service` | a service: its key, registration, people's tokens, the directory, notifications, erasure and export, its extension bundle |
| `igni-extension` | Python | `packages/igni-extension` | an extension's process: tools over MCP, its service as the person, the host |
| `@igni/extension-bridge` | npm | `packages/extension-bridge` | an extension's page: `connect()`, RPC, navigation, theme |
| `@igni/ui-react` | npm | `packages/ui-react` | the React components under the compatibility floor |
| `@igni/ui-css` | npm | `clients/ui` | the stylesheet `@igni/ui-react` is drawn with |

`@igni/ui-css` is the fifth because `@igni/ui-react`'s README tells every
extension to install it beside it, and a component package whose stylesheet
cannot be installed is not installable either.

## Where they go

Two Artifact Registry repositories per environment, made by igni_iac's
`modules/artifact-registry` (its `docs/sdk-package-repositories.md` has the
infrastructure and the one-time setup):

| | Python | npm |
|---|---|---|
| **prod — the index** | `https://us-central1-python.pkg.dev/ember-prod-491411/ember-prod-python/simple/` | `https://us-central1-npm.pkg.dev/ember-prod-491411/ember-prod-npm/` |
| dev — rehearsals | `…/ember-dev-491411/ember-dev-python/simple/` | `…/ember-dev-491411/ember-dev-npm/` |

Consumers install from prod. Nothing is published to PyPI or npmjs.

## Versions

**One version for all five (lockstep), semver.** §8 of
[EXTENSIBILITY.md](EXTENSIBILITY.md) versions the floor independently of the
product, as semver, declared as a range — and the five packages *are* the
floor's code: the bridge's protocol is the host's, `@igni/ui-react` is drawn
with `@igni/ui-css`, `igni-service` builds the bundles `igni-extension` runs
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
  @igni/ui-react` never picks one; uv and pip skip pre-releases unless asked.

The versions live in the five manifests (`pyproject.toml`, `package.json`) and
nowhere else. `make sdk-version VERSION=0.4.0` sets all five (and the Python
packages' `uv.lock`); `make sdk-version-check` lists them and fails when they
disagree, or disagree with `VERSION=` when it is given.

**The tag is `sdk-v<version>`**: `sdk-v0.4.0`, `sdk-v0.4.0-rc.1`. One tag,
because there is one version; `sdk-` because `v*` tags already release igni
itself (`release.yml`), and the two must never trigger each other.

## TypeScript ships as source

`@igni/extension-bridge` and `@igni/ui-react` are published as their
TypeScript source — `main`, `types` and `exports` name `.ts` files — not as
compiled JavaScript. It is the decision `@igni/ui-react`'s README already
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
  plugin. Next.js needs `transpilePackages: ["@igni/ui-react",
  "@igni/extension-bridge"]`; Jest a `transformIgnorePatterns` exception.
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
dist/sdk/npm/igni-extension-bridge-<v>.tgz  igni-ui-react-<v>.tgz  igni-ui-css-<v>.tgz
```

Python with `uv build --no-sources` (proving the packages build with no
checkout around them), npm with `npm pack` (each package's `files` list
leaves its tests out). Install a built one somewhere to try it:
`uv pip install dist/sdk/python/igni_service-*.whl`, `npm install
dist/sdk/npm/igni-ui-react-*.tgz`.

## Releasing

`.github/workflows/sdk-release.yml` does the publishing; a person only
versions and tags.

1. On a branch: `make sdk-version VERSION=0.4.0`, commit, open a PR, merge.
2. Optionally rehearse on dev: Actions → **SDK release** → Run workflow on
   `main`, environment `dev`. It publishes the manifests' version to
   `ember-dev-*`. Re-rehearsing changed code under the same version uploads
   nothing (the files are already there) — use a pre-release version for a
   second rehearsal.
3. Tag the merge commit and push the tag:

   ```bash
   git tag sdk-v0.4.0 && git push origin sdk-v0.4.0
   ```

The workflow then:

| Job | |
|---|---|
| Authorize | stops unless the actor is a repository admin (as `release.yml` does) |
| Resolve | prod for a tag (dev or prod for a manual run, prod only on an `sdk-v*` tag); checks the tag names the manifests' version; picks the npm dist-tag |
| Verify Python / Verify npm | `make packages-check`; `clients/web`'s typecheck and vitest, which cover `packages/` — the same checks ci.yml's `packages` and `web` jobs run, on the released commit |
| Build | `make sdk-build`, kept as a workflow artifact for 30 days |
| Publish | in the GitHub environment `sdk-<env>`: signs in through Workload Identity Federation as `ember-<env>-sdk-publish`, then `make sdk-publish` uploads the Python files (`uv publish`) and the npm tarballs (`npm publish`) |

**Re-running is safe.** `uv publish --check-url` skips files the index already
has; an npm version already in the registry is skipped. A release that failed
half-way is finished by re-running the workflow. A published version is never
replaced — fix forward with the next patch.

`make sdk-publish` can be run by hand by someone with write access
(`SDK_INDEX_TOKEN=$(gcloud auth print-access-token) make sdk-publish
SDK_PYTHON_URL=… SDK_NPM_URL=…`), but the workflow is the way: it is what
checks the versions and runs the tests first.

## Installing them

Everything below is what igni__artifacts and igni__extension-starter do.

**Python (uv).** Name the index, make it explicit, pin the SDKs to it:

```toml
[[tool.uv.index]]
name = "igni"
url = "https://us-central1-python.pkg.dev/ember-prod-491411/ember-prod-python/simple/"
explicit = true

[tool.uv.sources]
igni-service = { index = "igni" }
igni-extension = { index = "igni" }
```

`explicit` means uv asks this index for nothing but what `[tool.uv.sources]`
pins to it, and the SDKs are pinned to nothing else: a package called
`igni-service` on PyPI is never a candidate.

**npm.** Map the scope, beside `package.json`:

```ini
@igni:registry=https://us-central1-npm.pkg.dev/ember-prod-491411/ember-prod-npm/
//us-central1-npm.pkg.dev/ember-prod-491411/ember-prod-npm/:_authToken=${IGNI_INDEX_TOKEN}
```

Every `@igni/*` package then comes from igni's registry, and nothing else does.

**Credentials.** Artifact Registry takes a Google OAuth access token, with
the username `oauth2accesstoken`. On a laptop, signed in with `gcloud` as a
member of the developers' group:

```bash
export IGNI_INDEX_TOKEN="$(gcloud auth print-access-token)"   # an hour
export UV_INDEX_IGNI_USERNAME=oauth2accesstoken
export UV_INDEX_IGNI_PASSWORD="$IGNI_INDEX_TOKEN"
```

(The consumers' Makefiles set the two `UV_INDEX_IGNI_*` from
`IGNI_INDEX_TOKEN`.) Longer-lived alternatives, which refresh the token
themselves: for uv, `uv tool install keyring --with
keyrings.google-artifactregistry-auth` and `UV_KEYRING_PROVIDER=subprocess`
(keep `UV_INDEX_IGNI_USERNAME`); for npm, `npx google-artifactregistry-auth`,
which writes the token into your user `~/.npmrc`.

**CI** signs in through Workload Identity Federation as the read-only
`ember-prod-sdk-read` account. Both consumers carry the same composite action,
`.github/actions/igni-index`, which exports the token to uv, npm and docker
builds; it needs `id-token: write` and the repository variables
`IGNI_INDEX_WIF_PROVIDER` and `IGNI_INDEX_SERVICE_ACCOUNT`.

**Docker builds** take the token as a netrc, as a build secret
(`--secret id=igni_index_netrc,src=.igni-index.netrc`, mounted at
`/root/.netrc` for the `RUN`s that install), so it is in no layer and no
history. The image installs from `uv export --frozen` with hashes,
`igni-service` included — so pip, which has no per-package index, can still
only install the lock's files.

### Working against an igni checkout

Both consumers keep a local override, `IGNI_SDK=local`, for changing an SDK
and its consumer together:

```bash
make test IGNI_SDK=local                       # igni__artifacts (IGNI=<checkout>, default the sibling)
make install IGNI_SDK=local IGNI_REPO=<path>   # igni__extension-starter, then IGNI_SDK=local on every make
```

It installs everything else from the committed lockfiles (`uv sync --frozen
--no-install-package igni-…`), puts the checkout's SDKs over them —
editable for Python; copies for npm (`npm install --no-save
--install-links`) — and sets `UV_NO_SYNC` so `uv run` does not undo it. No
manifest or lockfile changes, and the index is never contacted, so it works
before a first release, offline, and without index access. Extension bundles
built this way take igni-extension from wheels of the checkout. Images always
install from an index.

### Air-gapped and mirrored builds

Nothing here is needed to *run* igni's services or extensions: an image
carries its installed SDK and a bundle its wheelhouse. To *build* without
reaching Google, mirror the repositories — or only the versions the lockfiles
pin — into an index the build can reach (Artifact Registry in another
project, Nexus, devpi, Verdaccio), and point the build at it:

```bash
docker build --build-arg PIP_INDEX_URL=https://mirror.example/pypi/simple/ \
             --build-arg IGNI_INDEX_URL=https://mirror.example/igni/simple/ …
```

The images install from `uv export` requirements, which carry hashes but
no URLs, so the mirror only has to serve the same files. `uv sync` is
different: `uv.lock` records the URL each SDK came from, so a uv-driven
build against a mirror re-resolves first —
`UV_INDEX="igni=https://mirror.example/igni/simple/" uv lock` overrides the
named index without touching `pyproject.toml`, and the lock then records the
mirror. For npm, `npm config set @igni:registry <mirror>` in the user
config; `package-lock.json`'s integrity hashes still hold.

## The first release

In order — the consumers cannot resolve against an index that is empty.

1. **Infrastructure** (igni_iac): `terraform apply` for prod (and dev for a
   rehearsal), then its `docs/sdk-package-repositories.md` steps: the WIF
   provider's attribute condition, the `sdk-<env>` GitHub environments here
   with `WIF_PROVIDER`/`WIF_SERVICE_ACCOUNT`, the consumers' repository
   variables, the developers' group in `sdk_reader_members`.
2. **Release** `0.1.0`, the version all five carry today: optionally rehearse
   on dev (above), then `git tag sdk-v0.1.0 && git push origin sdk-v0.1.0`.
3. **Consumers' lockfiles**, which still describe the SDKs as a checkout:

   ```bash
   export IGNI_INDEX_TOKEN="$(gcloud auth print-access-token)"
   # igni__artifacts
   make lock && make test && git add uv.lock && git commit
   # igni__extension-starter
   make lock && make install && make test && git add uv.lock ui/package-lock.json && git commit
   ```

   Until then their `IGNI_SDK=local` targets are what run, and their CI fails
   at install — it reads the index, and the index is empty.

## Not done

- **The names on the public registries.** Nothing depends on them any
  more. The consumers use an explicit index, the `@igni` scope mapping and
  hashes. The extension-bundle builder resolves with hashes and downloads
  with `--require-hashes`, so a same-named public package cannot match.
  Registering `igni-service`, `igni-extension` and the `@igni` npm
  organisation as empty placeholders is still worth doing, so nobody else
  can publish under those names.
- **A changelog.** The release workflow writes a run summary, not release
  notes.
