"""The codeindex eval's :class:`FixtureRepo` declaration.

Kept separate from ``build_jsonl.py`` so the spec is human-readable
and the builder stays content-free. If you want a different fixture
shape, change this file; the builder doesn't need to change.

Each entry's ``content`` mirrors the LLM-produced output shape from
the server pipeline:

- file content uses :class:`FileSummary` with seven mandatory sections
  + optional recommendations (matches
  ``processed_file.py::_build_final_file_summary_text``).
- entity content uses :class:`EntitySummary` with the mandatory
  ``summary`` plus four optional sections (matches
  ``processed_entity.py::_build_entity_summary_text``).

Quality fields here are the *expected outputs* for the eval — the
agent's queries should land on them. Real Python source for these
items lives at ``evals/fixtures/codeindex_repo/``.
"""

from __future__ import annotations

from evals.codeindex.build_jsonl import (
    EntitySummary,
    FileSummary,
    FixtureDocFile,
    FixtureDocSection,
    FixtureEntity,
    FixtureFile,
    FixtureRepo,
)
from ember_code.core.code_index.enums import (
    ComplexityLevel,
    DocumentationLevel,
    IssuesSeverity,
    PerformanceLevel,
    PriorityLevel,
    QualityLevel,
    SecurityLevel,
    TechnicalDebtLevel,
    TestabilityLevel,
    TestingLevel,
)


def fixture() -> FixtureRepo:
    """Return the canonical fixture spec for the codeindex eval."""
    return FixtureRepo(
        folders=[
            "src",
            "src/auth",
            "src/db",
            "src/cache",
            "src/factories",
            "src/legacy",
            "src/utils",
            "src/api",
            "src/web",
            "src/config",
            "src/algorithms",
            "docs",
        ],
        files=[
            # ── Critical security: SQL injection ─────────────────────────
            FixtureFile(
                path="src/auth/login.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "Authentication entry point for the application. "
                        "Verifies usernames + passwords against the users "
                        "table and resolves admin role."
                    ),
                    architecture_and_design=(
                        "Two thin functions wrapping raw SQL calls into "
                        "src/db/queries.py. No abstraction layer between "
                        "the request and the query string."
                    ),
                    code_quality=(
                        "Quality is poor. The same anti-pattern repeats in "
                        "both functions; both should be using parameterized "
                        "queries through a proper ORM or query builder."
                    ),
                    security=(
                        "Critical SQL-injection vulnerability. The username, "
                        "password, and user_id values are concatenated directly "
                        "into the SQL string. Any caller-supplied input ends up "
                        "executed verbatim. Treat as a P0."
                    ),
                    issues_and_technical_debt=(
                        "SQL injection in authenticate() and is_admin(). No "
                        "input validation, no parameterization, no audit log. "
                        "Refactor to use bound parameters before this ships."
                    ),
                    testing_and_reliability=(
                        "Untested. No unit tests, no integration coverage. The "
                        "raw-SQL path bypasses anything we'd normally exercise."
                    ),
                    dependencies_and_impact=(
                        "Imports from src/db/queries.py (run_raw). Called by "
                        "src/api/users.py::handle_login. A fix here cascades "
                        "into both files."
                    ),
                    recommendations=(
                        "Replace string interpolation with parameterized "
                        "queries. Add input length / character class validation."
                    ),
                ),
                quality=QualityLevel.POOR,
                complexity=ComplexityLevel.LOW,
                security=SecurityLevel.CRITICAL,
                testing=TestingLevel.UNTESTED,
                testability=TestabilityLevel.MODERATE,
                documentation=DocumentationLevel.MINIMAL,
                performance=PerformanceLevel.ACCEPTABLE,
                issues=IssuesSeverity.SEVERE,
                priority=PriorityLevel.CRITICAL,
                vulnerabilities=["sql-injection"],
                domain=["auth"],
                concerns=["security"],
                imports=["src/db/queries.py"],
                entities=[
                    FixtureEntity(
                        name="authenticate",
                        entity_type="function",
                        line_from=14,
                        line_to=22,
                        content=EntitySummary(
                            summary=(
                                "authenticate(username, password) — runs an "
                                "unsafe raw SQL query and returns the user "
                                "record on match."
                            ),
                            quality_assessment=(
                                "Poor. String interpolation directly into SQL "
                                "is a well-known anti-pattern."
                            ),
                            security_analysis=(
                                "SQL-injection vulnerability via the username "
                                "and password parameters. Trivially exploitable."
                            ),
                            issues_and_concerns=(
                                "Severe security bug; do not ship without "
                                "fixing."
                            ),
                            testing_status="Untested.",
                        ),
                        quality=QualityLevel.POOR,
                        security=SecurityLevel.CRITICAL,
                        complexity=ComplexityLevel.LOW,
                        testing=TestingLevel.UNTESTED,
                        issues=IssuesSeverity.SEVERE,
                        vulnerabilities=["sql-injection"],
                        domain=["auth"],
                        calls=["src/db/queries.py::run_raw"],
                    ),
                    FixtureEntity(
                        name="is_admin",
                        entity_type="function",
                        line_from=25,
                        line_to=29,
                        content=EntitySummary(
                            summary=(
                                "is_admin(user_id) — same SQL injection "
                                "pattern. Concatenates user_id into a raw "
                                "query."
                            ),
                            security_analysis=(
                                "SQL-injection vulnerability via user_id."
                            ),
                            testing_status="Untested.",
                        ),
                        quality=QualityLevel.POOR,
                        security=SecurityLevel.CRITICAL,
                        complexity=ComplexityLevel.LOW,
                        testing=TestingLevel.UNTESTED,
                        issues=IssuesSeverity.SEVERE,
                        vulnerabilities=["sql-injection"],
                        domain=["auth"],
                        calls=["src/db/queries.py::run_raw"],
                    ),
                ],
            ),
            # ── Major security: token leak in error message ─────────────
            FixtureFile(
                path="src/auth/session.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "Manages session lifecycle: store, resolve, and "
                        "revoke session tokens. Backed by an in-memory LRU "
                        "cache."
                    ),
                    architecture_and_design=(
                        "Three small functions wrapping a module-level "
                        "LRUCache instance. Tokens are opaque strings."
                    ),
                    code_quality=(
                        "Mostly fine. The error path on resolve_session "
                        "echoes the raw token in the exception message — "
                        "that's the smell."
                    ),
                    security=(
                        "Token-leak vulnerability: resolve_session raises a "
                        "ValueError that includes the unknown token. Any "
                        "logger or error handler that records exception "
                        "messages will capture credentials."
                    ),
                    issues_and_technical_debt=(
                        "Exception message must not echo session tokens. "
                        "Either log the token elsewhere with redaction, or "
                        "drop it from the message entirely."
                    ),
                    testing_and_reliability=(
                        "Partially tested — happy paths only. The error "
                        "path's leak isn't exercised."
                    ),
                    dependencies_and_impact=(
                        "Imports from src/cache/lru.py. No callers in the "
                        "indexed surface today, but the API layer is "
                        "expected to wire this in."
                    ),
                ),
                quality=QualityLevel.FAIR,
                complexity=ComplexityLevel.LOW,
                security=SecurityLevel.MAJOR_ISSUES,
                testing=TestingLevel.PARTIALLY_TESTED,
                testability=TestabilityLevel.EASY,
                documentation=DocumentationLevel.GOOD,
                performance=PerformanceLevel.OPTIMIZED,
                issues=IssuesSeverity.MODERATE,
                priority=PriorityLevel.HIGH,
                vulnerabilities=["token-leak"],
                domain=["auth"],
                concerns=["security"],
                imports=["src/cache/lru.py"],
                entities=[
                    FixtureEntity(
                        name="store_session",
                        entity_type="function",
                        line_from=11,
                        line_to=12,
                        content=EntitySummary(
                            summary="store_session(token, user_id) — caches the session.",
                        ),
                        quality=QualityLevel.GOOD,
                        domain=["auth"],
                    ),
                    FixtureEntity(
                        name="resolve_session",
                        entity_type="function",
                        line_from=15,
                        line_to=20,
                        content=EntitySummary(
                            summary=(
                                "resolve_session(token) — looks up the user_id."
                            ),
                            security_analysis=(
                                "Raises a ValueError that echoes the unknown "
                                "token verbatim. Token leak when error "
                                "messages are logged or returned to clients."
                            ),
                            issues_and_concerns=(
                                "Sanitize the exception message before raising."
                            ),
                        ),
                        quality=QualityLevel.FAIR,
                        security=SecurityLevel.MAJOR_ISSUES,
                        issues=IssuesSeverity.MODERATE,
                        vulnerabilities=["token-leak"],
                        domain=["auth"],
                    ),
                    FixtureEntity(
                        name="revoke_session",
                        entity_type="function",
                        line_from=23,
                        line_to=24,
                        content=EntitySummary(
                            summary="revoke_session(token) — drops a session from the cache.",
                        ),
                        quality=QualityLevel.GOOD,
                        domain=["auth"],
                    ),
                ],
            ),
            # ── Data-access layer: N+1 query smell ──────────────────────
            FixtureFile(
                path="src/db/queries.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "Data-access layer: a thin run_raw helper plus two "
                        "specific queries (list_users_with_orders, get_user)."
                    ),
                    architecture_and_design=(
                        "No ORM, no query builder — direct SQL strings. The "
                        "row shape is whatever the connector returns."
                    ),
                    code_quality=(
                        "Poor. list_users_with_orders is a textbook N+1: "
                        "one round-trip to fetch users, then one extra "
                        "query per user for orders."
                    ),
                    security=(
                        "Minor concerns: get_user concatenates user_id "
                        "into the SQL string. Same anti-pattern as the auth "
                        "layer, but the typical caller is internal."
                    ),
                    issues_and_technical_debt=(
                        "N+1 in list_users_with_orders. Replace with a "
                        "JOIN. Add parameter binding to get_user."
                    ),
                    testing_and_reliability="Untested at the unit level.",
                    dependencies_and_impact=(
                        "Used by src/auth/login.py (raw SQL) and "
                        "src/api/users.py (listing screens). Performance "
                        "bug here cascades to user-facing latency."
                    ),
                    recommendations=(
                        "Single SELECT with a LEFT JOIN against orders, "
                        "GROUP BY user. Will close the N+1 entirely."
                    ),
                ),
                quality=QualityLevel.POOR,
                complexity=ComplexityLevel.MEDIUM,
                security=SecurityLevel.MINOR_ISSUES,
                testing=TestingLevel.UNTESTED,
                testability=TestabilityLevel.DIFFICULT,
                documentation=DocumentationLevel.MINIMAL,
                performance=PerformanceLevel.INEFFICIENT,
                issues=IssuesSeverity.MODERATE,
                technical_debt=TechnicalDebtLevel.HIGH,
                needs_refactoring=True,
                priority=PriorityLevel.HIGH,
                file_issues=["n+1-query"],
                layers=["data-access"],
                domain=["db"],
                concerns=["performance"],
                entities=[
                    FixtureEntity(
                        name="run_raw",
                        entity_type="function",
                        line_from=10,
                        line_to=12,
                        content=EntitySummary(
                            summary="run_raw(sql) — execute arbitrary SQL. Returns rows.",
                        ),
                        quality=QualityLevel.FAIR,
                        domain=["db"],
                    ),
                    FixtureEntity(
                        name="list_users_with_orders",
                        entity_type="function",
                        line_from=15,
                        line_to=23,
                        content=EntitySummary(
                            summary=(
                                "list_users_with_orders() — fetch users and "
                                "their order counts."
                            ),
                            quality_assessment=(
                                "Poor. The implementation runs an extra "
                                "round-trip per user."
                            ),
                            issues_and_concerns=(
                                "N+1 query. Single JOIN would replace the "
                                "loop."
                            ),
                        ),
                        quality=QualityLevel.POOR,
                        complexity=ComplexityLevel.MEDIUM,
                        performance=PerformanceLevel.INEFFICIENT,
                        issues=IssuesSeverity.MODERATE,
                        file_issues=["n+1-query"],
                        domain=["db"],
                        calls=["src/db/queries.py::run_raw"],
                    ),
                    FixtureEntity(
                        name="get_user",
                        entity_type="function",
                        line_from=26,
                        line_to=28,
                        content=EntitySummary(
                            summary="get_user(user_id) — fetch a single user record.",
                        ),
                        quality=QualityLevel.FAIR,
                        domain=["db"],
                        calls=["src/db/queries.py::run_raw"],
                    ),
                ],
            ),
            # ── Gold-standard utility ────────────────────────────────────
            FixtureFile(
                path="src/cache/lru.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "A least-recently-used cache backed by OrderedDict. "
                        "Provides get / put / evict with a fixed capacity."
                    ),
                    architecture_and_design=(
                        "Single class, no inheritance, no globals. Capacity "
                        "is set at construction; eviction is automatic on "
                        "put when over the cap."
                    ),
                    code_quality=(
                        "Excellent. Small surface, predictable big-O, no "
                        "dependencies beyond the standard library."
                    ),
                    security=(
                        "No security concerns — pure in-memory data structure."
                    ),
                    issues_and_technical_debt="None.",
                    testing_and_reliability=(
                        "Well tested. Covers eviction order, capacity = 0 "
                        "rejection, and re-insertion behavior."
                    ),
                    dependencies_and_impact=(
                        "Imported by src/auth/session.py. Lightweight "
                        "enough that adopters don't need to think about it."
                    ),
                ),
                quality=QualityLevel.EXCELLENT,
                complexity=ComplexityLevel.LOW,
                security=SecurityLevel.SECURE,
                testing=TestingLevel.WELL_TESTED,
                testability=TestabilityLevel.EASY,
                documentation=DocumentationLevel.GOOD,
                performance=PerformanceLevel.OPTIMIZED,
                issues=IssuesSeverity.NONE,
                technical_debt=TechnicalDebtLevel.NONE,
                priority=PriorityLevel.LOW,
                patterns=["lru"],
                domain=["cache"],
                entities=[
                    FixtureEntity(
                        name="LRUCache",
                        entity_type="class",
                        line_from=14,
                        line_to=42,
                        content=EntitySummary(
                            summary=(
                                "LRUCache — least-recently-used cache class "
                                "with get/put/evict. Constant-time operations "
                                "via OrderedDict."
                            ),
                            quality_assessment=(
                                "Excellent — small, focused, well-tested."
                            ),
                            testing_status=(
                                "Comprehensive tests covering eviction order "
                                "and capacity edge cases."
                            ),
                        ),
                        quality=QualityLevel.EXCELLENT,
                        complexity=ComplexityLevel.LOW,
                        testing=TestingLevel.WELL_TESTED,
                        patterns=["lru"],
                        domain=["cache"],
                    ),
                ],
            ),
            # ── Factory pattern ─────────────────────────────────────────
            FixtureFile(
                path="src/factories/widget.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "Widget factory that dispatches to concrete Widget "
                        "subclasses (GearWidget, ScrewWidget) by ``kind``."
                    ),
                    architecture_and_design=(
                        "Plain functions and dataclasses. The factory is a "
                        "switch over kind keys, not a registry."
                    ),
                    code_quality=(
                        "Good. Straightforward, easy to extend by adding "
                        "another branch."
                    ),
                    security="No external input crosses this boundary.",
                    issues_and_technical_debt=(
                        "Adding a new kind requires editing make_widget. A "
                        "registry would be more open/closed but isn't "
                        "necessary at this scale."
                    ),
                    testing_and_reliability="Partially tested.",
                    dependencies_and_impact="Self-contained.",
                ),
                quality=QualityLevel.GOOD,
                complexity=ComplexityLevel.LOW,
                testing=TestingLevel.PARTIALLY_TESTED,
                testability=TestabilityLevel.EASY,
                documentation=DocumentationLevel.MINIMAL,
                patterns=["factory"],
                domain=["widgets"],
                entities=[
                    FixtureEntity(
                        name="make_widget",
                        entity_type="function",
                        line_from=27,
                        line_to=33,
                        content=EntitySummary(
                            summary=(
                                "make_widget(kind, **kwargs) — factory "
                                "function producing concrete Widget "
                                "subclasses by kind."
                            ),
                        ),
                        quality=QualityLevel.GOOD,
                        patterns=["factory"],
                        domain=["widgets"],
                    ),
                ],
            ),
            # ── Refactor candidate: legacy parser ───────────────────────
            FixtureFile(
                path="src/legacy/parser.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "Tokenizes and evaluates a tiny arithmetic "
                        "expression language with +, -, *, /, parentheses."
                    ),
                    architecture_and_design=(
                        "One function with deeply-nested closures sharing "
                        "a non-local position cursor. Tokenization, "
                        "parsing, and evaluation are all interleaved."
                    ),
                    code_quality=(
                        "Poor. Very-high cognitive complexity. Splitting "
                        "the lexer and parser is the obvious first step."
                    ),
                    security=(
                        "Minor concerns. The expression input itself is "
                        "trusted today, but if exposed to user input the "
                        "integer overflow + division-by-zero handling "
                        "needs hardening."
                    ),
                    issues_and_technical_debt=(
                        "Top refactor candidate. Untestable at the "
                        "function level — too many internal states. "
                        "Breaking out the tokenizer would unblock testing."
                    ),
                    testing_and_reliability=(
                        "Untested. The function's complexity discourages "
                        "even adding tests."
                    ),
                    dependencies_and_impact=(
                        "No internal dependencies. Refactoring this is "
                        "low-risk for the rest of the codebase."
                    ),
                    recommendations=(
                        "Split into a Tokenizer class + a recursive-descent "
                        "Parser class. Add unit tests for each level "
                        "before changing behavior."
                    ),
                ),
                quality=QualityLevel.POOR,
                complexity=ComplexityLevel.VERY_HIGH,
                security=SecurityLevel.MINOR_ISSUES,
                testing=TestingLevel.UNTESTED,
                testability=TestabilityLevel.DIFFICULT,
                documentation=DocumentationLevel.MINIMAL,
                technical_debt=TechnicalDebtLevel.HIGH,
                needs_refactoring=True,
                priority=PriorityLevel.HIGH,
                domain=["parser"],
                concerns=["maintainability"],
                entities=[
                    FixtureEntity(
                        name="parse_and_eval",
                        entity_type="function",
                        line_from=10,
                        line_to=110,
                        content=EntitySummary(
                            summary=(
                                "parse_and_eval(expr) — tokenize, parse, "
                                "and evaluate a tiny arithmetic expression. "
                                "Contains nested closures sharing a "
                                "non-local cursor."
                            ),
                            quality_assessment=(
                                "Very-high complexity. Top refactor "
                                "candidate."
                            ),
                            issues_and_concerns=(
                                "Untestable as written; must be split "
                                "before changes."
                            ),
                            testing_status="No tests.",
                        ),
                        quality=QualityLevel.POOR,
                        complexity=ComplexityLevel.VERY_HIGH,
                        testing=TestingLevel.UNTESTED,
                        testability=TestabilityLevel.DIFFICULT,
                        needs_refactoring=True,
                        priority=PriorityLevel.HIGH,
                        domain=["parser"],
                    ),
                ],
            ),
            # ── Well-tested utilities ───────────────────────────────────
            FixtureFile(
                path="src/utils/strings.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "Lightweight string helpers: slugify and truncate."
                    ),
                    architecture_and_design=(
                        "Two pure functions, no shared state."
                    ),
                    code_quality=(
                        "Good. Idiomatic Python, no surprises."
                    ),
                    security="Pure data transforms.",
                    issues_and_technical_debt="None.",
                    testing_and_reliability=(
                        "Well tested — both functions have unit tests "
                        "covering edge cases."
                    ),
                    dependencies_and_impact=(
                        "Used by src/api/users.py for slugifying user "
                        "names."
                    ),
                ),
                quality=QualityLevel.GOOD,
                complexity=ComplexityLevel.LOW,
                testing=TestingLevel.WELL_TESTED,
                testability=TestabilityLevel.EASY,
                documentation=DocumentationLevel.GOOD,
                domain=["utils"],
                entities=[
                    FixtureEntity(
                        name="slugify",
                        entity_type="function",
                        line_from=8,
                        line_to=15,
                        content=EntitySummary(
                            summary="slugify(text) — lowercase + hyphenate + drop non-alnum.",
                            testing_status="Well tested.",
                        ),
                        quality=QualityLevel.GOOD,
                        testing=TestingLevel.WELL_TESTED,
                        domain=["utils"],
                    ),
                    FixtureEntity(
                        name="truncate",
                        entity_type="function",
                        line_from=18,
                        line_to=24,
                        content=EntitySummary(
                            summary=(
                                "truncate(text, limit, suffix='...') — cap "
                                "text at limit, appending suffix when cut."
                            ),
                            testing_status="Well tested.",
                        ),
                        quality=QualityLevel.GOOD,
                        testing=TestingLevel.WELL_TESTED,
                        domain=["utils"],
                    ),
                ],
            ),
            # ── Path traversal vulnerability ────────────────────────────
            FixtureFile(
                path="src/web/upload.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "File upload and download handlers. Joins a "
                        "caller-supplied filename onto the upload "
                        "directory and writes / reads bytes."
                    ),
                    architecture_and_design=(
                        "Two thin functions wrapping pathlib.Path "
                        "operations against a fixed UPLOAD_DIR root."
                    ),
                    code_quality=(
                        "Poor. The path-handling step is missing — there "
                        "is no normalization, containment, or rejection of "
                        "absolute paths or '..' segments."
                    ),
                    security=(
                        "Critical path-traversal vulnerability. A caller "
                        "passing '../../etc/passwd' escapes UPLOAD_DIR. "
                        "Any process with write access to disk can be "
                        "leveraged into arbitrary file write."
                    ),
                    issues_and_technical_debt=(
                        "Resolve target with Path.resolve() and assert it "
                        "is_relative_to(UPLOAD_DIR.resolve()) before any "
                        "I/O. Reject anything else with a 400."
                    ),
                    testing_and_reliability=(
                        "Untested. Treat the missing tests as part of the "
                        "vulnerability — there's no proof the fix landed."
                    ),
                    dependencies_and_impact=(
                        "No internal callers in the indexed surface. "
                        "Likely entry point for an HTTP handler we haven't "
                        "wired up yet."
                    ),
                    recommendations=(
                        "Add a containment check; add a test asserting "
                        "'../etc/passwd' is rejected; add the same test "
                        "for the read path."
                    ),
                ),
                quality=QualityLevel.POOR,
                complexity=ComplexityLevel.LOW,
                security=SecurityLevel.CRITICAL,
                testing=TestingLevel.UNTESTED,
                testability=TestabilityLevel.MODERATE,
                documentation=DocumentationLevel.MINIMAL,
                issues=IssuesSeverity.SEVERE,
                priority=PriorityLevel.CRITICAL,
                vulnerabilities=["path-traversal"],
                domain=["web", "uploads"],
                concerns=["security"],
                layers=["presentation"],
                entities=[
                    FixtureEntity(
                        name="save_upload",
                        entity_type="function",
                        line_from=14,
                        line_to=22,
                        content=EntitySummary(
                            summary=(
                                "save_upload(filename, body) — joins "
                                "filename onto UPLOAD_DIR and writes the "
                                "bytes."
                            ),
                            security_analysis=(
                                "Path traversal. Caller-supplied filename "
                                "can escape UPLOAD_DIR via '..' segments."
                            ),
                            issues_and_concerns=(
                                "Missing containment check before writing."
                            ),
                        ),
                        quality=QualityLevel.POOR,
                        security=SecurityLevel.CRITICAL,
                        issues=IssuesSeverity.SEVERE,
                        vulnerabilities=["path-traversal"],
                        domain=["web", "uploads"],
                    ),
                    FixtureEntity(
                        name="read_upload",
                        entity_type="function",
                        line_from=25,
                        line_to=27,
                        content=EntitySummary(
                            summary=(
                                "read_upload(filename) — reads bytes from "
                                "UPLOAD_DIR/filename."
                            ),
                            security_analysis=(
                                "Symmetric path traversal on the read side."
                            ),
                        ),
                        quality=QualityLevel.POOR,
                        security=SecurityLevel.CRITICAL,
                        issues=IssuesSeverity.SEVERE,
                        vulnerabilities=["path-traversal"],
                        domain=["web", "uploads"],
                    ),
                ],
            ),
            # ── XSS via Jinja-flavored rendering ────────────────────────
            FixtureFile(
                path="src/web/render.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "Renders HTML responses for welcome and "
                        "search-results screens. Substitutes user input "
                        "into a template via str.format / f-strings."
                    ),
                    architecture_and_design=(
                        "Two render functions. The template is a constant "
                        "string with placeholders; no autoescape layer "
                        "between user input and the rendered output."
                    ),
                    code_quality=(
                        "Fair. The functions themselves are readable but "
                        "they bypass the templating engine's autoescape."
                    ),
                    security=(
                        "Cross-site scripting (XSS). User-supplied name "
                        "and query values are concatenated into HTML "
                        "without escaping. Trivially exploitable via a "
                        "<script> tag in the query string."
                    ),
                    issues_and_technical_debt=(
                        "Switch to Jinja2 with autoescape enabled, or "
                        "wrap each user value in html.escape() before "
                        "interpolation."
                    ),
                    testing_and_reliability=(
                        "Partially tested — happy paths, no XSS regression "
                        "tests."
                    ),
                    dependencies_and_impact=(
                        "Presentation layer; rendered HTML reaches the "
                        "user's browser directly."
                    ),
                    recommendations=(
                        "Adopt Jinja2 with autoescape=True and ship a "
                        "regression test that asserts <script> is "
                        "rendered as &lt;script&gt;."
                    ),
                ),
                quality=QualityLevel.FAIR,
                complexity=ComplexityLevel.LOW,
                security=SecurityLevel.MAJOR_ISSUES,
                testing=TestingLevel.PARTIALLY_TESTED,
                testability=TestabilityLevel.EASY,
                documentation=DocumentationLevel.MINIMAL,
                issues=IssuesSeverity.MODERATE,
                priority=PriorityLevel.HIGH,
                vulnerabilities=["xss"],
                frameworks=["jinja2"],
                domain=["web", "templates"],
                concerns=["security"],
                layers=["presentation"],
                entities=[
                    FixtureEntity(
                        name="render_welcome",
                        entity_type="function",
                        line_from=18,
                        line_to=20,
                        content=EntitySummary(
                            summary=(
                                "render_welcome(name, query) — interpolates "
                                "name and last query into the welcome HTML."
                            ),
                            security_analysis=(
                                "XSS via unescaped name and query values."
                            ),
                        ),
                        quality=QualityLevel.FAIR,
                        security=SecurityLevel.MAJOR_ISSUES,
                        vulnerabilities=["xss"],
                        frameworks=["jinja2"],
                        domain=["web"],
                    ),
                    FixtureEntity(
                        name="render_search_results",
                        entity_type="function",
                        line_from=23,
                        line_to=26,
                        content=EntitySummary(
                            summary=(
                                "render_search_results(query, results) — "
                                "renders a results list."
                            ),
                            security_analysis=(
                                "XSS hazard for both the query string and "
                                "each result item."
                            ),
                        ),
                        quality=QualityLevel.FAIR,
                        security=SecurityLevel.MAJOR_ISSUES,
                        vulnerabilities=["xss"],
                        frameworks=["jinja2"],
                        domain=["web"],
                    ),
                ],
            ),
            # ── Singleton anti-pattern: process-wide settings ──────────
            FixtureFile(
                path="src/config/settings.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "Application settings loaded from environment "
                        "variables at import time and exposed as a "
                        "module-level singleton (SETTINGS)."
                    ),
                    architecture_and_design=(
                        "_Settings class plus a SETTINGS instance plus a "
                        "reload() helper for tests. Singleton pattern."
                    ),
                    code_quality=(
                        "Fair. Works for read-mostly config but couples "
                        "every consumer to a global. Tests need to "
                        "monkey-patch the module to override values."
                    ),
                    security="No external surface.",
                    issues_and_technical_debt=(
                        "Testability is poor — the existence of reload() "
                        "is a tell. Direct dependency injection would "
                        "remove the need for it."
                    ),
                    testing_and_reliability="Partially tested.",
                    dependencies_and_impact=(
                        "Imported across the codebase. Migration to DI "
                        "would touch many call sites at once."
                    ),
                    recommendations=(
                        "Introduce a Settings parameter at the boundary "
                        "of each subsystem; deprecate the singleton."
                    ),
                ),
                quality=QualityLevel.FAIR,
                complexity=ComplexityLevel.LOW,
                security=SecurityLevel.SECURE,
                testing=TestingLevel.PARTIALLY_TESTED,
                testability=TestabilityLevel.DIFFICULT,
                documentation=DocumentationLevel.GOOD,
                technical_debt=TechnicalDebtLevel.MEDIUM,
                priority=PriorityLevel.LOW,
                patterns=["singleton"],
                domain=["config"],
                concerns=["testability"],
                entities=[
                    FixtureEntity(
                        name="_Settings",
                        entity_type="class",
                        line_from=14,
                        line_to=23,
                        content=EntitySummary(
                            summary=(
                                "_Settings — env-driven config holder. "
                                "Constructed once at module import, "
                                "exported as SETTINGS."
                            ),
                            quality_assessment=(
                                "Singleton. Couples consumers to a global."
                            ),
                        ),
                        quality=QualityLevel.FAIR,
                        patterns=["singleton"],
                        testability=TestabilityLevel.DIFFICULT,
                        domain=["config"],
                    ),
                    FixtureEntity(
                        name="reload",
                        entity_type="function",
                        line_from=29,
                        line_to=37,
                        content=EntitySummary(
                            summary=(
                                "reload() — re-reads env into the singleton."
                            ),
                            issues_and_concerns=(
                                "Test-support hatch. Its existence is the "
                                "smell — DI would remove the need."
                            ),
                        ),
                        quality=QualityLevel.FAIR,
                        domain=["config"],
                    ),
                ],
            ),
            # ── Genuinely complex but well-built algorithm ─────────────
            FixtureFile(
                path="src/algorithms/graph.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "Computes shortest paths on weighted directed "
                        "graphs via Dijkstra's algorithm."
                    ),
                    architecture_and_design=(
                        "Public shortest_path() drives a min-heap of "
                        "(distance, node) pairs and relaxes outgoing "
                        "edges. _reconstruct() walks the predecessor "
                        "map to materialize the resulting path."
                    ),
                    code_quality=(
                        "Good. Genuinely intricate — heap-based "
                        "relaxation isn't trivial — but the structure is "
                        "clean and the helper is properly extracted."
                    ),
                    security="Algorithmic; no external boundary.",
                    issues_and_technical_debt=(
                        "Negative-weight rejection happens late (mid-walk) "
                        "rather than upfront. Minor."
                    ),
                    testing_and_reliability=(
                        "Well tested. Covers reachable / unreachable / "
                        "negative-weight rejection paths."
                    ),
                    dependencies_and_impact=(
                        "Pure utility. No external callers in the "
                        "indexed surface today."
                    ),
                    recommendations=(
                        "Validate edge weights upfront for a clearer "
                        "error path."
                    ),
                ),
                quality=QualityLevel.GOOD,
                complexity=ComplexityLevel.HIGH,
                security=SecurityLevel.SECURE,
                testing=TestingLevel.WELL_TESTED,
                testability=TestabilityLevel.EASY,
                documentation=DocumentationLevel.GOOD,
                performance=PerformanceLevel.OPTIMIZED,
                issues=IssuesSeverity.NONE,
                technical_debt=TechnicalDebtLevel.LOW,
                priority=PriorityLevel.LOW,
                patterns=["dijkstra"],
                domain=["algorithms", "graph"],
                keywords=["shortest-path"],
                entities=[
                    FixtureEntity(
                        name="shortest_path",
                        entity_type="function",
                        line_from=18,
                        line_to=51,
                        content=EntitySummary(
                            summary=(
                                "shortest_path(graph, source, target) — "
                                "Dijkstra with a priority-queue, returns "
                                "(weight, path) or None. Rejects negative "
                                "weights with ValueError."
                            ),
                            quality_assessment=(
                                "High complexity but well-structured."
                            ),
                            testing_status="Well tested.",
                        ),
                        quality=QualityLevel.GOOD,
                        complexity=ComplexityLevel.HIGH,
                        testing=TestingLevel.WELL_TESTED,
                        patterns=["dijkstra"],
                        domain=["algorithms"],
                        calls=["src/algorithms/graph.py::_reconstruct"],
                    ),
                    FixtureEntity(
                        name="_reconstruct",
                        entity_type="function",
                        line_from=54,
                        line_to=62,
                        content=EntitySummary(
                            summary=(
                                "_reconstruct(prev, source, target) — "
                                "walks the predecessor map to produce "
                                "the path."
                            ),
                        ),
                        quality=QualityLevel.GOOD,
                        complexity=ComplexityLevel.LOW,
                        testing=TestingLevel.WELL_TESTED,
                        domain=["algorithms"],
                    ),
                ],
            ),
            # ── API layer pulling on auth + db + utils ──────────────────
            FixtureFile(
                path="src/api/users.py",
                content=FileSummary(
                    purpose_and_functionality=(
                        "User-facing handler functions for login, profile "
                        "lookup, and listing. Plain functions, no "
                        "framework adapter."
                    ),
                    architecture_and_design=(
                        "Three thin handlers that orchestrate calls into "
                        "the auth, db, and utils subsystems."
                    ),
                    code_quality=(
                        "Fair. Inherits the security debt of its "
                        "dependencies — handle_login flows directly into "
                        "the SQL-injection path."
                    ),
                    security=(
                        "Major issues by composition. The handlers don't "
                        "introduce new vulnerabilities, but they expose "
                        "the SQL injection in the auth layer to external "
                        "input."
                    ),
                    issues_and_technical_debt=(
                        "Once authenticate() is parameterized, this "
                        "module's exposure drops to baseline."
                    ),
                    testing_and_reliability="Partially tested.",
                    dependencies_and_impact=(
                        "Pulls on src/auth/login.py, src/db/queries.py, "
                        "and src/utils/strings.py."
                    ),
                ),
                quality=QualityLevel.FAIR,
                complexity=ComplexityLevel.LOW,
                security=SecurityLevel.MAJOR_ISSUES,
                testing=TestingLevel.PARTIALLY_TESTED,
                testability=TestabilityLevel.MODERATE,
                documentation=DocumentationLevel.MINIMAL,
                domain=["api", "users"],
                imports=[
                    "src/auth/login.py",
                    "src/db/queries.py",
                    "src/utils/strings.py",
                ],
                entities=[
                    FixtureEntity(
                        name="handle_login",
                        entity_type="function",
                        line_from=12,
                        line_to=17,
                        content=EntitySummary(
                            summary=(
                                "handle_login(username, password) — invokes "
                                "authenticate() and returns the user "
                                "record + admin flag."
                            ),
                        ),
                        domain=["api"],
                        calls=[
                            "src/auth/login.py::authenticate",
                            "src/auth/login.py::is_admin",
                        ],
                    ),
                    FixtureEntity(
                        name="handle_user_profile",
                        entity_type="function",
                        line_from=20,
                        line_to=25,
                        content=EntitySummary(
                            summary=(
                                "handle_user_profile(user_id) — fetches "
                                "and slugifies."
                            ),
                        ),
                        domain=["api"],
                        calls=[
                            "src/db/queries.py::get_user",
                            "src/utils/strings.py::slugify",
                        ],
                    ),
                    FixtureEntity(
                        name="handle_user_listing",
                        entity_type="function",
                        line_from=28,
                        line_to=29,
                        content=EntitySummary(
                            summary=(
                                "handle_user_listing() — returns the "
                                "N+1-affected user list."
                            ),
                        ),
                        domain=["api"],
                        calls=["src/db/queries.py::list_users_with_orders"],
                    ),
                ],
            ),
        ],
        docs=[
            FixtureDocFile(
                path="docs/AUTH.md",
                body=(
                    "# Authentication\n\n"
                    "The auth subsystem covers session lifecycle and identity checks.\n"
                ),
                sections=[
                    FixtureDocSection(
                        name="Authentication",
                        level=1,
                        line_from=1,
                        line_to=3,
                        body=(
                            "# Authentication\n\n"
                            "The auth subsystem covers session lifecycle and identity checks."
                        ),
                    ),
                    FixtureDocSection(
                        name="Login flow",
                        level=2,
                        line_from=5,
                        line_to=9,
                        body=(
                            "## Login flow\n\n"
                            "The entry point is `src/auth/login.py::authenticate`. It looks "
                            "up credentials against the `users` table and returns the user "
                            "record on match."
                        ),
                        parent_chain=["Authentication"],
                    ),
                    FixtureDocSection(
                        name="SQL safety",
                        level=3,
                        line_from=11,
                        line_to=15,
                        body=(
                            "### SQL safety\n\n"
                            "Right now the queries use string interpolation for the `name` "
                            "and `password` fields. This is a known issue — track it as "
                            "critical security technical debt."
                        ),
                        parent_chain=["Authentication", "Login flow"],
                    ),
                    FixtureDocSection(
                        name="Sessions",
                        level=2,
                        line_from=17,
                        line_to=21,
                        body=(
                            "## Sessions\n\n"
                            "Sessions live in an LRU cache (`src/cache/lru.py`). Tokens are "
                            "opaque strings; revocation simply evicts the cache entry."
                        ),
                        parent_chain=["Authentication"],
                    ),
                    FixtureDocSection(
                        name="Token leakage",
                        level=3,
                        line_from=23,
                        line_to=27,
                        body=(
                            "### Token leakage\n\n"
                            "The session resolver currently echoes the unknown token back "
                            "in the error message. That value should never reach a client."
                        ),
                        parent_chain=["Authentication", "Sessions"],
                    ),
                    FixtureDocSection(
                        name="Roles",
                        level=2,
                        line_from=29,
                        line_to=32,
                        body=(
                            "## Roles\n\n"
                            "Roles are read from the same `users` table. There's only one "
                            "non-default role today: `admin`."
                        ),
                        parent_chain=["Authentication"],
                    ),
                ],
            ),
        ],
    )
