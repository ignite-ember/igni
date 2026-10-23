"""Graph infrastructure both capabilities stand on.

CodeIndex and Artifacts each keep a Neo4j store, and both are being
extracted into extensions. Neither can leave while the rules for
querying a graph safely live inside the other — and that is where
they were: ``assert_read_only_cypher`` was
``core/tools/codeindex/cypher_guard.py``, imported by Artifacts, by
the permission catalog, by the eval runner and by the prompt
fragments, and ``DEFAULT_DATABASE`` was a constant in CodeIndex's
schema module that Artifacts also read.

A safety rule used by two capabilities and by the host's own
permission system is the host's. It stays when they go.

This package holds only what is genuinely shared. Each capability's
*own* schema stays with it: CodeIndex's commit and meta DDL in
``core/code_index/neo4j_schema.py``, Artifacts' in
``core/artifacts/graph_schema.py`` — which is where
``ARTIFACT_SCHEMA_STATEMENTS`` now lives, rather than in CodeIndex's
file describing Artifacts' graph.
"""

from __future__ import annotations

from ember_code.core.graph.cypher_guard import (
    CypherGuardError,
    assert_read_only_cypher,
)

#: The Neo4j database every store in this process uses.
#:
#: One physical database; isolation is the *process*, not the
#: database name — one Neo4j process holds one commit's data, and
#: switching commits means switching process. Named here rather than
#: spelled ``"neo4j"`` at each call site, and here rather than in
#: either capability, because both of them pass it.
DEFAULT_DATABASE: str = "neo4j"

__all__ = ["DEFAULT_DATABASE", "CypherGuardError", "assert_read_only_cypher"]
