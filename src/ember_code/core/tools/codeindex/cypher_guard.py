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

2. Parameter names must be a subset of one of:

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
        # ``CALL`` is deliberately absent. It used to be here, which made
        # ``_check_call_tokens`` dead code — the keyword check runs first, so
        # every CALL was rejected and the vector index could not be reached.
        # The target allowlist in ``_is_safe_call_target`` is the real gate.
        #
        # A CALL that smuggles a write *in a subquery* is caught by the write
        # tokens below, which apply to the whole statement. A write inside a
        # string the procedure then executes is NOT — string literals are
        # stripped before this scan. That is why ``_ALLOWED_APOC`` is empty:
        # any procedure that runs its argument defeats the token scan.
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
        "path_prefix",
        # Semantic search. The agent passes ``semantic_query`` as *text*; the
        # service embeds it and binds the result as ``query_vector``, because a
        # model cannot write a 384-dim literal and there was previously no way to
        # reach the vector index at all — 2,255 evaluation queries used it zero
        # times, which is what an unreachable capability looks like.
        "semantic_query",
        "query_vector",
    }
)

# Empty on purpose, and kept as a named constant so the reason survives.
#
# ``apoc.cypher.run`` was the sole entry, and it is a write primitive
# wearing a read-only costume: it *executes* its first argument, and that
# argument is a string literal — which the token scanner strips before
# looking for forbidden keywords, exactly so that
# ``WHERE n.name = 'CREATE'`` is not rejected. The two rules compose into
# a bypass:
#
#     CALL apoc.cypher.run('CREATE (n:X) RETURN n', {})
#
# That passed the guard. The comment beside ``_FORBIDDEN_TOKENS`` claimed
# "a CALL that smuggles a write is still caught by the write tokens
# below" — true for a subquery, false for a string the procedure goes on
# to run.
#
# Nothing referenced it: no query in the service, no prompt, no test, no
# doc. So this is a removal rather than a trade-off — the capability had
# no consumer and the hole was the whole of its effect. Adding any
# procedure here again means auditing whether it can execute a string.
_ALLOWED_APOC: Final[frozenset[str]] = frozenset()

# Read-only procedures beyond apoc. ``db.index.vector.queryNodes`` only reads a
# vector index, and without it the chunk embeddings — 93% of the nodes written,
# and the dominant cost of every load — cannot be queried by an agent at all.
# Two read-only index procedures, and only these. ``queryNodes`` on the vector
# index finds by meaning; on the full-text index it finds by term — and the
# second is the one that can return nothing, which similarity never does.
_ALLOWED_PROCEDURES: Final[frozenset[str]] = frozenset(
    {"db.index.vector.querynodes", "db.index.fulltext.querynodes"}
)


class CypherGuardError(ValueError):
    """Base class for Cypher guardrail violations."""


class CypherReadOnlyViolation(CypherGuardError):
    """The query contains a write / admin / forbidden operation."""


class CypherUnknownParam(CypherGuardError):
    """A ``$param`` reference in the query is not on the allowed list."""


_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)

# Cypher string literals (both quote styles, backslash escapes honoured) and
# backtick-quoted identifiers.
#
# These are redacted before the keyword scan, because otherwise the scan's
# verdict on a string depends on where in it a keyword happens to sit. The
# tokeniser splits on whitespace and Cypher punctuation but not on quotes, so
# a quote glues itself to the word it touches:
#
#     WHERE n.body CONTAINS 'DELETE FROM users'   → tokens 'DELETE, FROM, users'   → ALLOWED
#     WHERE n.body CONTAINS 'and then DELETE it'  → tokens 'and, then, DELETE, it' → REJECTED
#
# Same keyword, same kind of string, opposite outcomes — and the rejection
# says "this tool is read-only" about a query that only reads. Searching a
# code graph for code that contains `DELETE` or `DROP TABLE` is an ordinary
# thing to want, so the false rejection is not hypothetical.
#
# Redacting is safe here, and it is worth being precise about why: a keyword
# inside a string is only dangerous if something goes on to *execute* that
# string. Nothing can. ``_ALLOWED_APOC`` is empty precisely because
# ``apoc.cypher.run`` did exactly that, and the two procedures in
# ``_ALLOWED_PROCEDURES`` take an index name and a search term, neither of
# which is Cypher. The allowlist is the gate; the token scan is defence for
# the statement itself.
#
# (The comment beside ``_ALLOWED_APOC`` said string literals were already
# stripped before the scan. They were not — nothing stripped them, and the
# quote-adjacency accident above is what made a write inside a string
# invisible. Its conclusion was right and its mechanism was wrong.)
_STRING_RE = re.compile(
    r"'(?:[^'\\]|\\.)*'"
    r'|"(?:[^"\\]|\\.)*"'
    r"|`(?:[^`\\]|\\.)*`",
    re.DOTALL,
)


def _strip_comments(cypher: str) -> str:
    return _COMMENT_RE.sub(" ", cypher)


def _redact_strings(cypher: str) -> str:
    """Replace every string literal with a placeholder of the same shape.

    Only ever used for *scanning*. ``assert_read_only_cypher`` returns the
    query with its literals intact — handing the database a redacted query
    would break every search this function exists to permit.
    """
    return _STRING_RE.sub(" '' ", cypher)


def _tokenize(cypher: str) -> list[str]:
    """Whitespace + punctuation tokenizer.

    Splits on whitespace and the Cypher punctuation ``()[]{},.;:``
    so that ``MATCH(n)`` becomes ``["MATCH", "n"]`` — we only
    care about the keywords for matching, not the identifiers.
    """
    return [tok for tok in re.split(r"[\s()\[\]{},.;:]+", cypher) if tok]


def _is_safe_call_target(token_text: str) -> bool:
    """Check a ``CALL`` invocation target.

    The Cypher ``CALL`` form is ``CALL <procedure>(...)``. Allowed: the
    read-only index lookups in ``_ALLOWED_PROCEDURES``. Everything else is
    rejected — no admin, no ``dbms.``, no other ``db.`` procedure, and no
    ``apoc.`` (see ``_ALLOWED_APOC`` for why that one is empty).

    The vector lookup is an exception worth naming: it only reads a vector
    index, and blocking it made the chunk embeddings unreachable from the one
    tool an agent has.
    """
    lowered = token_text.lower()
    if any(lowered.startswith(allow) for allow in _ALLOWED_PROCEDURES):
        return True
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


# The procedure name after ``CALL``, dotted, taken from the text rather than the
# token stream: the tokeniser splits on dots, so a target arrived as ``db`` and
# an allowlist of dotted names could never match it. That is why the documented
# ``apoc.cypher.run`` allowance never actually worked either. ``CALL {`` opens a
# subquery rather than naming a procedure and is skipped — any write inside it is
# still caught by the forbidden-token check, which scans the whole statement.
_CALL_TARGET_RE: Final = re.compile(r"\bCALL\s+(?!\{)([A-Za-z_][\w.]*)", re.IGNORECASE)


def _check_call_tokens(cypher: str, tokens: list[str]) -> None:
    """Reject ``CALL`` invocations outside the allowlist."""
    targets = _CALL_TARGET_RE.findall(cypher)
    if not targets:
        return
    for call_target in targets:
        if not _is_safe_call_target(call_target):
            raise CypherReadOnlyViolation(
                f"CALL target {call_target!r} is not on the read-only allowlist. "
                "Only `apoc.cypher.run` (and subprocedures) and "
                "`db.index.vector.queryNodes` are permitted."
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
    # ``CALL`` belongs here: a vector lookup opens with it, and the docstring
    # above has always claimed it was allowed. Which procedure may be called is
    # gated by ``_is_safe_call_target``; a write is rejected wherever it appears.
    allowed_starters = {"MATCH", "OPTIONAL", "RETURN", "UNWIND", "WITH", "EXPLAIN", "USE", "CALL"}
    if starter not in allowed_starters:
        raise CypherReadOnlyViolation(
            f"Query must start with one of {sorted(allowed_starters)}; got {starter!r}."
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
    # Scan against the string-redacted form so a literal's contents cannot
    # decide the verdict; return the real query below.
    scannable = _redact_strings(stripped)
    # Tokenize once and reuse across checks.
    tokens = _tokenize(scannable)
    _check_keywords(scannable, tokens)
    _check_call_tokens(scannable, tokens)
    _check_multi_statement(scannable)
    # Note: no `project_hash` predicate check. Each (project, commit)
    # runs in its own Neo4j PROCESS (see `neo4j_schema.py:1-31`), so
    # cross-project leakage is impossible by construction — the
    # process boundary is the isolation, not a query-time predicate.
    # An earlier `_check_project_hash` guard was removed because it
    # both duplicated the process boundary and made every query
    # verbose without adding safety.
    _check_param_names(scannable)
    return stripped
