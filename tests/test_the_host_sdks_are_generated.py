"""Both SDKs agree with the host schema, because both are generated from it.

``docs/HOST_OBJECT_MODEL.md``: "One schema, two SDKs." The Python SDK's
``igni_extension.host_model`` and the bridge's ``hostModel.generated.ts``
are written by :mod:`ember_code.extensions.host.codegen`. Edit the schema
and forget ``make host-sdks``, and this fails — an extension author must
never get an SDK that disagrees with the host it talks to.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from ember_code.extensions.host.codegen import (
    PYTHON_OUTPUT,
    TYPESCRIPT_OUTPUT,
    HostSdks,
    PythonGenerator,
)
from ember_code.extensions.host.definition import HOST_SCHEMA, SCHEMA_VERSION
from ember_code.extensions.host.spec import TypeExpr

ROOT = Path(__file__).resolve().parent.parent
SDK_SRC = ROOT / "packages" / "igni-extension" / "src"


def test_the_checked_in_sdks_are_what_the_schema_generates() -> None:
    stale = HostSdks(ROOT).stale()
    assert not stale, (
        f"generated from an older schema: {[str(p.relative_to(ROOT)) for p in stale]}. "
        "Run `make host-sdks`."
    )


def test_both_carry_the_schema_version() -> None:
    assert f'SCHEMA_VERSION = "{SCHEMA_VERSION}"' in (ROOT / PYTHON_OUTPUT).read_text()
    assert f'SCHEMA_VERSION = "{SCHEMA_VERSION}";' in (ROOT / TYPESCRIPT_OUTPUT).read_text()


def test_every_signal_travels_under_its_schema_wire_name_in_both() -> None:
    python = (ROOT / PYTHON_OUTPUT).read_text()
    typescript = (ROOT / TYPESCRIPT_OUTPUT).read_text()
    for spec in HOST_SCHEMA.objects:
        for signal in spec.signals:
            wire = HOST_SCHEMA.wire_name(spec, signal)
            assert f'Signal("{wire}"' in python, wire
            assert f'new Signal("{wire}")' in typescript, wire


def _sdk_module(name: str) -> object:
    sys.path.insert(0, str(SDK_SRC))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(SDK_SRC))


def test_hand_written_types_the_python_sdk_uses_match_the_schema() -> None:
    """``LocalNotification`` and ``Target`` predate the model and are kept.

    They are used in place of generated classes, so their fields must be
    the schema's — and every value the schema allows must be accepted.
    """
    notifications = _sdk_module("igni_extension.notifications")
    for schema_name, (_, python_name) in PythonGenerator.EXTERNAL.items():
        spec = HOST_SCHEMA.type(schema_name)
        assert spec is not None
        model = getattr(notifications, python_name)
        assert set(model.model_fields) == {f.name for f in spec.fields}, schema_name
        for field in spec.fields:
            if not field.optional:
                assert model.model_fields[field.name].is_required(), f"{schema_name}.{field.name}"
            TypeExpr.parse(field.type)  # a type the generators understand


def test_the_generated_python_imports_and_builds_the_whole_tree() -> None:
    host_model = _sdk_module("igni_extension.host_model")

    class NoLink:
        async def request(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("not called")

    host = host_model.Host(NoLink())  # type: ignore[attr-defined]
    for spec in HOST_SCHEMA.objects:
        node = host
        for part in filter(None, spec.path.split(".")):
            node = getattr(node, part)
        for group in (spec.properties, spec.signals, spec.methods):
            for member in group:
                assert hasattr(node, member.name), f"{spec.path}.{member.name}"
    assert host.windows.ui("w-1").PATH == "ui"
    elsewhere = host.sessions.chat("s-2")
    assert elsewhere.PATH == "chat" and elsewhere.approvals.PATH == "chat.approvals"
    assert elsewhere._session == "s-2" and elsewhere.approvals._session == "s-2"
    assert host.chat._session is None
