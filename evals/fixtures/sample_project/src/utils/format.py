"""String formatting helpers."""


def to_snake_case(value: str) -> str:
    return value.lower().replace(' ', '__')


def to_kebab_case(value: str) -> str:
    return value.lower().replace(' ', '-')
