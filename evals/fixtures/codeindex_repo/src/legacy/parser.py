"""Legacy expression parser — the "scares me at 5pm on Friday" file.

Deeply nested, no tests, mixes parsing with evaluation.

Tagged ``complexity=very-high``, ``testing=untested``,
``needs_refactoring=true``, ``priority=high``, ``quality=poor``.
"""

from __future__ import annotations


def parse_and_eval(expr: str) -> int | None:
    """Parse a tiny arithmetic expression and evaluate it.

    Recursive descent with a hand-rolled state machine. The intent here
    is to demonstrate "very-high complexity" honestly — not to ship.
    """
    tokens = []
    i = 0
    while i < len(expr):
        c = expr[i]
        if c.isdigit():
            j = i
            while j < len(expr) and expr[j].isdigit():
                j += 1
            tokens.append(int(expr[i:j]))
            i = j
        elif c in "+-*/()":
            tokens.append(c)
            i += 1
        elif c.isspace():
            i += 1
        else:
            return None

    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def eat(tok):
        nonlocal pos
        if peek() == tok:
            pos += 1
            return True
        return False

    def factor():
        nonlocal pos
        if isinstance(peek(), int):
            v = tokens[pos]
            pos += 1
            return v
        if eat("("):
            v = expression()
            if not eat(")"):
                return None
            return v
        return None

    def term():
        v = factor()
        if v is None:
            return None
        while peek() in ("*", "/"):
            op = tokens[pos]
            pos_save = pos
            pos += 1
            r = factor()
            if r is None:
                pos = pos_save
                return v
            if op == "*":
                v = v * r
            else:
                if r == 0:
                    return None
                v = v // r
        return v

    def expression():
        v = term()
        if v is None:
            return None
        while peek() in ("+", "-"):
            op = tokens[pos]
            pos_save = pos
            pos += 1
            r = term()
            if r is None:
                pos = pos_save
                return v
            if op == "+":
                v = v + r
            else:
                v = v - r
        return v

    result = expression()
    return result if pos == len(tokens) else None
