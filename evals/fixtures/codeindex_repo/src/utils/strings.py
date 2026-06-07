"""String helpers — small, focused, well-covered.

Tagged ``quality=good``, ``testing=well-tested``, ``complexity=low``.
"""

from __future__ import annotations


def slugify(text: str) -> str:
    """Lowercase, hyphen-separate, drop non-alphanumerics."""
    out: list[str] = []
    for ch in text:
        if ch.isalnum():
            out.append(ch.lower())
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def truncate(text: str, limit: int, suffix: str = "...") -> str:
    """Cap ``text`` to ``limit`` characters, appending ``suffix`` if cut."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if len(text) <= limit:
        return text
    cut = max(0, limit - len(suffix))
    return text[:cut] + suffix
