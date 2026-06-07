"""Enums shared across the code_index package.

Quality enums mirror the server-side definitions in
``ember-server/app/dataset/dataset_creation/schemas/quality/enums.py``.
We duplicate them here (rather than depend on the server) so the
``codeindex_query`` tool can declare them in its signature — agno
turns ``StrEnum`` types into JSON schema enums on the tool, which
constrains what the LLM can pass.

If you change a value here, change it on the server side too.
"""

from __future__ import annotations

from enum import StrEnum


class FileSystemType(StrEnum):
    FOLDER = "folder"
    FILE = "file"
    ENTITY = "entity"


class Kind(StrEnum):
    CODE = "code"
    DOCS = "docs"


# ── Quality categoricals ────────────────────────────────────────────────


class QualityLevel(StrEnum):
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    UNKNOWN = "unknown"


class ComplexityLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very-high"
    UNKNOWN = "unknown"


class SecurityLevel(StrEnum):
    SECURE = "secure"
    MINOR_ISSUES = "minor-issues"
    MAJOR_ISSUES = "major-issues"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class TestingLevel(StrEnum):
    WELL_TESTED = "well-tested"
    PARTIALLY_TESTED = "partially-tested"
    UNTESTED = "untested"
    UNKNOWN = "unknown"


class TestabilityLevel(StrEnum):
    EASY = "easy"
    MODERATE = "moderate"
    DIFFICULT = "difficult"
    UNKNOWN = "unknown"


class DocumentationLevel(StrEnum):
    EXCELLENT = "excellent"
    GOOD = "good"
    MINIMAL = "minimal"
    MISSING = "missing"
    UNKNOWN = "unknown"


class PerformanceLevel(StrEnum):
    OPTIMIZED = "optimized"
    ACCEPTABLE = "acceptable"
    INEFFICIENT = "inefficient"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class IssuesSeverity(StrEnum):
    NONE = "none"
    MINOR = "minor"
    MODERATE = "moderate"
    SEVERE = "severe"
    UNKNOWN = "unknown"


class TechnicalDebtLevel(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class CohesionLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class CouplingLevel(StrEnum):
    LOOSE = "loose"
    MODERATE = "moderate"
    TIGHT = "tight"
    UNKNOWN = "unknown"


class StabilityLevel(StrEnum):
    STABLE = "stable"
    EVOLVING = "evolving"
    UNSTABLE = "unstable"
    UNKNOWN = "unknown"


class PriorityLevel(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


# ── Reference relations ─────────────────────────────────────────────────


class Relation(StrEnum):
    CALLS = "calls"
    CALLED_BY = "called_by"
    IMPORTS = "imports"
    IMPORTED_BY = "imported_by"
    EXTENDS = "extends"
    EXTENDED_BY = "extended_by"
    IMPLEMENTS = "implements"
    IMPLEMENTED_BY = "implemented_by"
    DECORATES = "decorates"
    DECORATED_BY = "decorated_by"
    TYPES_AS = "types_as"
    TYPED_BY = "typed_by"


# ── Content sections ────────────────────────────────────────────────────
#
# The indexer's LLM-summary pass writes the ``content`` field as a
# sequence of named ``[SECTION:<name>]…[/SECTION]`` markers, but the
# concrete section names differ per item type:
#
#   entity → summary, quality_assessment, security_analysis,
#            issues_and_concerns, testing_status
#   file   → purpose_and_functionality, architecture_and_design,
#            code_quality, security, issues_and_technical_debt,
#            testing_and_reliability, dependencies_and_impact,
#            recommendations, entities
#   folder → module_purpose, organization_and_structure,
#            architectural_assessment, quality_patterns,
#            security_posture, common_issues,
#            testing_and_reliability, module_health_score
#
# The ``Section`` enum below abstracts over those concrete names with
# semantic groups so the agent says "I want SECURITY context" and the
# filter resolves to ``security_analysis`` for entities, ``security``
# for files, and ``security_posture`` for folders. ``_SECTION_ALIASES``
# in ``tools/codeindex.py`` carries the group → concrete-name mapping.


class Section(StrEnum):
    SUMMARY = "summary"  # entity.summary | file.purpose_and_functionality | folder.module_purpose
    QUALITY = "quality"  # entity.quality_assessment | file.code_quality | folder.quality_patterns
    SECURITY = "security"  # entity.security_analysis | file.security | folder.security_posture
    ISSUES = "issues"  # entity.issues_and_concerns | file.issues_and_technical_debt | folder.common_issues
    TESTING = "testing"  # entity.testing_status | file/folder.testing_and_reliability
    ARCHITECTURE = "architecture"  # file.architecture_and_design | folder.organization_and_structure + architectural_assessment
    DEPENDENCIES = "dependencies"  # file.dependencies_and_impact
    RECOMMENDATIONS = "recommendations"  # file.recommendations
    HEALTH_SCORE = "health_score"  # folder.module_health_score
    ENTITIES = "entities"  # file.entities — list of entities contained in the file
