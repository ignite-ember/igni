"""Data-access layer with the textbook N+1 query problem.

Tagged ``layers=[data-access]``, ``file_issues=[n+1-query]``,
``quality=poor``, ``technical_debt=high``.
"""

from __future__ import annotations

from typing import Any


def run_raw(sql: str) -> list[dict[str, Any]]:
    """Stub — pretend this hits the database. The eval doesn't run code."""
    return []


def list_users_with_orders() -> list[dict]:
    """Classic N+1: one query per user instead of a single JOIN."""
    users = run_raw("SELECT id, name FROM users")
    out = []
    for user in users:
        # One extra query per user — N+1.
        orders = run_raw(f"SELECT * FROM orders WHERE user_id={user['id']}")
        out.append({"user": user, "order_count": len(orders)})
    return out


def get_user(user_id: str) -> dict | None:
    rows = run_raw(f"SELECT * FROM users WHERE id={user_id}")
    return rows[0] if rows else None
