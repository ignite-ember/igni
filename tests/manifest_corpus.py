"""Manifests the extension contract must accept and refuse.

Shared by the two tests that check the contract from opposite sides:
``test_a_manifest_is_checked_against_its_schema`` drives the JSON
schema an author's editor uses, and
``test_the_manifest_model_and_its_schema_agree`` drives the Pydantic
model the backend installs through. One corpus, because the entire
risk here is the two drifting apart — a manifest the editor accepts
and the installer rejects is a support ticket, and the reverse is a
permission somebody believes they declared.

Not a test module: no ``test_`` prefix, so pytest collects nothing
from it.
"""

from __future__ import annotations

from typing import Any

#: The smallest manifest that is valid, so each case below can stay
#: about one thing.
BASE: dict[str, Any] = {
    "id": "acme.scanner",
    "version": "1.0.0",
    "contract": ">=1.0 <2",
    "trust": "unsigned",
    "sidecar": {"image": "acme__scanner:1.0.0", "mcp": {"transport": "stdio"}},
}


def with_(**overrides: Any) -> dict[str, Any]:
    """:data:`BASE` with fields replaced or added."""
    return {**BASE, **overrides}


def without(*keys: str) -> dict[str, Any]:
    """:data:`BASE` with fields removed."""
    return {k: v for k, v in BASE.items() if k not in keys}


#: ``(name, manifest, schema_error_path)``.
#:
#: The path is where the JSON schema must report the problem — an
#: error against the wrong path sends an author to the wrong line —
#: and ``""`` means the complaint belongs at the document root, which
#: is where a rule about a *combination* of fields lands.
#:
#: Every case is a mistake that was either already made in this
#: repository or is one keystroke away.
#: One asset, as a manifest declares it.
ASSET: dict[str, Any] = {
    "name": "model",
    "platform": "any",
    "file": "model.onnx",
    "size": 90405214,
    "sha256": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
    "source": "https://downloads.example/model.onnx",
}

REFUSALS: list[tuple[str, dict[str, Any], str]] = [
    (
        "a sidecar with nowhere to come from",
        with_(sidecar={"mcp": {"transport": "stdio"}}),
        "sidecar",
    ),
    (
        "both packagings at once",
        with_(endpoint={"url": "https://scanner.internal/mcp"}),
        "",
    ),
    ("neither packaging", without("sidecar"), ""),
    (
        "a signature in the manifest, which a bundle cannot carry over itself",
        with_(trust="partner", signature="detached"),
        "",
    ),
    (
        "a python package with nothing to run",
        {
            **without("sidecar"),
            "python": {"package": "acme_scanner", "mcp": {"transport": "stdio"}},
        },
        "python",
    ),
    (
        "a python package alongside a sidecar",
        with_(
            python={
                "package": "acme_scanner",
                "entry": "acme-scanner",
                "mcp": {"transport": "stdio"},
            }
        ),
        "",
    ),
    ("an id that is not reverse-DNS", with_(id="scanner"), "id"),
    ("a version that is not semver", with_(version="1.0"), "version"),
    (
        "a command without its leading slash",
        with_(contributes={"commands": [{"command": "compliance", "desc": "x"}]}),
        "contributes/commands/0/command",
    ),
    (
        "a menu entry misfiled as a command",
        with_(contributes={"commands": [{"label": "C", "command": "/c", "desc": "x"}]}),
        "contributes/commands/0",
    ),
    (
        "a prompt fragment with no text",
        with_(contributes={"promptFragments": [{"anchor": "ACME_POLICY"}]}),
        "contributes/promptFragments/0",
    ),
    (
        "an anchor in the wrong case",
        with_(contributes={"promptFragments": [{"anchor": "acme_policy", "source": "file"}]}),
        "contributes/promptFragments/0/anchor",
    ),
    (
        "a prompt target in a prompt igni does not have",
        with_(
            contributes={
                "prompt": {
                    "overrides": [
                        {"target": "system", "sections": ["a"], "source": "file", "from": "p.md"}
                    ]
                }
            }
        ),
        "contributes/prompt/overrides/0/target",
    ),
    (
        "an edit that names a section the old way",
        with_(
            contributes={
                "prompt": {
                    "edits": [
                        {"target": "main.workflow", "append": "x", "source": "file", "from": "p.md"}
                    ]
                }
            }
        ),
        "contributes/prompt/edits/0/target",
    ),
    (
        "an override that does not say its sections",
        with_(
            contributes={
                "prompt": {"overrides": [{"target": "main", "source": "file", "from": "p.md"}]}
            }
        ),
        "contributes/prompt/overrides/0",
    ),
    (
        "an edit that is no change at all",
        with_(
            contributes={
                "prompt": {"edits": [{"target": "main", "source": "file", "from": "p.md"}]}
            }
        ),
        "contributes/prompt/edits/0",
    ),
    (
        "an edit with nowhere to read it from",
        with_(
            contributes={"prompt": {"edits": [{"target": "main", "append": "x", "source": "file"}]}}
        ),
        "contributes/prompt/edits/0",
    ),
    (
        "a condition that is not a slug",
        with_(
            contributes={
                "prompt": {
                    "edits": [
                        {
                            "target": "main",
                            "append": "x",
                            "source": "file",
                            "from": "p.md",
                            "when": "Index Ready",
                        }
                    ]
                }
            }
        ),
        "contributes/prompt/edits/0/when",
    ),
    (
        "an rpc pattern that is not a prefix",
        with_(permissions={"rpc": ["ext.*.scanner.list"]}),
        "permissions/rpc/0",
    ),
    (
        "an egress entry that is a URL rather than a host",
        with_(permissions={"egress": ["https://ledger.internal"]}),
        "permissions/egress/0",
    ),
    (
        "infrastructure asked of igni, which provides none",
        with_(requires=[{"capability": "graph-db", "constraint": ">=5"}]),
        "",
    ),
    ("a typo in a top-level key", with_(permissons={"workspace": "read"}), ""),
    (
        "an asset whose file name climbs out of its directory",
        with_(assets=[{**ASSET, "file": "../escape.tar.gz"}]),
        "assets/0/file",
    ),
    (
        "an asset fetched over plain http",
        with_(assets=[{**ASSET, "source": "http://downloads.example/model.onnx"}]),
        "assets/0/source",
    ),
    (
        "an asset with no digest",
        with_(assets=[{k: v for k, v in ASSET.items() if k != "sha256"}]),
        "assets/0",
    ),
    (
        "a page kind that is not namespaced",
        with_(contributes={"pages": [{"kind": "scan", "label": "Scan"}]}),
        "contributes/pages/0/kind",
    ),
    (
        "an enum setting with no options",
        with_(contributes={"settings": [{"key": "failOn", "type": "enum", "label": "Fail on"}]}),
        "contributes/settings/0",
    ),
    (
        "a dev command on a signed manifest",
        {
            **{k: v for k, v in BASE.items() if k != "sidecar"},
            "trust": "partner",
            "dev": {"command": ["node", "./server.js"]},
        },
        "trust",
    ),
    (
        "a dev command alongside a sidecar",
        with_(dev={"command": ["node", "./server.js"]}),
        "",
    ),
    (
        "a dev command with nothing to run",
        {
            **{k: v for k, v in BASE.items() if k != "sidecar"},
            "dev": {"command": []},
        },
        "dev/command",
    ),
    (
        "a workspace grant that is not one of the three",
        with_(permissions={"workspace": "admin"}),
        "permissions/workspace",
    ),
    (
        "a tool named with no hints",
        with_(permissions={"tools": ["acme.scanner.list"]}),
        "permissions/tools/0",
    ),
    (
        "a tool that leaves a hint to MCP's default",
        with_(
            permissions={
                "tools": [
                    {
                        "name": "acme.scanner.list",
                        "annotations": {
                            "readOnlyHint": True,
                            "destructiveHint": False,
                            "idempotentHint": True,
                        },
                    }
                ]
            }
        ),
        "permissions/tools/0/annotations",
    ),
    (
        "a tool that reads and destroys",
        with_(
            permissions={
                "tools": [
                    {
                        "name": "acme.scanner.list",
                        "annotations": {
                            "readOnlyHint": True,
                            "destructiveHint": True,
                            "idempotentHint": True,
                            "openWorldHint": False,
                        },
                    }
                ]
            }
        ),
        "permissions/tools/0/annotations",
    ),
]
