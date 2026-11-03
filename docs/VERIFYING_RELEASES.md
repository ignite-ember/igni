# Verifying a release

Every igni release on GitHub carries the evidence to check it: what is in it,
that its files are the ones this repository's release workflow built, and —
once the release key exists — a signature from Ignite Ember. This page shows
how to check each, on a connected machine and on one with no network at all.

Two kinds of release come out of this repository:

| Tag | Workflow | What it ships |
|---|---|---|
| `v<version>` | `.github/workflows/release.yml` | the `ignite-ember` wheel and sdist, the desktop installers and macOS updater payload, the VS Code extension (`.vsix`), the JetBrains plugin (`.zip`) |
| `sdk-v<version>` | `.github/workflows/sdk-release.yml` | `igni-service` and `igni-extension` wheels and sdists, the three `@ignite-ember/*` npm tarballs |

## What is on a release

| File | What it is |
|---|---|
| `SHA256SUMS` | The SHA-256 of every other file on the release, SBOMs included. |
| `SHA256SUMS.sig` | A cosign signature over `SHA256SUMS`. **Absent until the key ceremony has happened** — see the last section. |
| `*.provenance.sigstore.json` | The build-provenance attestation for every file in `SHA256SUMS`, as a Sigstore bundle, for offline verification. The same attestation is stored on GitHub. |
| `*.cdx.json` | CycloneDX SBOMs. |

SBOMs, per thing you install:

| SBOM | Covers | Built from |
|---|---|---|
| `ignite-ember-<v>.cdx.json` | the wheel / sdist, every optional extra | `uv.lock` |
| `igni-desktop-<v>.cdx.json` | the desktop app: the Tauri shell's crates and the web UI's npm packages | `clients/tauri/src-tauri/Cargo.lock`, `clients/web/package-lock.json` |
| `igni-vscode-<v>.cdx.json` | the VS Code extension: the web UI it bundles (the extension has no runtime dependencies of its own) | `clients/web/package-lock.json`, `clients/vscode/package-lock.json` |
| `igni-jetbrains-<v>.cdx.json` | the JetBrains plugin: the web UI it bundles (the IntelliJ Platform provides the plugin's runtime) | `clients/web/package-lock.json` |
| `igni-service-<v>.cdx.json`, `igni-extension-<v>.cdx.json` | each Python SDK | its own `uv.lock` |

The npm SDK packages declare no dependencies (`@ignite-ember/ui-react`'s are
peer dependencies your application chooses), so they have no SBOM; npm's
provenance covers them (below). Development dependencies are in no SBOM: they
are not shipped. The desktop app downloads `uv`, Python and `ignite-ember` on
first launch; those are covered by the uv release's own checksums and by
`ignite-ember-<v>.cdx.json`, not by the desktop SBOM.

To scan one: `trivy sbom igni-desktop-1.0.4.cdx.json`, or
`grype sbom:igni-desktop-1.0.4.cdx.json`.

## 1. Checksums

```bash
sha256sum --check --ignore-missing SHA256SUMS
```

Every file you downloaded must say `OK`. This is only as good as the
`SHA256SUMS` you checked against, which is what the next two steps are for.

## 2. Build provenance (online)

Each file has a SLSA build-provenance attestation, made by GitHub Actions in
the run that built it and signed with a short-lived Sigstore certificate that
names this repository, the workflow and the commit. It needs the GitHub CLI
(`gh` 2.49 or later):

```bash
gh attestation verify igni_1.0.4_aarch64.dmg --repo ignite-ember/igni
```

To also insist on the workflow and the tag that built it:

```bash
gh attestation verify igni_1.0.4_aarch64.dmg --repo ignite-ember/igni \
  --signer-workflow ignite-ember/igni/.github/workflows/release.yml \
  --source-ref refs/tags/v1.0.4
```

(For SDK files, `sdk-release.yml` and `refs/tags/sdk-v<version>`.)

Every file at once:

```bash
while read -r _ file; do
  [ -f "$file" ] && gh attestation verify "$file" --repo ignite-ember/igni >/dev/null && echo "OK  $file"
done < SHA256SUMS
```

A pass means the file is byte-for-byte what that workflow produced from that
commit. It does not mean the commit is free of bugs; that is what the SBOMs
and the advisories are for.

## 3. Build provenance on an air-gapped machine

Verification needs no network if you bring two things with you: the
attestation bundle (attached to the release) and Sigstore's trusted root (the
public keys and certificate authorities the bundle chains to).

**On a connected machine**, once per release:

```bash
gh release download v1.0.4 --repo ignite-ember/igni --dir igni-1.0.4
gh attestation trusted-root > igni-1.0.4/trusted_root.jsonl
```

`trusted_root.jsonl` is not a secret and changes rarely — only when Sigstore
rotates a key — but fetch it at or after the release date, so it includes
the keys the release was signed under. If you prefer not to trust the bundle
attached to the release, `gh attestation download <file> --repo
ignite-ember/igni` fetches the same attestation from GitHub instead.

**Carry the directory in. On the air-gapped machine** (a `gh` binary of 2.49
or later; with `--bundle` and `--custom-trusted-root` it contacts nothing):

```bash
cd igni-1.0.4
sha256sum --check --ignore-missing SHA256SUMS
gh attestation verify igni_1.0.4_aarch64.dmg \
  --repo ignite-ember/igni \
  --bundle igni-1.0.4.provenance.sigstore.json \
  --custom-trusted-root trusted_root.jsonl
```

The bundle carries the signing certificate and the transparency-log
inclusion proof, so nothing has to be looked up. The flags in step 2
(`--signer-workflow`, `--source-ref`) work here too.

## 4. The cosign signature

**Not yet.** Releases are signed with a cosign key pair the same way igni
server signs its images: the private key held by Ignite Ember, the public key
(`igni-cosign.pub`) given to you once, through a channel other than the
release — with the contract, or compared by fingerprint. Never trust a key that
arrives with the files it is meant to check. The signature is not recorded in
a public transparency log, so verifying it needs only the public key:

```bash
cosign verify-blob --key igni-cosign.pub --insecure-ignore-tlog=true \
  --signature SHA256SUMS.sig SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
```

`--insecure-ignore-tlog=true` is what a key-pair signature without a log entry
needs; it does not weaken the signature check. A pass means Ignite Ember's
release key signed this exact list of digests, and therefore every file whose
digest is on it.

Until the key ceremony has happened, releases have no `SHA256SUMS.sig`, and
the release workflow says so in its log. Provenance (steps 2 and 3) is
available from the first release built by these workflows regardless.

## 5. The package registries

The same bytes on PyPI and npmjs carry the registries' own evidence:

* **PyPI** — published with trusted publishing, which uploads PEP 740
  attestations; pypi.org shows them on each file's page. To check one:
  `pip install pypi-attestations` then
  `pypi-attestations verify pypi --repository https://github.com/ignite-ember/igni pypi:<filename>`.
* **npmjs** — published with `--provenance`; `npm audit signatures` in a
  project that installs them verifies the registry signatures and
  provenance.
* **IDE marketplaces** — the VS Code Marketplace and JetBrains Marketplace
  copies are uploaded from the same `.vsix` and `.zip`, so their digests match
  `SHA256SUMS`.

## What else signs a release

These are separate from the evidence above and predate it:

* **macOS** — the `.app` is signed with Ignite Ember's Apple Developer ID and
  notarised, and the ticket is stapled, so Gatekeeper checks it offline.
* **The macOS updater** — `latest.json` points at `igni_aarch64.app.tar.gz`
  with a minisign signature the installed app checks against the public key
  it ships with.
* **Windows and Linux installers** carry no platform code signature;
  provenance and, later, the cosign signature are what vouch for them.

## For maintainers: turning signing on

`release.yml` (job `release-evidence`) and `sdk-release.yml` (job
`github-release`) sign when all three of these exist on the repository, skip
with a notice when none do, and fail when only some do:

| Name | Kind | Content |
|---|---|---|
| `COSIGN_PRIVATE_KEY` | secret | `cosign.key`, encrypted PEM |
| `COSIGN_PASSWORD` | secret | its password |
| `COSIGN_PUBLIC_KEY` | variable | `cosign.pub` |

The names are igni server's (its `docs/OFFLINE_UPDATES.md`). Whether igni's
releases use the same key pair as igni server's images is the key ceremony's
decision; one key means a customer holds one public key. If the key is moved
into a GitHub environment, add `environment:` to those two jobs and limit the
environment to `v*` and `sdk-v*` tags.
