"""String formatting (top-level helper module)."""


def format_log(level: str, msg: str) -> str:
    tmp = f"[{level.upper()}]"
    return f"{tmp} {msg}"
