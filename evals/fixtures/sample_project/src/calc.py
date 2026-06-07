"""Arithmetic helpers."""


def add(a: int, b: int) -> int:
    return a + b


def divide(a: int, b: int) -> float | None:
    if b == 0:
        raise ZeroDivisionError("cannot divide by zero")
    return a / b
