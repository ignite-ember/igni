"""Audit a lockfile, report every advisory, and fail on the critical ones.

    uv run --no-project --with 'cvss>=3,<4' python scripts/dependency_audit.py npm clients/web
    uv run --no-project --with 'cvss>=3,<4' python scripts/dependency_audit.py cargo clients/tauri/src-tauri

Python is not here: `pip-audit` in ci.yml already fails on every advisory,
with no exceptions, and stays that way.

npm and cargo trees are bigger and older, and some of what they report has no
fix to take or does not reach anything igni runs. So this gate is narrower:

* every advisory is **printed**, whatever its severity;
* an advisory at or above ``--fail-on`` (default ``critical``) **fails**,
  unless ``.github/dependency-audit-allowlist.toml`` names it;
* an allowlist entry past its ``review_by`` date fails too, so an exception
  cannot outlive the reasoning written beside it; and an entry that no longer
  matches anything is reported, so it can be deleted.

npm's report carries a severity. RustSec's carries a CVSS vector, or nothing:
the vector is scored here, and an advisory without one is reported as
``unknown`` and does not gate — RustSec leaves most of its memory-safety and
"unsound" advisories unscored, and failing on all of them would fail on
advisories with no fixed release. docs/VULNERABILITY_MANAGEMENT.md has the
whole policy.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST = REPO / ".github" / "dependency-audit-allowlist.toml"
SEVERITIES = ("unknown", "low", "moderate", "high", "critical")


@dataclass(frozen=True)
class Finding:
    """One advisory against one package, as either tool reports it."""

    ecosystem: str
    package: str
    version: str
    advisory: str
    aliases: tuple[str, ...]
    severity: str
    title: str

    @property
    def ids(self) -> set[str]:
        return {self.advisory, *self.aliases}

    def rank(self) -> int:
        return SEVERITIES.index(self.severity)


@dataclass(frozen=True)
class AllowEntry:
    """An allowlist entry: one advisory, one package, a reason and a date."""

    ecosystem: str
    advisory: str
    package: str
    reason: str
    review_by: dt.date

    def covers(self, finding: Finding) -> bool:
        return (
            finding.ecosystem == self.ecosystem
            and finding.package == self.package
            and self.advisory in finding.ids
        )

    def expired(self, today: dt.date) -> bool:
        return today > self.review_by


@dataclass
class Allowlist:
    entries: list[AllowEntry] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Allowlist:
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text())
        entries = []
        for raw in data.get("allow", []):
            missing = {"ecosystem", "advisory", "package", "reason", "review_by"} - raw.keys()
            if missing:
                raise SystemExit(f"{path}: an entry is missing {sorted(missing)}: {raw}")
            if not str(raw["reason"]).strip():
                raise SystemExit(f"{path}: {raw['advisory']} has an empty reason")
            entries.append(
                AllowEntry(
                    ecosystem=raw["ecosystem"],
                    advisory=raw["advisory"],
                    package=raw["package"],
                    reason=" ".join(str(raw["reason"]).split()),
                    review_by=raw["review_by"],
                )
            )
        return cls(entries)

    def matching(self, finding: Finding) -> AllowEntry | None:
        return next((e for e in self.entries if e.covers(finding)), None)

    def for_ecosystem(self, ecosystem: str) -> list[AllowEntry]:
        return [e for e in self.entries if e.ecosystem == ecosystem]


class NpmAudit:
    """`npm audit --omit=dev`: what ships, not what builds it."""

    ecosystem = "npm"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def findings(self) -> list[Finding]:
        # npm exits non-zero whenever it finds anything; the JSON is the answer.
        run = subprocess.run(
            ["npm", "audit", "--omit=dev", "--json"],
            cwd=self.directory,
            capture_output=True,
            text=True,
            check=False,
        )
        report = json.loads(run.stdout or "{}")
        if "error" in report:
            raise SystemExit(f"npm audit failed in {self.directory}: {report['error']}")
        found = []
        for name, vuln in (report.get("vulnerabilities") or {}).items():
            for via in vuln.get("via", []):
                if not isinstance(via, dict):
                    continue  # a transitive pointer; the advisory is listed under its own package
                url = via.get("url", "")
                found.append(
                    Finding(
                        ecosystem=self.ecosystem,
                        package=name,
                        version=via.get("range", vuln.get("range", "?")),
                        advisory=url.rsplit("/", 1)[-1] or str(via.get("source")),
                        aliases=(url,) if url else (),
                        severity=via.get("severity", "unknown"),
                        title=via.get("title", ""),
                    )
                )
        return found


class CargoAudit:
    """`cargo audit` against the RustSec database; vulnerabilities only.

    RustSec's informational warnings (unmaintained, unsound, yanked) are
    counted and printed by `cargo audit` itself, never gated.
    """

    ecosystem = "cargo"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @staticmethod
    def severity(vector: str | None) -> str:
        if not vector:
            return "unknown"
        if vector.startswith("CVSS:4"):
            from cvss import CVSS4

            label = CVSS4(vector).severity
        else:
            from cvss import CVSS3

            label = CVSS3(vector).severities()[0]
        label = label.lower()
        return {"medium": "moderate", "none": "low"}.get(label, label)

    def findings(self) -> list[Finding]:
        run = subprocess.run(
            ["cargo", "audit", "--json"],
            cwd=self.directory,
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            report = json.loads(run.stdout)
        except json.JSONDecodeError:
            raise SystemExit(f"cargo audit failed in {self.directory}:\n{run.stderr}") from None
        warnings = report.get("warnings") or {}
        for kind, items in sorted(warnings.items()):
            print(f"report   {kind:<13} {len(items)} RustSec warning(s) — informational, not gated")
        found = []
        for vuln in report.get("vulnerabilities", {}).get("list", []):
            advisory = vuln["advisory"]
            found.append(
                Finding(
                    ecosystem=self.ecosystem,
                    package=vuln["package"]["name"],
                    version=vuln["package"]["version"],
                    advisory=advisory["id"],
                    aliases=tuple(advisory.get("aliases") or ()),
                    severity=self.severity(advisory.get("cvss")),
                    title=advisory.get("title", ""),
                )
            )
        return found


class Gate:
    def __init__(self, allowlist: Allowlist, fail_on: str, today: dt.date) -> None:
        self.allowlist = allowlist
        self.threshold = SEVERITIES.index(fail_on)
        self.today = today

    def judge(self, ecosystem: str, findings: list[Finding]) -> int:
        failing = 0
        used: set[AllowEntry] = set()
        for f in sorted(findings, key=lambda f: (-f.rank(), f.package, f.advisory)):
            entry = self.allowlist.matching(f)
            if entry is not None:
                used.add(entry)
            gates = f.rank() >= self.threshold and f.severity != "unknown"
            if gates and entry is None:
                status = "FAILS  "
                failing += 1
                print(f"::error title={f.advisory}::{f.package} {f.version}: {f.title}")
            elif entry is not None:
                status = "allowed"
            else:
                status = "report "
            print(f"{status}  {f.severity:<9} {f.package} {f.version}  {f.advisory}  {f.title}")
            if entry is not None:
                print(f"         allowed until {entry.review_by}: {entry.reason}")

        for e in self.allowlist.for_ecosystem(ecosystem):
            if e.expired(self.today):
                failing += 1
                print(
                    f"::error title=allowlist::{e.advisory} ({e.package}) was to be reviewed by "
                    f"{e.review_by}; re-assess it and move the date, or remove the entry"
                )
            elif e not in used:
                print(
                    f"::notice title=allowlist::{e.advisory} ({e.package}) no longer matches "
                    "anything; delete the entry"
                )
        return failing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("ecosystem", choices=("npm", "cargo"))
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--fail-on", choices=SEVERITIES[1:], default="critical")
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    args = parser.parse_args()

    gate = Gate(Allowlist.load(args.allowlist), args.fail_on, dt.date.today())
    auditor = {"npm": NpmAudit, "cargo": CargoAudit}[args.ecosystem]
    findings: list[Finding] = []
    for directory in args.directories:
        print(f"── {args.ecosystem} audit: {directory}")
        found = auditor(directory).findings()
        print(f"   {len(found)} advisor{'y' if len(found) == 1 else 'ies'}")
        findings.extend(found)
    failing = gate.judge(args.ecosystem, findings)
    if failing:
        print(
            f"{failing} finding(s) at or above {args.fail_on} with no allowlist entry, or expired entries"
        )
        return 1
    print(f"nothing at or above {args.fail_on} outside the allowlist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
