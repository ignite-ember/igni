"""Read-only guardrail for raw Cypher queries.

The :func:`assert_read_only_cypher` function is the single
gate :func:`CodeIndexTools.codeindex_cypher` runs every ad-hoc
Cypher through. It exists as its own module so the rules
themselves are testable without instantiating the toolkit /
mocking the CodeIndex, and so future Cypher expansions (e.g.
allowing ``EXPLAIN``) only change one file.

Rules (all must hold for the query to be allowed):

1. The query must open with one of the read-only starters
   (``_check_multi_statement``) and must not contain any of:

     - write / admin / transaction keywords (``_FORBIDDEN_TOKENS``):
       CREATE, MERGE, SET, DELETE, DETACH DELETE, REMOVE, DROP,
       SHOW, PROFILE, BEGIN/COMMIT/ROLLBACK, …
     - the multi-word clauses in ``_FORBIDDEN_PHRASES``: ``LOAD CSV``
       (which makes the *server* fetch a URL of the query's choosing),
       ``… IN TRANSACTIONS``, ``FOREACH``, ``USING PERIODIC COMMIT``.
       These are phrases rather than tokens because their first word —
       ``LOAD``, ``IN``, ``USING`` — is an ordinary identifier in a code
       graph and rejecting it on its own would be a false positive.
     - a ``CALL`` whose target is outside ``_ALLOWED_PROCEDURES`` (the
       vector and fulltext index lookups; no ``dbms.*``, no other
       ``db.*``, no ``apoc.*``).

   There is deliberately no *allowlist* of tokens. A whitespace
   tokeniser cannot tell the keyword ``MATCH`` from a variable called
   ``match``, so an allowlist either rejects every identifier in the
   query or — as the one here did for as long as it existed — is
   evaluated and thrown away, which is how ``LOAD CSV`` passed.

   ``EXPLAIN`` is allowed (it plans, it does not run); ``PROFILE`` is
   forbidden (it executes the query against the actual DB).

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
from typing import Final, NamedTuple

# There was an ``_ALLOWED_TOKENS`` frozenset here, and it never changed a
# verdict: ``_check_keywords`` looked a token up in it and ``continue``d either
# way. The module docstring described the guard as an allowlist on that basis,
# so the clauses it did not list were assumed rejected. They were not — most of
# the read-only story is carried by ``_FORBIDDEN_TOKENS`` below, and
# ``LOAD CSV FROM 'http://…'`` after a ``WITH`` contains none of those tokens,
# opens with ``MATCH``, and made the Neo4j host fetch an attacker-chosen URL.
#
# It cannot be resurrected as written: the tokeniser splits on whitespace and
# punctuation, so every identifier, label, property and function name in the
# query arrives as a token too, and an allowlist of clause keywords rejects all
# of them. The dangerous multi-word clauses are denied by phrase instead —
# see ``_FORBIDDEN_PHRASES``.

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

# Clauses whose first word is an ordinary identifier in a code graph, so they
# can only be denied as a phrase. Each entry is (pattern, what it does).
#
# ``LOAD CSV`` is the one that matters: it is not a write, so no forbidden
# token appears in it, and it is legal after a ``WITH`` — which means
# ``MATCH (i:Item) WITH i LIMIT 1 LOAD CSV FROM 'http://attacker/x' AS row
# RETURN row`` opened with an allowed starter and passed every check. The
# effect is that the *Neo4j host* issues an HTTP request to a URL chosen by
# whoever wrote the query, and this guard is the only gate between agent-
# authored Cypher and the database (``tool.py``).
#
# ``… IN TRANSACTIONS`` turns a ``CALL`` subquery into batched auto-commit
# transactions — transaction control, which the forbidden tokens otherwise ban
# by name. ``FOREACH`` and ``USING PERIODIC COMMIT`` exist only to drive
# updates.
_FORBIDDEN_PHRASES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\bLOAD\s+CSV\b", re.IGNORECASE), "LOAD CSV"),
    (re.compile(r"\bIN\s+TRANSACTIONS\b", re.IGNORECASE), "CALL … IN TRANSACTIONS"),
    (re.compile(r"\bFOREACH\s*\(", re.IGNORECASE), "FOREACH"),
    (re.compile(r"\bUSING\s+PERIODIC\s+COMMIT\b", re.IGNORECASE), "USING PERIODIC COMMIT"),
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


# Strings and comments are resolved in one left-to-right pass, because they
# nest into each other and any two-regex ordering gets one of the directions
# wrong:
#
#   * comments first, then strings:  ``WHERE n.url CONTAINS 'http://x' RETURN n``
#     — the ``//`` inside the literal starts a line comment, so the rest of the
#     query is eaten. That was the shipped behaviour, and the *mangled* query
#     was the one returned to the caller and executed: the tool answered a
#     different question than the agent asked, silently. Searching a code graph
#     for ``http://``, ``//`` or a comment marker is an ordinary thing to want.
#   * strings first, then comments:  ``MATCH (n) // don't\nDELETE n WHERE a='x'``
#     — the apostrophe in the comment opens a "literal" that swallows the
#     ``DELETE``, and the write becomes invisible to the scan.
#
# A single pass with one state (in-string / in-comment / neither) is what the
# database itself does, so the scanned text and the executed text agree.
#
# Why literals are blanked for the scan at all: otherwise the verdict on a
# string depends on where in it a keyword happens to sit. The tokeniser splits
# on whitespace and Cypher punctuation but not on quotes, so a quote glues
# itself to the word it touches:
#
#     WHERE n.body CONTAINS 'DELETE FROM users'   → tokens 'DELETE, FROM, users'   → ALLOWED
#     WHERE n.body CONTAINS 'and then DELETE it'  → tokens 'and, then, DELETE, it' → REJECTED
#
# Same keyword, same kind of string, opposite outcomes. Blanking is safe
# because a keyword inside a string is only dangerous if something goes on to
# *execute* that string, and nothing can: ``_ALLOWED_APOC`` is empty precisely
# because ``apoc.cypher.run`` did exactly that, and the two procedures in
# ``_ALLOWED_PROCEDURES`` take an index name and a search term, neither of
# which is Cypher.
_QUOTES: Final[str] = "'\"`"


class _LexedCypher(NamedTuple):
    """The two views of one query produced by :func:`_lex`.

    ``scannable`` is what every check reads: literals blanked, comments gone.
    ``without_comments`` is what gets executed: literals intact, comments gone.
    """

    scannable: str
    without_comments: str


def _lex(cypher: str) -> _LexedCypher:
    """Split ``cypher`` into a scannable view and an executable view."""
    scan: list[str] = []
    kept: list[str] = []
    i, n = 0, len(cypher)
    while i < n:
        ch = cypher[i]
        if ch in _QUOTES:
            end = _end_of_literal(cypher, i)
            if end is None:
                raise CypherGuardError(
                    f"Unterminated {ch} literal in Cypher — the guard cannot tell "
                    "where the string ends, so it cannot tell what would run."
                )
            kept.append(cypher[i:end])
            scan.append(" '' ")
            i = end
            continue
        if cypher.startswith("//", i):
            end = cypher.find("\n", i)
            i = n if end == -1 else end
            scan.append(" ")
            kept.append(" ")
            continue
        if cypher.startswith("/*", i):
            end = cypher.find("*/", i + 2)
            i = n if end == -1 else end + 2
            scan.append(" ")
            kept.append(" ")
            continue
        scan.append(ch)
        kept.append(ch)
        i += 1
    return _LexedCypher("".join(scan), "".join(kept))


def _end_of_literal(cypher: str, start: int) -> int | None:
    """Index just past the literal opening at ``start``, or None if unclosed."""
    quote = cypher[start]
    i = start + 1
    n = len(cypher)
    while i < n:
        if cypher[i] == "\\":
            i += 2
            continue
        if cypher[i] == quote:
            return i + 1
        i += 1
    return None


# Identifier positions. A Cypher write is a keyword in *statement* position —
# ``DELETE n``, ``CREATE (x)``, ``SET n.p = 1``. The same word after ``AS``,
# after a ``.``, or after a ``:`` is a name the user chose, and cannot execute
# anything:
#
#     MATCH (i:Issue) RETURN i.commit_sha AS commit   → token 'commit' → REJECTED
#
# ``commit`` is on the forbidden list because ``COMMIT`` ends a transaction, so
# naming a column after the field you selected was enough to be told the tool is
# read-only. The same went for any alias, property or label called ``set``,
# ``create``, ``delete``, ``drop`` or ``stop`` — all ordinary words in a code
# graph, where ``Issue.severity AS drop`` or a ``:Create`` label are things
# people write.
#
# Redacted only for the keyword scan, and only one identifier per match, so a
# real write following an alias (``RETURN n AS x DELETE n``) is still tokenised
# and still rejected. ``_check_call_tokens`` reads the untouched text, because
# a procedure target is dotted and redacting its dots would blind the allowlist.
_ALIAS_RE: Final = re.compile(r"\bAS\s+([A-Za-z_]\w*)", re.IGNORECASE)
_PROPERTY_RE: Final = re.compile(r"\.\s*([A-Za-z_]\w*)")
_LABEL_RE: Final = re.compile(r":\s*([A-Za-z_]\w*)")


def _redact_identifier_positions(cypher: str) -> str:
    """Blank out names that cannot be commands, for the keyword scan only."""
    out = _ALIAS_RE.sub("AS _alias", cypher)
    out = _PROPERTY_RE.sub("._prop", out)
    return _LABEL_RE.sub(":_label", out)


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
                f"Permitted: {sorted(_ALLOWED_PROCEDURES)}. "
                "`apoc.*` is not — apoc.cypher.run executes its string argument, "
                "which is how a write gets past a read-only guard."
            )


def _extract_param_names(cypher: str) -> set[str]:
    """Find every ``$name`` reference in the Cypher text."""
    return set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", cypher))


def _check_keywords(cypher: str, tokens: list[str]) -> None:
    """Reject write / admin keywords and the forbidden multi-word clauses."""
    for tok in tokens:
        if tok.upper() in _FORBIDDEN_TOKENS:
            raise CypherReadOnlyViolation(
                f"Cypher token {tok!r} is forbidden: this tool is read-only."
            )
    for pattern, name in _FORBIDDEN_PHRASES:
        if pattern.search(cypher):
            raise CypherReadOnlyViolation(
                f"Cypher clause {name!r} is forbidden: this tool is read-only, "
                "and LOAD CSV also makes the database host fetch a URL of the "
                "query's choosing."
            )


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
    # One pass produces both views. ``without_comments`` is what we return and
    # what runs; its string literals are untouched, so a query searching for
    # ``http://`` is executed as written rather than truncated at the ``//``.
    lexed = _lex(cypher)
    stripped = lexed.without_comments.strip()
    # Scan against the literal-blanked form so a literal's contents cannot
    # decide the verdict; return the real query below.
    scannable = lexed.scannable.strip()
    # Tokenize once and reuse across checks. The keyword scan gets its own
    # stream with identifier positions blanked — an alias or property named
    # ``commit`` is not a transaction command. Everything after it reads the
    # unredacted form: ``_check_call_tokens`` needs the dots in a procedure
    # target, and ``_check_multi_statement`` needs the real opening word.
    tokens = _tokenize(scannable)
    _check_keywords(scannable, _tokenize(_redact_identifier_positions(scannable)))
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
