#!/usr/bin/env node
// Single source-of-truth version syncer.
//
// ``igni-core.version`` is the version of igni's backend this app installs
// on first launch (``igni-core==<it>``), and the app's own version:
// the two are released in lockstep (release.yml checks PyPI has it). This
// script copies it into the files that carry their own copy: the Tauri
// shell's ``tauri.conf.json`` and ``Cargo.toml``, whose version is the
// ``CARGO_PKG_VERSION`` the shell pins the backend to.
//
// Run from anywhere — the script resolves paths relative to itself.
//
//   node scripts/sync-version.mjs
//
// Idempotent: writes only if the generated file is missing or out
// of date, so re-running in CI between npm-install and tsc doesn't
// touch mtime needlessly.

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const igniteEmberVersion = readFileSync(join(repoRoot, "igni-core.version"), "utf8").trim();
if (!/^\d+\.\d+\.\d+([-.]?[0-9A-Za-z.]+)?$/.test(igniteEmberVersion)) {
  console.error(`igni-core.version is not a version: ${JSON.stringify(igniteEmberVersion)}`);
  process.exit(1);
}

// Two kinds of targets:
//
//   • ``write`` — full file generated from a template (none today).
//   • ``patch`` — surgical regex replacement against an existing
//     file, so the surrounding content (and formatting) stays
//     untouched. The pattern's capture groups frame the literal
//     that gets swapped.
const targets = [
  {
    kind: "patch",
    label: "Tauri config (tauri.conf.json)",
    path: join(repoRoot, "clients/tauri/src-tauri/tauri.conf.json"),
    // Anchor on the top-level ``"version"`` key. The schema places
    // it directly under the root object, so the first match wins.
    pattern: /("version"\s*:\s*")[^"]+(")/,
  },
  {
    kind: "patch",
    label: "Tauri Cargo crate (Cargo.toml)",
    path: join(repoRoot, "clients/tauri/src-tauri/Cargo.toml"),
    // The Cargo package version doubles as ``CARGO_PKG_VERSION``
    // in ``runtime.rs`` — the value pinned in
    // ``uv pip install igni-core==<X>``. Drift here means the
    // Tauri shell installs a wrong ignite version on first launch.
    pattern: /(^version\s*=\s*")[^"]+(")/m,
  },
];

let changed = 0;
for (const t of targets) {
  if (t.kind === "write") {
    const current = existsSync(t.path) ? readFileSync(t.path, "utf8") : null;
    if (current === t.contents) continue;
    writeFileSync(t.path, t.contents);
    console.log(`updated ${t.label}: ${t.path}`);
    changed++;
    continue;
  }
  // ``patch``
  const before = existsSync(t.path) ? readFileSync(t.path, "utf8") : null;
  if (before === null) {
    console.error(`missing target file: ${t.path}`);
    process.exit(1);
  }
  if (!t.pattern.test(before)) {
    console.error(`version pattern not found in ${t.path}`);
    process.exit(1);
  }
  const after = before.replace(t.pattern, `$1${igniteEmberVersion}$2`);
  if (after === before) continue;
  writeFileSync(t.path, after);
  console.log(`updated ${t.label}: ${t.path}`);
  changed++;
}
if (changed === 0) console.log(`version ${igniteEmberVersion} already in sync`);
