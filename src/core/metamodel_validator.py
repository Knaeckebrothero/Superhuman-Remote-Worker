"""
Metamodel Compliance Validator

Deterministic validation of Neo4j graph databases against the FINIUS metamodel.
Phase 1 of the hybrid verification pipeline - produces auditable compliance reports.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import time

from .neo4j_utils import Neo4jConnection


class Severity(Enum):
    """Severity levels for compliance check results."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class CheckResult:
    """Result of a single compliance check."""
    check_id: str
    check_name: str
    passed: bool
    severity: Severity
    message: str
    violations: list[dict] = field(default_factory=list)
    query_used: str = ""
    execution_time_ms: float = 0.0


@dataclass
class ComplianceReport:
    """Complete compliance report from Phase 1 validation."""
    timestamp: datetime
    passed: bool
    error_count: int
    warning_count: int
    checks_performed: list[str]
    results: list[CheckResult]

    def to_dict(self) -> dict:
        """Serialize for JSON storage/transmission."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "passed": self.passed,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "checks_performed": self.checks_performed,
            "results": [
                {
                    "check_id": r.check_id,
                    "check_name": r.check_name,
                    "passed": r.passed,
                    "severity": r.severity.value,
                    "message": r.message,
                    "violations": r.violations,
                    "query_used": r.query_used,
                    "execution_time_ms": r.execution_time_ms,
                }
                for r in self.results
            ],
        }

    def format_summary(self) -> str:
        """Format a human-readable summary of the report."""
        status = "PASSED" if self.passed else "FAILED"
        lines = [
            f"Metamodel Compliance Report",
            f"=" * 40,
            f"Timestamp: {self.timestamp.isoformat()}",
            f"Status: {status}",
            f"Errors: {self.error_count}",
            f"Warnings: {self.warning_count}",
            "",
        ]

        if not self.passed or self.warning_count > 0:
            lines.append("Findings:")
            for result in self.results:
                if not result.passed:
                    severity_icon = "✗" if result.severity == Severity.ERROR else "⚠"
                    lines.append(f"  {severity_icon} [{result.check_id}] {result.message}")
                    for violation in result.violations[:3]:  # Show first 3
                        lines.append(f"      → {violation}")
                    if len(result.violations) > 3:
                        lines.append(f"      ... and {len(result.violations) - 3} more")

        return "\n".join(lines)


class MetamodelValidator:
    """
    Deterministic metamodel compliance validator.

    Executes Cypher-based checks against a Neo4j database to verify
    conformance with the FINIUS metamodel specification.
    """

    # Metamodel Definition
    ALLOWED_NODE_LABELS = {"Requirement", "BusinessObject", "Message"}

    REQUIRED_PROPERTIES = {
        "Requirement": ["rid"],
        "BusinessObject": ["boid"],
        "Message": ["mid"],
    }

    ALLOWED_RELATIONSHIPS = {
        # (source_label, relationship_type, target_label)
        ("Requirement", "REFINES", "Requirement"),
        ("Requirement", "DEPENDS_ON", "Requirement"),
        ("Requirement", "TRACES_TO", "Requirement"),
        ("Requirement", "RELATES_TO_OBJECT", "BusinessObject"),
        ("Requirement", "IMPACTS_OBJECT", "BusinessObject"),
        ("Requirement", "RELATES_TO_MESSAGE", "Message"),
        ("Requirement", "IMPACTS_MESSAGE", "Message"),
        ("Message", "USES_OBJECT", "BusinessObject"),
        ("Message", "PRODUCES_OBJECT", "BusinessObject"),
    }

    ALLOWED_RELATIONSHIP_TYPES = {rel[1] for rel in ALLOWED_RELATIONSHIPS}

    def __init__(self, connection: Neo4jConnection):
        """
        Initialize validator with a Neo4j connection.

        Args:
            connection: Connected Neo4jConnection instance
        """
        self.connection = connection

    def _execute_check(self, query: str, parameters: Optional[dict] = None) -> tuple[list[dict], float]:
        """Execute a query and return results with timing."""
        start = time.time()
        results = self.connection.execute_query(query, parameters)
        elapsed_ms = (time.time() - start) * 1000
        return results, elapsed_ms

    # =========================================================================
    # Category A: Structural Constraints (Severity: ERROR)
    # =========================================================================

    def check_a1_node_labels(self) -> CheckResult:
        """Verify all nodes have allowed labels."""
        query = """
        MATCH (n)
        WITH n, labels(n) AS node_labels
        WHERE NONE(label IN node_labels WHERE label IN $allowed_labels)
        RETURN labels(n) AS invalid_labels, count(n) AS count
        """
        results, elapsed = self._execute_check(
            query, {"allowed_labels": list(self.ALLOWED_NODE_LABELS)}
        )

        violations = [
            {"labels": r["invalid_labels"], "count": r["count"]}
            for r in results
        ]
        passed = len(violations) == 0

        return CheckResult(
            check_id="A1",
            check_name="validate_node_labels",
            passed=passed,
            severity=Severity.ERROR,
            message="All nodes have valid labels" if passed else f"Found {sum(v['count'] for v in violations)} nodes with invalid labels",
            violations=violations,
            query_used=query.strip(),
            execution_time_ms=elapsed,
        )

    def check_a2_unique_constraints(self) -> CheckResult:
        """Verify ID properties are unique within their label."""
        violations = []

        for label, props in self.REQUIRED_PROPERTIES.items():
            id_prop = props[0]  # First property is the ID
            query = f"""
            MATCH (n:{label})
            WITH n.{id_prop} AS id, count(*) AS cnt
            WHERE cnt > 1
            RETURN id, cnt
            """
            results, _ = self._execute_check(query)
            for r in results:
                violations.append({
                    "label": label,
                    "property": id_prop,
                    "value": r["id"],
                    "count": r["cnt"],
                })

        passed = len(violations) == 0

        return CheckResult(
            check_id="A2",
            check_name="validate_unique_constraints",
            passed=passed,
            severity=Severity.ERROR,
            message="All ID properties are unique" if passed else f"Found {len(violations)} duplicate ID values",
            violations=violations,
            query_used="Multiple queries for each label/ID combination",
            execution_time_ms=0.0,
        )

    def check_a3_required_properties(self) -> CheckResult:
        """Verify each node type has its required ID property."""
        violations = []

        for label, props in self.REQUIRED_PROPERTIES.items():
            id_prop = props[0]
            query = f"""
            MATCH (n:{label})
            WHERE n.{id_prop} IS NULL
            RETURN count(n) AS count
            """
            results, _ = self._execute_check(query)
            count = results[0]["count"] if results else 0
            if count > 0:
                violations.append({
                    "label": label,
                    "missing_property": id_prop,
                    "count": count,
                })

        passed = len(violations) == 0

        return CheckResult(
            check_id="A3",
            check_name="validate_required_properties",
            passed=passed,
            severity=Severity.ERROR,
            message="All required properties present" if passed else f"Found {sum(v['count'] for v in violations)} nodes missing required properties",
            violations=violations,
            query_used="Multiple queries for each label/property combination",
            execution_time_ms=0.0,
        )

    # =========================================================================
    # Category B: Relationship Constraints (Severity: ERROR)
    # =========================================================================

    def check_b1_relationship_types(self) -> CheckResult:
        """Verify all relationship types are from the allowed set."""
        query = """
        MATCH ()-[r]->()
        WITH type(r) AS rel_type, count(*) AS count
        WHERE NOT rel_type IN $allowed_types
        RETURN rel_type, count
        """
        results, elapsed = self._execute_check(
            query, {"allowed_types": list(self.ALLOWED_RELATIONSHIP_TYPES)}
        )

        violations = [
            {"relationship_type": r["rel_type"], "count": r["count"]}
            for r in results
        ]
        passed = len(violations) == 0

        return CheckResult(
            check_id="B1",
            check_name="validate_relationship_types",
            passed=passed,
            severity=Severity.ERROR,
            message="All relationship types are valid" if passed else f"Found {len(violations)} invalid relationship types",
            violations=violations,
            query_used=query.strip(),
            execution_time_ms=elapsed,
        )

    def check_b2_relationship_directions(self) -> CheckResult:
        """Verify relationships connect correct source→target label pairs."""
        query = """
        MATCH (a)-[r]->(b)
        WITH a, r, b,
             labels(a)[0] AS source_label,
             type(r) AS rel_type,
             labels(b)[0] AS target_label
        WHERE NOT (
            (source_label = 'Requirement' AND target_label = 'Requirement'
             AND rel_type IN ['REFINES', 'DEPENDS_ON', 'TRACES_TO'])
            OR
            (source_label = 'Requirement' AND target_label = 'BusinessObject'
             AND rel_type IN ['RELATES_TO_OBJECT', 'IMPACTS_OBJECT'])
            OR
            (source_label = 'Requirement' AND target_label = 'Message'
             AND rel_type IN ['RELATES_TO_MESSAGE', 'IMPACTS_MESSAGE'])
            OR
            (source_label = 'Message' AND target_label = 'BusinessObject'
             AND rel_type IN ['USES_OBJECT', 'PRODUCES_OBJECT'])
        )
        RETURN source_label, rel_type, target_label, count(*) AS violation_count
        """
        results, elapsed = self._execute_check(query)

        violations = [
            {
                "source": r["source_label"],
                "relationship": r["rel_type"],
                "target": r["target_label"],
                "count": r["violation_count"],
            }
            for r in results
        ]
        passed = len(violations) == 0

        return CheckResult(
            check_id="B2",
            check_name="validate_relationship_directions",
            passed=passed,
            severity=Severity.ERROR,
            message="All relationships have valid source→target combinations" if passed else f"Found {len(violations)} invalid relationship patterns",
            violations=violations,
            query_used=query.strip(),
            execution_time_ms=elapsed,
        )

    def check_b3_self_loops(self) -> CheckResult:
        """Verify no invalid self-referential relationships exist."""
        # Self-loops are only allowed for Requirement→Requirement relationships
        query = """
        MATCH (n)-[r]->(n)
        WHERE NOT (n:Requirement AND type(r) IN ['REFINES', 'DEPENDS_ON', 'TRACES_TO'])
        RETURN labels(n)[0] AS label, type(r) AS rel_type,
               CASE WHEN n.rid IS NOT NULL THEN n.rid
                    WHEN n.boid IS NOT NULL THEN n.boid
                    WHEN n.mid IS NOT NULL THEN n.mid
                    ELSE 'unknown' END AS node_id,
               count(*) AS count
        """
        results, elapsed = self._execute_check(query)

        violations = [
            {
                "label": r["label"],
                "relationship": r["rel_type"],
                "node_id": r["node_id"],
                "count": r["count"],
            }
            for r in results
        ]
        passed = len(violations) == 0

        return CheckResult(
            check_id="B3",
            check_name="validate_no_invalid_self_loops",
            passed=passed,
            severity=Severity.ERROR,
            message="No invalid self-loops found" if passed else f"Found {len(violations)} invalid self-referential relationships",
            violations=violations,
            query_used=query.strip(),
            execution_time_ms=elapsed,
        )

    # =========================================================================
    # Category C: Quality Gates (Severity: WARNING)
    # =========================================================================

    def check_c1_orphan_requirements(self) -> CheckResult:
        """Verify requirements have connections to BusinessObject or Message."""
        query = """
        MATCH (r:Requirement)
        WHERE NOT (r)--(:BusinessObject) AND NOT (r)--(:Message)
        RETURN r.rid AS rid, r.name AS name
        LIMIT 100
        """
        results, elapsed = self._execute_check(query)

        violations = [{"rid": r["rid"], "name": r["name"]} for r in results]
        passed = len(violations) == 0

        return CheckResult(
            check_id="C1",
            check_name="validate_orphan_requirements",
            passed=passed,
            severity=Severity.WARNING,
            message="All requirements have connections" if passed else f"Found {len(violations)} orphan requirements (no BO/Message connection)",
            violations=violations,
            query_used=query.strip(),
            execution_time_ms=elapsed,
        )

    def check_c2_message_content(self) -> CheckResult:
        """Verify messages have USES_OBJECT relationships."""
        query = """
        MATCH (m:Message)
        WHERE NOT (m)-[:USES_OBJECT]->(:BusinessObject)
        RETURN m.mid AS mid, m.name AS name
        LIMIT 100
        """
        results, elapsed = self._execute_check(query)

        violations = [{"mid": r["mid"], "name": r["name"]} for r in results]
        passed = len(violations) == 0

        return CheckResult(
            check_id="C2",
            check_name="validate_message_content",
            passed=passed,
            severity=Severity.WARNING,
            message="All messages specify their content" if passed else f"Found {len(violations)} messages without USES_OBJECT relationship",
            violations=violations,
            query_used=query.strip(),
            execution_time_ms=elapsed,
        )

    def check_c3_gobd_traceability(self) -> CheckResult:
        """Verify GoBD-relevant items have proper IMPACTS_* relationships."""
        query = """
        MATCH (r:Requirement)
        WHERE r.goBDRelevant = true
        AND NOT (r)-[:IMPACTS_OBJECT]->(:BusinessObject)
        AND NOT (r)-[:IMPACTS_MESSAGE]->(:Message)
        RETURN r.rid AS rid, r.name AS name
        LIMIT 100
        """
        results, elapsed = self._execute_check(query)

        violations = [{"rid": r["rid"], "name": r["name"]} for r in results]
        passed = len(violations) == 0

        return CheckResult(
            check_id="C3",
            check_name="validate_gobd_traceability",
            passed=passed,
            severity=Severity.WARNING,
            message="All GoBD-relevant requirements have impact traceability" if passed else f"Found {len(violations)} GoBD-relevant requirements lacking IMPACTS_* relationships",
            violations=violations,
            query_used=query.strip(),
            execution_time_ms=elapsed,
        )

    # =========================================================================
    # Aggregation Methods
    # =========================================================================

    def run_all_checks(self) -> ComplianceReport:
        """Execute all compliance checks and return a complete report."""
        results = []

        # Category A: Structural
        results.append(self.check_a1_node_labels())
        results.append(self.check_a2_unique_constraints())
        results.append(self.check_a3_required_properties())

        # Category B: Relationships
        results.append(self.check_b1_relationship_types())
        results.append(self.check_b2_relationship_directions())
        results.append(self.check_b3_self_loops())

        # Category C: Quality Gates
        results.append(self.check_c1_orphan_requirements())
        results.append(self.check_c2_message_content())
        results.append(self.check_c3_gobd_traceability())

        # Aggregate results
        error_count = sum(
            1 for r in results if not r.passed and r.severity == Severity.ERROR
        )
        warning_count = sum(
            1 for r in results if not r.passed and r.severity == Severity.WARNING
        )

        return ComplianceReport(
            timestamp=datetime.utcnow(),
            passed=(error_count == 0),
            error_count=error_count,
            warning_count=warning_count,
            checks_performed=[r.check_id for r in results],
            results=results,
        )

    def run_structural_checks(self) -> ComplianceReport:
        """Run only Category A (structural) checks."""
        results = [
            self.check_a1_node_labels(),
            self.check_a2_unique_constraints(),
            self.check_a3_required_properties(),
        ]
        return self._create_report(results)

    def run_relationship_checks(self) -> ComplianceReport:
        """Run only Category B (relationship) checks."""
        results = [
            self.check_b1_relationship_types(),
            self.check_b2_relationship_directions(),
            self.check_b3_self_loops(),
        ]
        return self._create_report(results)

    def run_quality_gate_checks(self) -> ComplianceReport:
        """Run only Category C (quality gate) checks."""
        results = [
            self.check_c1_orphan_requirements(),
            self.check_c2_message_content(),
            self.check_c3_gobd_traceability(),
        ]
        return self._create_report(results)

    def run_specific_check(self, check_id: str) -> ComplianceReport:
        """Run a specific check by ID."""
        check_map = {
            "A1": self.check_a1_node_labels,
            "A2": self.check_a2_unique_constraints,
            "A3": self.check_a3_required_properties,
            "B1": self.check_b1_relationship_types,
            "B2": self.check_b2_relationship_directions,
            "B3": self.check_b3_self_loops,
            "C1": self.check_c1_orphan_requirements,
            "C2": self.check_c2_message_content,
            "C3": self.check_c3_gobd_traceability,
        }

        if check_id not in check_map:
            raise ValueError(f"Unknown check ID: {check_id}. Valid IDs: {list(check_map.keys())}")

        results = [check_map[check_id]()]
        return self._create_report(results)

    def _create_report(self, results: list[CheckResult]) -> ComplianceReport:
        """Create a ComplianceReport from a list of check results."""
        error_count = sum(
            1 for r in results if not r.passed and r.severity == Severity.ERROR
        )
        warning_count = sum(
            1 for r in results if not r.passed and r.severity == Severity.WARNING
        )

        return ComplianceReport(
            timestamp=datetime.utcnow(),
            passed=(error_count == 0),
            error_count=error_count,
            warning_count=warning_count,
            checks_performed=[r.check_id for r in results],
            results=results,
        )
