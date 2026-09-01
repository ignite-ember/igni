"""IDs the JetBrains plugin looks up must be IDs it registered.

`plugin.xml` declares extensions by string ID and the Kotlin fetches them
back by the same string. Nothing checks that the two agree — not the Kotlin
compiler, not `buildPlugin`, not the plugin verifier — so a rename on one
side is invisible until a user triggers the code path.

That had happened. `plugin.xml` declares:

    <notificationGroup id="igni" displayType="BALLOON" isLogByDefault="true"/>

Two call sites asked for `"igni"` and worked. Three asked for
`"EmberCode"`, which is not registered anywhere:

    actions/RestartBackendAction.kt:25
    actions/ReinstallBackendAction.kt:38
    EmberFirstLaunchActivity.kt:74   (via NOTIFICATION_GROUP = "EmberCode")

`NotificationGroupManager.getNotificationGroup` has no null-safe answer for
an unregistered ID, and the Kotlin dereferences the result immediately —
so **Tools → igni → Restart Backend** and **Reinstall Backend** did their
work and then threw instead of reporting it, and the first-launch JCEF
suggestion never appeared. The restart itself succeeded, which is what
makes this the quiet kind of broken: the user sees an IDE error where a
confirmation should be.

This is the same failure `test_env_var_prefix.py` was written for — a
renamed identifier crossing a boundary with one side missed. There the
boundary was process-to-process; here it is XML-to-Kotlin. Both are
invisible to every compiler involved.

Checked in Python rather than as a JUnit test on purpose: this needs no
JDK, no IntelliJ SDK and no 16-second Gradle run, so it fails in the
ordinary test suite that everybody already runs.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PLUGIN = _ROOT / "clients/jetbrains/src/main/resources/META-INF/plugin.xml"
_KOTLIN = _ROOT / "clients/jetbrains/src/main/kotlin"

pytestmark = pytest.mark.skipif(
    not _PLUGIN.exists(), reason="the JetBrains client is not present in this checkout"
)


def _registered_notification_groups() -> set[str]:
    xml = _PLUGIN.read_text()
    # `<notificationGroup id="x" .../>` — attributes in any order.
    blocks = re.findall(r"<notificationGroup\b[^>]*>", xml, re.S)
    ids = set()
    for block in blocks:
        m = re.search(r'\bid\s*=\s*"([^"]+)"', block)
        if m:
            ids.add(m.group(1))
    return ids


def _kotlin_sources() -> list[pathlib.Path]:
    return sorted(_KOTLIN.rglob("*.kt"))


def _looked_up_groups() -> dict[str, list[str]]:
    """id → the files that ask for it.

    Resolves `getNotificationGroup(SOME_CONST)` through a same-file
    `const val SOME_CONST = "..."`, because that indirection is exactly
    where one of the three broken call sites hid.
    """
    out: dict[str, list[str]] = {}
    for path in _kotlin_sources():
        source = path.read_text()
        consts = dict(
            re.findall(r'\bval\s+([A-Z_][A-Z0-9_]*)\s*(?::\s*String\s*)?=\s*"([^"]+)"', source)
        )
        for arg in re.findall(r"getNotificationGroup\(\s*([^)]+?)\s*\)", source):
            arg = arg.strip()
            if arg.startswith('"') and arg.endswith('"'):
                value = arg[1:-1]
            elif arg in consts:
                value = consts[arg]
            else:
                # A computed or imported id — not something this check can
                # resolve, and worth knowing about rather than skipping
                # silently.
                value = f"<unresolved: {arg}>"
            out.setdefault(value, []).append(str(path.relative_to(_ROOT)))
    return out


def test_the_scan_finds_something():
    """Both halves must be non-empty, or this passes by looking at nothing."""
    assert _registered_notification_groups(), "no <notificationGroup> found in plugin.xml"
    assert _looked_up_groups(), "no getNotificationGroup call found in the Kotlin"
    assert len(_kotlin_sources()) >= 5, len(_kotlin_sources())


def test_every_notification_group_looked_up_is_registered():
    registered = _registered_notification_groups()
    looked_up = _looked_up_groups()

    missing = {gid: files for gid, files in looked_up.items() if gid not in registered}

    assert not missing, (
        "the Kotlin asks for notification groups plugin.xml does not register:\n"
        + "\n".join(f'  "{gid}" — {", ".join(files)}' for gid, files in sorted(missing.items()))
        + f"\n  registered: {sorted(registered)}\n"
        "  An unregistered id has no group to return, and the call sites "
        "dereference the result immediately."
    )


def test_no_lookup_id_is_unresolvable():
    """If a call site starts computing its id, this check goes blind — better
    to fail and have the test taught how to read it."""
    unresolved = [gid for gid in _looked_up_groups() if gid.startswith("<unresolved:")]

    assert not unresolved, f"cannot statically resolve: {unresolved}"


def test_action_ids_are_unique():
    """A duplicate `<action id>` makes the platform drop one registration,
    and the symptom is a menu entry that silently does nothing."""
    xml = _PLUGIN.read_text()
    ids = re.findall(r'<action\b[^>]*\bid\s*=\s*"([^"]+)"', xml)

    duplicates = sorted({i for i in ids if ids.count(i) > 1})

    assert ids, "no <action id> found — has plugin.xml changed shape?"
    assert not duplicates, duplicates
