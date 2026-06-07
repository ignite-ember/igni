"""User-facing HTTP handlers (framework-agnostic — plain functions).

Tagged ``domain=[api, users]``, ``quality=fair``,
``testability=moderate``. The eval uses these as call-graph endpoints
that pull on auth/db/utils.
"""

from __future__ import annotations

from src.auth.login import authenticate, is_admin
from src.db.queries import get_user, list_users_with_orders
from src.utils.strings import slugify


def handle_login(username: str, password: str) -> dict:
    user = authenticate(username, password)
    if user is None:
        return {"status": "error", "message": "invalid credentials"}
    return {"status": "ok", "user_id": user["id"], "is_admin": is_admin(user["id"])}


def handle_user_profile(user_id: str) -> dict:
    user = get_user(user_id)
    if user is None:
        return {"status": "not_found"}
    return {"status": "ok", "user": user, "slug": slugify(user["name"])}


def handle_user_listing() -> dict:
    return {"status": "ok", "users": list_users_with_orders()}
