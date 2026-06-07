"""Login endpoint — DELIBERATELY VULNERABLE for the codeindex eval.

This file shows a textbook SQL-injection pattern. The eval JSONL
tags it with ``security=critical`` + ``vulnerabilities=[sql-injection]``;
the agent should find it when asked about critical security risks
or SQL injection candidates.
"""

from __future__ import annotations

from src.db.queries import run_raw


def authenticate(username: str, password: str) -> dict | None:
    """Look up the user via raw SQL string concatenation.

    The unsafe pattern here is intentional — eval fixture, not real code.
    """
    sql = f"SELECT id, role FROM users WHERE name='{username}' AND password='{password}'"
    rows = run_raw(sql)
    if not rows:
        return None
    return {"id": rows[0]["id"], "role": rows[0]["role"]}


def is_admin(user_id: str) -> bool:
    """Check admin role with another concatenated query — same hazard."""
    sql = f"SELECT role FROM users WHERE id='{user_id}'"
    rows = run_raw(sql)
    return bool(rows) and rows[0]["role"] == "admin"
