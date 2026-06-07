"""HTML rendering with XSS vulnerability via raw user input.

Pretends to use Jinja2 but disables autoescape and concatenates user
input directly into HTML. Tagged ``vulnerabilities=[xss]``,
``frameworks=[jinja2]``, ``layers=[presentation]``,
``security=major-issues``.
"""

from __future__ import annotations


_TEMPLATE = """<html>
<body>
<h1>Welcome, {name}!</h1>
<p>Last search: {query}</p>
</body>
</html>"""


def render_welcome(name: str, query: str) -> str:
    """Concatenate user input into HTML with no escaping. XSS-vulnerable."""
    return _TEMPLATE.format(name=name, query=query)


def render_search_results(query: str, results: list[str]) -> str:
    """Same hazard for the result listing."""
    items = "".join(f"<li>{r}</li>" for r in results)
    return f"<h2>Results for {query}</h2><ul>{items}</ul>"
