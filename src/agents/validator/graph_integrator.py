"""Graph Integrator for Validator Agent.

Handles safe integration of validated requirements into the Neo4j graph,
including transaction management, metamodel validation, and rollback.
"""

import logging
import uuid
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from src.core.neo4j_utils import Neo4jConnection
from src.core.metamodel_validator import MetamodelValidator, ComplianceReport

logger = logging.getLogger(__name__)


@dataclass
class GraphOperation:
    """Represents a single graph operation."""
    operation_type: str  # create_node, create_relationship, update_node
    target_label: Optional[str] = None
    target_id: Optional[str] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    relationship_type: Optional[str] = None
    source_id: Optional[str] = None
    destination_id: Optional[str] = None
    cypher_query: str = ""
    executed: bool = False
    success: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "operation_type": self.operation_type,
            "target_label": self.target_label,
            "target_id": self.target_id,
            "properties": self.properties,
            "relationship_type": self.relationship_type,
            "source_id": self.source_id,
            "destination_id": self.destination_id,
            "cypher_query": self.cypher_query,
            "executed": self.executed,
            "success": self.success,
            "error": self.error,
        }


@dataclass
class IntegrationResult:
    """Result of graph integration."""
    success: bool
    requirement_node_id: Optional[str] = None
    operations_executed: List[GraphOperation] = field(default_factory=list)
    relationships_created: int = 0
    metamodel_valid: bool = True
    compliance_report: Optional[Dict] = None
    rolled_back: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "success": self.success,
            "requirement_node_id": self.requirement_node_id,
            "operations_executed": [op.to_dict() for op in self.operations_executed],
            "relationships_created": self.relationships_created,
            "metamodel_valid": self.metamodel_valid,
            "compliance_report": self.compliance_report,
            "rolled_back": self.rolled_back,
            "error": self.error,
        }


class GraphIntegrator:
    """Integrates validated requirements into the Neo4j graph.

    Handles:
    - Requirement node creation with proper properties
    - Fulfillment relationship creation (FULFILLED_BY_*, NOT_FULFILLED_BY_*)
    - Transaction safety with rollback on failure
    - Metamodel validation before commit
    - Operation logging for audit trail

    Example:
        ```python
        integrator = GraphIntegrator(neo4j_conn)
        result = integrator.integrate_requirement(
            requirement_data={...},
            fulfillment_analysis={...},
        )
        if result.success:
            print(f"Created requirement: {result.requirement_node_id}")
        ```
    """

    def __init__(
        self,
        neo4j_connection: Neo4jConnection,
        validate_before_commit: bool = True,
        dry_run: bool = False,
    ):
        """Initialize the graph integrator.

        Args:
            neo4j_connection: Active Neo4j connection
            validate_before_commit: Run metamodel validation before committing
            dry_run: If True, don't actually commit changes
        """
        self.neo4j = neo4j_connection
        self.validate_before_commit = validate_before_commit
        self.dry_run = dry_run
        self.validator = MetamodelValidator(neo4j_connection)

    def _generate_requirement_id(self) -> str:
        """Generate a new unique requirement ID.

        Returns:
            New RID like "R-0042"
        """
        query = """
        MATCH (r:Requirement)
        WHERE r.rid STARTS WITH 'R-'
        RETURN r.rid AS rid
        ORDER BY r.rid DESC
        LIMIT 1
        """
        try:
            results = self.neo4j.execute_query(query)
            if results and results[0].get("rid"):
                import re
                last_rid = results[0]["rid"]
                match = re.search(r"R-(\d+)", last_rid)
                if match:
                    next_num = int(match.group(1)) + 1
                    return f"R-{next_num:04d}"
            return "R-0001"
        except Exception as e:
            logger.warning(f"Error generating RID: {e}")
            # Fallback to UUID-based
            return f"R-{uuid.uuid4().hex[:8].upper()}"

    def _build_create_requirement_query(
        self,
        requirement: Dict[str, Any],
        rid: str,
    ) -> Tuple[str, Dict[str, Any]]:
        """Build Cypher query to create requirement node.

        Args:
            requirement: Requirement data
            rid: Generated requirement ID

        Returns:
            Tuple of (query, parameters)
        """
        # Map requirement fields to node properties
        properties = {
            "rid": rid,
            "name": requirement.get("name", f"Requirement {rid}"),
            "text": requirement.get("text", ""),
            "type": requirement.get("type", "functional"),
            "priority": requirement.get("priority", "medium"),
            "status": "active",
            "source": requirement.get("source_document", ""),
            "valueStream": requirement.get("value_stream", ""),
            "goBDRelevant": requirement.get("gobd_relevant", False),
            "gdprRelevant": requirement.get("gdpr_relevant", False),
            "complianceStatus": requirement.get("compliance_status", "open"),
            "createdAt": datetime.utcnow().isoformat(),
            "createdBy": "validator_agent",
        }

        # Build query
        query = """
        CREATE (r:Requirement $props)
        RETURN r.rid AS rid, elementId(r) AS element_id
        """

        return query, {"props": properties}

    def _build_relationship_query(
        self,
        source_rid: str,
        target_id: str,
        target_label: str,
        relationship_type: str,
        properties: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        """Build Cypher query to create a relationship.

        Args:
            source_rid: Source requirement RID
            target_id: Target entity ID (boid or mid)
            target_label: Target node label
            relationship_type: Relationship type
            properties: Relationship properties

        Returns:
            Tuple of (query, parameters)
        """
        id_field = "boid" if target_label == "BusinessObject" else "mid"

        query = f"""
        MATCH (r:Requirement {{rid: $source_rid}})
        MATCH (t:{target_label} {{{id_field}: $target_id}})
        CREATE (r)-[rel:{relationship_type} $props]->(t)
        RETURN type(rel) AS rel_type
        """

        params = {
            "source_rid": source_rid,
            "target_id": target_id,
            "props": {
                **properties,
                "validatedAt": datetime.utcnow().isoformat(),
                "validatedByAgent": "validator",
            },
        }

        return query, params

    def _execute_operation(
        self,
        operation: GraphOperation,
    ) -> GraphOperation:
        """Execute a single graph operation.

        Args:
            operation: Operation to execute

        Returns:
            Updated operation with execution result
        """
        if self.dry_run:
            operation.executed = True
            operation.success = True
            logger.info(f"[DRY RUN] Would execute: {operation.cypher_query}")
            return operation

        try:
            results = self.neo4j.execute_query(operation.cypher_query, operation.properties)
            operation.executed = True
            operation.success = True

            # Extract created node ID if applicable
            if results and operation.operation_type == "create_node":
                operation.target_id = results[0].get("rid") or results[0].get("element_id")

            logger.info(f"Executed {operation.operation_type}: {operation.target_id or operation.relationship_type}")

        except Exception as e:
            operation.executed = True
            operation.success = False
            operation.error = str(e)
            logger.error(f"Operation failed: {e}")

        return operation

    def _rollback_operations(
        self,
        operations: List[GraphOperation],
    ) -> None:
        """Rollback executed operations.

        Args:
            operations: List of operations to rollback
        """
        if self.dry_run:
            logger.info("[DRY RUN] Would rollback operations")
            return

        for operation in reversed(operations):
            if not operation.executed or not operation.success:
                continue

            try:
                if operation.operation_type == "create_node":
                    # Delete created node
                    if operation.target_id:
                        delete_query = """
                        MATCH (n:Requirement {rid: $rid})
                        DETACH DELETE n
                        """
                        self.neo4j.execute_query(delete_query, {"rid": operation.target_id})
                        logger.info(f"Rolled back: deleted node {operation.target_id}")

                elif operation.operation_type == "create_relationship":
                    # Delete created relationship
                    if operation.source_id and operation.destination_id and operation.relationship_type:
                        delete_query = f"""
                        MATCH (a)-[r:{operation.relationship_type}]->(b)
                        WHERE elementId(a) = $source_id OR elementId(b) = $dest_id
                        DELETE r
                        """
                        self.neo4j.execute_query(delete_query, {
                            "source_id": operation.source_id,
                            "dest_id": operation.destination_id,
                        })
                        logger.info(f"Rolled back: deleted relationship {operation.relationship_type}")

            except Exception as e:
                logger.error(f"Rollback failed for operation: {e}")

    def integrate_requirement(
        self,
        requirement_data: Dict[str, Any],
        fulfillment_analysis: Dict[str, Any],
        citation_ids: Optional[List[str]] = None,
    ) -> IntegrationResult:
        """Integrate a validated requirement into the graph.

        Args:
            requirement_data: Requirement data from cache
            fulfillment_analysis: Result from FulfillmentChecker
            citation_ids: List of citation IDs to link

        Returns:
            IntegrationResult with success status and details
        """
        operations: List[GraphOperation] = []
        result = IntegrationResult(success=False)

        try:
            # 1. Generate requirement ID
            rid = self._generate_requirement_id()
            logger.info(f"Integrating requirement with RID: {rid}")

            # 2. Set compliance status from fulfillment analysis
            requirement_data["compliance_status"] = fulfillment_analysis.get(
                "recommended_compliance_status", "open"
            )

            # 3. Create requirement node
            create_query, create_params = self._build_create_requirement_query(
                requirement_data, rid
            )

            create_op = GraphOperation(
                operation_type="create_node",
                target_label="Requirement",
                target_id=rid,
                properties=create_params["props"],
                cypher_query=create_query,
            )
            create_op = self._execute_operation(create_op)
            operations.append(create_op)

            if not create_op.success:
                result.error = f"Failed to create requirement node: {create_op.error}"
                return result

            result.requirement_node_id = rid

            # 4. Create fulfillment relationships for BusinessObjects
            for obj_fulfillment in fulfillment_analysis.get("object_fulfillments", []):
                rel_type = obj_fulfillment.get("relationship_type", "FULFILLED_BY_OBJECT")
                rel_props = {
                    "confidence": obj_fulfillment.get("confidence", 0.5),
                    "evidence": obj_fulfillment.get("evidence", ""),
                }

                if "NOT_FULFILLED" in rel_type:
                    # Add gap information
                    gaps = obj_fulfillment.get("gaps", [])
                    if gaps:
                        rel_props["gapDescription"] = gaps[0].get("gap_description", "")
                        rel_props["severity"] = gaps[0].get("severity", "major")
                        rel_props["remediation"] = gaps[0].get("remediation", "")

                rel_query, rel_params = self._build_relationship_query(
                    source_rid=rid,
                    target_id=obj_fulfillment.get("entity_id", ""),
                    target_label="BusinessObject",
                    relationship_type=rel_type,
                    properties=rel_props,
                )

                rel_op = GraphOperation(
                    operation_type="create_relationship",
                    relationship_type=rel_type,
                    source_id=rid,
                    destination_id=obj_fulfillment.get("entity_id"),
                    properties=rel_params,
                    cypher_query=rel_query,
                )
                rel_op = self._execute_operation(rel_op)
                operations.append(rel_op)

                if rel_op.success:
                    result.relationships_created += 1

            # 5. Create fulfillment relationships for Messages
            for msg_fulfillment in fulfillment_analysis.get("message_fulfillments", []):
                rel_type = msg_fulfillment.get("relationship_type", "FULFILLED_BY_MESSAGE")
                rel_props = {
                    "confidence": msg_fulfillment.get("confidence", 0.5),
                    "evidence": msg_fulfillment.get("evidence", ""),
                }

                if "NOT_FULFILLED" in rel_type:
                    gaps = msg_fulfillment.get("gaps", [])
                    if gaps:
                        rel_props["gapDescription"] = gaps[0].get("gap_description", "")
                        rel_props["severity"] = gaps[0].get("severity", "major")
                        rel_props["remediation"] = gaps[0].get("remediation", "")

                rel_query, rel_params = self._build_relationship_query(
                    source_rid=rid,
                    target_id=msg_fulfillment.get("entity_id", ""),
                    target_label="Message",
                    relationship_type=rel_type,
                    properties=rel_props,
                )

                rel_op = GraphOperation(
                    operation_type="create_relationship",
                    relationship_type=rel_type,
                    source_id=rid,
                    destination_id=msg_fulfillment.get("entity_id"),
                    properties=rel_params,
                    cypher_query=rel_query,
                )
                rel_op = self._execute_operation(rel_op)
                operations.append(rel_op)

                if rel_op.success:
                    result.relationships_created += 1

            # 6. Validate metamodel compliance if enabled
            if self.validate_before_commit and not self.dry_run:
                report = self.validator.run_all_checks()
                result.metamodel_valid = report.passed
                result.compliance_report = report.to_dict()

                if not report.passed:
                    # Check if errors are critical
                    if report.error_count > 0:
                        logger.warning(f"Metamodel validation failed with {report.error_count} errors")
                        # Rollback on critical errors
                        self._rollback_operations(operations)
                        result.rolled_back = True
                        result.error = f"Metamodel validation failed: {report.error_count} errors"
                        return result
                    else:
                        # Warnings are acceptable
                        logger.info(f"Metamodel validation passed with {report.warning_count} warnings")

            # 7. Success
            result.success = True
            result.operations_executed = operations
            logger.info(f"Successfully integrated requirement {rid} with {result.relationships_created} relationships")

        except Exception as e:
            logger.error(f"Integration failed: {e}")
            result.error = str(e)

            # Rollback on any failure
            self._rollback_operations(operations)
            result.rolled_back = True

        return result

    def create_requirement_node(
        self,
        requirement: Dict[str, Any],
    ) -> Optional[str]:
        """Create just a requirement node without relationships.

        Args:
            requirement: Requirement data

        Returns:
            Created requirement RID or None on failure
        """
        rid = self._generate_requirement_id()

        create_query, create_params = self._build_create_requirement_query(requirement, rid)

        create_op = GraphOperation(
            operation_type="create_node",
            target_label="Requirement",
            target_id=rid,
            properties=create_params["props"],
            cypher_query=create_query,
        )
        create_op = self._execute_operation(create_op)

        if create_op.success:
            return rid
        return None

    def create_fulfillment_relationship(
        self,
        requirement_rid: str,
        entity_id: str,
        entity_type: str,
        relationship_type: str,
        properties: Dict[str, Any],
    ) -> bool:
        """Create a single fulfillment relationship.

        Args:
            requirement_rid: Source requirement RID
            entity_id: Target entity ID
            entity_type: "BusinessObject" or "Message"
            relationship_type: Relationship type
            properties: Relationship properties

        Returns:
            True if successful
        """
        rel_query, rel_params = self._build_relationship_query(
            source_rid=requirement_rid,
            target_id=entity_id,
            target_label=entity_type,
            relationship_type=relationship_type,
            properties=properties,
        )

        rel_op = GraphOperation(
            operation_type="create_relationship",
            relationship_type=relationship_type,
            source_id=requirement_rid,
            destination_id=entity_id,
            properties=rel_params,
            cypher_query=rel_query,
        )
        rel_op = self._execute_operation(rel_op)

        return rel_op.success


def create_graph_integrator(
    neo4j_connection: Neo4jConnection,
    validate_before_commit: bool = True,
) -> GraphIntegrator:
    """Create a GraphIntegrator instance.

    Args:
        neo4j_connection: Active Neo4j connection
        validate_before_commit: Run metamodel validation before committing

    Returns:
        Configured GraphIntegrator
    """
    return GraphIntegrator(
        neo4j_connection=neo4j_connection,
        validate_before_commit=validate_before_commit,
    )
