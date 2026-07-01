/**
 * Tests for ``kindFor`` — the extension-to-icon-kind mapper inside
 * FileTypeIcon. Ten kinds, ~70 extensions in the table; the bulk
 * of risk is silent regressions when someone adds an extension
 * to the wrong group or drops one entirely.
 *
 * Pinning the obvious classes + the case-insensitivity contract;
 * we don't enumerate all 70 extensions (overspecifying just makes
 * the tests bigger without finding bugs the kind-level checks
 * don't already catch).
 */

import { describe, expect, it } from "vitest";
import { kindFor } from "./FileTypeIcon";

describe("kindFor", () => {
  it("returns 'file' for files with no extension", () => {
    // Bare names (README, Makefile, LICENSE) — the generic doc
    // glyph reads as "some file" without overcommitting.
    expect(kindFor("README")).toBe("file");
    expect(kindFor("Makefile")).toBe("file");
    expect(kindFor("LICENSE")).toBe("file");
  });

  it("returns 'file' for an unknown extension (forward-compat)", () => {
    // ``.foo`` isn't in the table. Don't crash, don't guess —
    // generic page icon is the right fallback.
    expect(kindFor("data.foo")).toBe("file");
  });

  it("is case-insensitive on the extension", () => {
    // macOS Finder often surfaces ``Photo.JPG``. We lowercase
    // before lookup.
    expect(kindFor("Photo.JPG")).toBe("image");
    expect(kindFor("module.TS")).toBe("code");
    expect(kindFor("data.JSON")).toBe("data");
  });

  it("uses the last dot for multi-dot filenames", () => {
    // ``foo.test.ts`` → ts (code), not test. Mirrors how
    // ``guessLang`` does it in ChatItems — same convention.
    expect(kindFor("component.test.tsx")).toBe("code");
    expect(kindFor("backup.tar.gz")).toBe("archive");
  });

  it("classifies common image extensions as 'image'", () => {
    // Sample a few — png/jpg/svg/webp covers the common cases.
    for (const ext of ["png", "jpg", "jpeg", "gif", "webp", "svg", "heic"]) {
      expect(kindFor(`x.${ext}`)).toBe("image");
    }
  });

  it("classifies code extensions as 'code' (not 'data')", () => {
    // The .js/.ts split between code and data is the most
    // common silent regression — both look like text files to
    // a naive grouping. Pin them as code.
    for (const ext of ["js", "ts", "py", "rs", "go", "cpp", "java"]) {
      expect(kindFor(`x.${ext}`)).toBe("code");
    }
  });

  it("classifies markup as 'code' (html/css/etc.)", () => {
    // HTML/CSS could plausibly land under data; we put them
    // under code so the icon picks the chevron glyph.
    expect(kindFor("page.html")).toBe("code");
    expect(kindFor("style.css")).toBe("code");
    expect(kindFor("style.scss")).toBe("code");
  });

  it("classifies structured-data extensions as 'data'", () => {
    // JSON/YAML/TOML/XML/SQL all under data — the rows-of-text
    // glyph reads as "tabular / structured".
    for (const ext of ["json", "yaml", "yml", "toml", "csv", "sql"]) {
      expect(kindFor(`x.${ext}`)).toBe("data");
    }
  });

  it("classifies plaintext docs as 'doc'", () => {
    // .md/.txt/.rst are the lightweight text docs — distinct
    // glyph from .json/.yaml since the user reads them, not
    // pipelines them.
    for (const ext of ["txt", "md", "rst", "log"]) {
      expect(kindFor(`x.${ext}`)).toBe("doc");
    }
  });

  it("classifies PDF as its own kind", () => {
    // PDF gets a dedicated badge glyph because the user
    // commonly drops them in.
    expect(kindFor("paper.pdf")).toBe("pdf");
  });

  it("classifies archives", () => {
    // The ``7z`` key is the one that's tricky to get into a
    // Record<string, Kind> literal; pin it to catch a future
    // refactor that drops it.
    for (const ext of ["zip", "tar", "gz", "tgz", "rar", "7z", "bz2", "xz"]) {
      expect(kindFor(`x.${ext}`)).toBe("archive");
    }
  });

  it("classifies video extensions", () => {
    for (const ext of ["mp4", "mov", "mkv", "webm"]) {
      expect(kindFor(`x.${ext}`)).toBe("video");
    }
  });

  it("classifies audio extensions", () => {
    for (const ext of ["mp3", "wav", "ogg", "flac", "m4a"]) {
      expect(kindFor(`x.${ext}`)).toBe("audio");
    }
  });

  it("classifies shell scripts (sh/bash/zsh/fish/ps1)", () => {
    // Distinct kind because shell scripts get a runs-things
    // glyph — they're not text docs.
    for (const ext of ["sh", "bash", "zsh", "fish", "ps1"]) {
      expect(kindFor(`x.${ext}`)).toBe("shell");
    }
  });
});
