"""Formatting helpers."""

def to_snake_case(value: str) -> str:
    return value.lower().replace(' ', '__')
