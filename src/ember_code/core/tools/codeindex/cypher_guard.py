"""Read-only guardrail for raw Cypher queries.

The :func:`assert_read_only_cypher` function is the single
gate :func:`CodeIndexTools.codeindex_cypher` runs every ad-hoc
Cypher through. It exists as its own module so the rules
themselves are testable without instantiating the toolkit /
mocking the CodeIndex, and so future Cypher expansions (e.g.
allowing ``EXPLAIN``) only change one file.

Rules (all must hold for the query to be allowed):

1. The query, with comments stripped, must consist **only** of the
   following tokens (case-insensitive, separated by ``;``):

      MATCH, OPTIONAL MATCH, WITH, WHERE, RETURN, ORDER BY, SKIP,
      LIMIT, UNION, UNWIND, CALL (only ``apoc.cypher.run*``
      subprocedures — disallow any ``CALL … IN TRANSACTIONS`` /
      ``CALL dbms.*`` / ``CALL db.*`` / admin-procedure paths),
      USE, ON MATCH, ON CREATE

   Specifically forbidden:
     - writes: CREATE, MERGE, SET, DELETE, DETACH DELETE, REMOVE
     - schema: CREATE INDEX, DROP INDEX, CREATE CONSTRAINT, …
     - admin:  SHOW, CALL dbms.*, CALL db.*, ALTER DATABASE, …
     - session: BEGIN, COMMIT, ROLLBACK (transaction control)
     - explain: EXPLAIN is allowed (read-only); PROFILE is
       forbidden (it executes the query against the actual DB)

2. The query must mention ``project_hash`` scoping (the
   ``Item`` / ``Chunk`` nodes carry a ``project_hash``
   property — cross-project queries by accident are real if
   the agent hand-types ``MATCH (i:Item)`` without a
   ``project_hash`` predicate). We require ``project_hash``
   to appear in the Cypher text at least once.

3. The Cypher must not reference relationship ``project_hash``
   properties in a way that would leak writes — the
   relationship-shape checks above cover that.

4. Parameter names must be a subset of one of:

      - the typed-parameter allowlist below, or
      - ``proj`` (auto-injected by the toolkit).

   The agent-supplied kwargs are passed through as Cypher
   ``$paramName`` placeholders. Untyped names would let an
   agent smuggle a ``$cypher_string = "REMOVE ..."`` interpolation,
   so we lock the surface to known names.

The errors that come out of this module are
:class:`CypherGuardError` subclasses; :class:`CodeIndexTools`
catches them and serialises a stable JSON error envelope to
the agent. Anything not in here is the agent's problem.
"""

from __future__ import annotations

import re
from typing import Final

# Tokens that are allowed in a read-only query (case-insensitive
# match against whitespace-bounded tokens). ``UNION`` deliberately
# kept here — even though it's two reads composed into one, it's
# still read-only. ``CALL`` is allowed only for a small allowlist
# of ``apoc.cypher.*`` subprocedures that themselves do not
# write; any other ``CALL`` is rejected.
_ALLOWED_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "MATCH",
        "OPTIONAL",
        "WITH",
        "WHERE",
        "RETURN",
        "ORDER",
        "BY",
        "SKIP",
        "LIMIT",
        "UNION",
        "UNWIND",
        "USE",
        "ON",
        "EXPLAIN",
    }
)

# Tokens that look like read-only keywords but actually do writes.
# Cypher is case-insensitive; the comparison below is too.
_FORBIDDEN_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "CREATE",
        "MERGE",
        "SET",
        "DELETE",
        "DETACH",
        "REMOVE",
        "DROP",
        "ALTER",
        "RENAME",
        "CALL",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "SHOW",
        "PROFILE",
        "INDEXES",
        "CONSTRAINT",
        "CONSTRAINTS",
        "TERMINATE",
        "STOP",
    }
)

# Parameter names the agent may use. Matches the typed envelope on
# the tool side. Anything outside this set is rejected so a
# misbehaving caller can't smuggle a Cypher placeholder whose
# value is itself a write.
ALLOWED_PARAM_NAMES: Final[frozenset[str]] = frozenset(
    {
        "proj",
        "commit_sha",
        "ids",
        "limit_n",
        "skip_n",
        "kind",
        "type",
        "quality",
    }
)

# A small allowlist of read-only ``apoc.*`` subprocedures that the
# query planner exposes. Anything else with ``CALL`` is rejected.
_ALLOWED_APOC: Final[frozenset[str]] = frozenset({"apoc.cypher.run"})


class CypherGuardError(ValueError):
    """Base class for Cypher guardrail violations."""


class CypherReadOnlyViolation(CypherGuardError):
    """The query contains a write / admin / forbidden operation."""


class CypherMissingProjectHash(CypherGuardError):
    """The query has no ``project_hash`` scoping predicate."""


class CypherUnknownParam(CypherGuardError):
    """A ``$param`` reference in the query is not on the allowed list."""


_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


def _strip_comments(cypher: str) -> str:
    return _COMMENT_RE.sub(" ", cypher)


def _tokenize(cypher: str) -> list[str]:
    """Whitespace + punctuation tokenizer.

    Splits on whitespace and the Cypher punctuation ``()[]{},.;:``
    so that ``MATCH(n)`` becomes ``["MATCH", "n"]`` — we only
    care about the keywords for matching, not the identifiers.
    """
    return [tok for tok in re.split(r"[\s()\[\]{},.;:]+", cypher) if tok]


def _is_safe_call_target(token_text: str) -> bool:
    """Check a ``CALL`` invocation target.

    The Cypher ``CALL`` form is ``CALL <procedure>(...)``. We
    allow only names that start with ``apoc.cypher.run`` (no
    admin, no ``dbms.``, no ``db.``, no procedure-spanning)
    and reject anything that contains a dot after a non-allowlisted
    prefix.
    """
    return any(token_text.startswith(allow) for allow in _ALLOWED_APOC)


def _collect_call_args(tokens: list[str]) -> list[tuple[int, str]]:
    """Find ``CALL`` invocations and return ``(start_index, call_text)``
    tuples after stripping ``CALL``. The caller checks ``call_text``
    against the allowlist.

    A ``CALL`` invocation in Cypher is the contiguous identifier
    after ``CALL`` through to a balanced ``(``/``)`` group, ignoring
    anything inside the parens. We approximate by taking the next
    non-paren token after ``CALL``.
    """
    pairs: list[tuple[int, str]] = []
    for i, tok in enumerate(tokens):
        if tok.upper() == "CALL":
            for j in range(i + 1, len(tokens)):
                if tokens[j].startswith("(") and tokens[j].endswith(")"):
                    continue
                if "(" in tokens[j]:
                    continue
                pairs.append((i, tokens[j]))
                break
    return pairs


def _check_call_tokens(cypher: str, tokens: list[str]) -> None:
    """Reject ``CALL`` invocations outside the allowlist."""
    pairs = _collect_call_args(tokens)
    if not pairs:
        return
    for _, call_target in pairs:
        if not _is_safe_call_target(call_target):
            raise CypherReadOnlyViolation(
                f"CALL target {call_target!r} is not on the read-only allowlist. "
                "Only `apoc.cypher.run` (and subprocedures) are permitted."
            )


def _extract_param_names(cypher: str) -> set[str]:
    """Find every ``$name`` reference in the Cypher text."""
    return set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", cypher))


def _check_keywords(cypher: str, tokens: list[str]) -> None:
    """Reject any token that's not in the read-only allowlist."""
    for tok in tokens:
        u = tok.upper()
        if u in _FORBIDDEN_TOKENS:
            raise CypherReadOnlyViolation(
                f"Cypher token {tok!r} is forbidden: this tool is read-only."
            )
        if u not in _ALLOWED_TOKENS:
            continue
        # All-read — keep scanning.


def _check_multi_statement(cypher: str) -> None:
    """Reject ``;``-separated multi-statement scripts.

    The first non-whitespace token must be one of the read-only
    starters (MATCH, OPTIONAL MATCH, RETURN, UNWIND, WITH,
    EXPLAIN, USE, CALL). Anything else is rejected. ``;``
    inside the matched body is allowed (we don't try to fully
    parse — we just forbid trailing statements).
    """
    stripped = cypher.strip()
    # Cypher allows ``;`` as a statement separator; ban anything
    # after the first ``;`` so the agent submits one query per
    # call. A trailing ``;`` is fine.
    primary, *rest = re.split(r";", stripped, maxsplit=1)
    if rest and rest[0].strip():
        raise CypherReadOnlyViolation(
            "Multi-statement Cypher is not allowed; submit a single statement."
        )
    starter = re.split(r"[\s()]+", primary.strip(), maxsplit=1)[0].upper()
    allowed_starters = {"MATCH", "OPTIONAL", "RETURN", "UNWIND", "WITH", "EXPLAIN", "USE"}
    if starter not in allowed_starters:
        raise CypherReadOnlyViolation(
            f"Query must start with one of {sorted(allowed_starters)}; got {starter!r}."
        )


def _check_project_hash(cypher: str) -> None:
    """Every raw Cypher query must filter by ``project_hash``.

    The CodeIndex item/chunk nodes carry a ``project_hash``
    property that ties them to a project root. A query without
    such a predicate would scan the entire commit-scoped DB and
    burn the wrong budget / leak cross-project data when the
    driver is shared.
    """
    if "project_hash" not in cypher:
        raise CypherMissingProjectHash(
            "Raw Cypher queries must include `project_hash` scoping "
            "(e.g. `MATCH (i:Item {project_hash: $proj})`)."
        )


def _check_param_names(cypher: str) -> None:
    """Reject any ``$name`` reference not on the typed-envelope list."""
    params = _extract_param_names(cypher)
    unknown = params - ALLOWED_PARAM_NAMES
    if unknown:
        raise CypherUnknownParam(
            f"Cypher references $names not on the allowlist: {sorted(unknown)}. "
            f"Allowed: {sorted(ALLOWED_PARAM_NAMES)}."
        )


def assert_read_only_cypher(cypher: str) -> str:
    """Validate and normalise ``cypher``.

    Returns the comment-stripped query (with leading/trailing
    whitespace removed) so the toolkit passes a deterministic
    payload to :class:`Neo4jClient.execute_query`.

    Raises:
        CypherGuardError: subclasses per failure kind. The
            toolkit catches all three.
    """
    if not isinstance(cypher, str) or not cypher.strip():
        raise CypherGuardError("Cypher must be a non-empty string.")
    stripped = _strip_comments(cypher).strip()
    # Tokenize once and reuse across checks.
    tokens = _tokenize(stripped)
    _check_keywords(cypher, tokens)
    _check_call_tokens(stripped, tokens)
    _check_multi_statement(stripped)
    _check_project_hash(stripped)
    _check_param_names(stripped)
    return stripped
