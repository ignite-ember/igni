"""Unit tests for the Neo4j row codec.

Exercises :class:`Neo4jRowCodec` in isolation — no driver, no
database. Round-trips :class:`CodeIndexItem` → params → dict →
:class:`CodeIndexResult` to confirm the write/read pair agrees
on field shape and semantics.
"""

from __future__ import annotations

from uuid import uuid4

from ember_code.core.code_index.enums import FileSystemType
from ember_code.core.code_index.neo4j_codec import Neo4jRowCodec
from ember_code.core.code_index.schema.items import CodeIndexItem


def _make_item(**overrides) -> CodeIndexItem:
    """Build a CodeIndexItem with sensible defaults + per-test overrides."""
    base = dict(
        item_id=str(uuid4()),
        name="verify_password",
        type=FileSystemType.ENTITY,
        path="src/auth.py::verify_password",
        kind="code",
        entity_type="function",
        file_extension=".py",
        parent_id=str(uuid4()),
        content="def verify_password(p): return bcrypt.hashpw(p, salt)",
        token_count=42,
        line_from=12,
        line_to=18,
        needs_refactoring=False,
        quality="good",
        complexity="low",
        security="secure",
        vulnerabilities=["sql-injection"],
        frameworks=["fastapi", "sqlalchemy"],
        domain=["auth"],
    )
    base.update(overrides)
    return CodeIndexItem(**base)


def test_to_node_params_preserves_lists_and_nulls() -> None:
    codec = Neo4jRowCodec()
    item = _make_item()
    params = codec.to_node_params(item, content=item.content)
    # Lists become native Python lists (Neo4j stores them as list properties).
    assert params["vulnerabilities"] == ["sql-injection"]
    assert params["frameworks"] == ["fastapi", "sqlalchemy"]
    # Quality fields preserved as enum strings.
    assert params["quality"] == "good"
    assert params["security"] == "secure"
    # Line range preserved as nullable ints.
    assert params["line_from"] == 12
    assert params["line_to"] == 18


def test_to_node_params_handles_missing_fields_as_none() -> None:
    codec = Neo4jRowCodec()
    item = _make_item(line_from=None, line_to=None, quality=None, entity_type=None)
    params = codec.to_node_params(item, content=None)
    # ``None`` becomes ``None`` (Neo4j null) — no "" / -1 sentinels.
    assert params["line_from"] is None
    assert params["line_to"] is None
    assert params["quality"] is None
    assert params["entity_type"] is None


def test_round_trip_preserves_critical_fields() -> None:
    """write → read: same item, same result."""
    codec = Neo4jRowCodec()
    item = _make_item()
    props = codec.to_node_params(item, content=item.content)
    result = codec.from_node(item.item_id, props, content=item.content)
    assert result.item_id == item.item_id
    assert result.name == "verify_password"
    assert result.commit == ""  # session-derived; codec doesn't store it
    assert result.line_from == 12
    assert result.line_to == 18
    # Lists come back as lists.
    assert result.vulnerabilities == ["sql-injection"]
    assert result.frameworks == ["fastapi", "sqlalchemy"]
    assert result.domain == ["auth"]
    # Quality strings come back as strings (empty if missing).
    assert result.security == "secure"


def test_from_node_handles_missing_props_gracefully() -> None:
    """A node with only the required ``item_id`` shouldn't crash the reader."""
    codec = Neo4jRowCodec()
    result = codec.from_node("u1", {"item_id": "u1"}, content="hi")
    assert result.item_id == "u1"
    assert result.commit == ""  # session-derived; codec doesn't store it
    assert result.content == "hi"
    assert result.line_from is None
    assert result.line_to is None
    # Missing quality → empty string (CodeIndexResult convention).
    assert result.quality == ""
    # Missing lists → empty lists.
    assert result.vulnerabilities == []
