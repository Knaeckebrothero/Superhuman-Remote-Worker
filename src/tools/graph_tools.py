"""Graph tools for the Universal Agent.

Provides Neo4j graph operations for the Validator Agent:
- Cypher query execution
- Duplicate detection
- Entity resolution
- Schema validation
- Requirement and relationship creation
"""

import logging
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
GRAPH_TOOLS_METADATA = {
    "execute_cypher_query": {
        "module": "graph_tools",
        "function": "execute_cypher_query",
        "description": "Execute a Cypher query against Neo4j",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Execute Cypher query against Neo4j database.",
    },
    "get_database_schema": {
        "module": "graph_tools",
        "function": "get_database_schema",
        "description": "Get Neo4j database schema (labels, relationships, properties)",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Get Neo4j schema (labels, relationships, properties).",
    },
    "find_similar_requirements": {
        "module": "graph_tools",
        "function": "find_similar_requirements",
        "description": "Find existing requirements similar to given text",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Find similar requirements by text similarity.",
    },
    "check_for_duplicates": {
        "module": "graph_tools",
        "function": "check_for_duplicates",
        "description": "Check if requirement is a duplicate (95% threshold)",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Check if requirement is a duplicate (95% threshold).",
    },
    "resolve_business_object": {
        "module": "graph_tools",
        "function": "resolve_business_object",
        "description": "Match entity mention to BusinessObject node",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Resolve mention to existing BusinessObject node.",
    },
    "resolve_message": {
        "module": "graph_tools",
        "function": "resolve_message",
        "description": "Match entity mention to Message node",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Resolve mention to existing Message node.",
    },
    "validate_schema_compliance": {
        "module": "graph_tools",
        "function": "validate_schema_compliance",
        "description": "Run metamodel compliance checks",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Run metamodel compliance checks (structural/relationships/quality).",
    },
    "create_requirement_node": {
        "module": "graph_tools",
        "function": "create_requirement_node",
        "description": "Create a Requirement node in Neo4j",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Create new Requirement node in Neo4j graph.",
    },
    "create_fulfillment_relationship": {
        "module": "graph_tools",
        "function": "create_fulfillment_relationship",
        "description": "Create fulfillment relationship between Requirement and entity",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Create fulfillment relationship (FULFILLED_BY_*, etc.).",
    },
    "generate_requirement_id": {
        "module": "graph_tools",
        "function": "generate_requirement_id",
        "description": "Generate a new unique requirement ID (R-XXXX format)",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Generate new unique R-XXXX requirement ID.",
    },
    "get_entity_relationships": {
        "module": "graph_tools",
        "function": "get_entity_relationships",
        "description": "Get all relationships for a BusinessObject or Message",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Get all relationships for a BusinessObject or Message.",
    },
    "count_graph_statistics": {
        "module": "graph_tools",
        "function": "count_graph_statistics",
        "description": "Get counts of nodes and relationships in the graph",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Get node and relationship counts in the graph.",
    },
}


def create_graph_tools(context: ToolContext) -> List:
    """Create graph tools with injected context.

    Args:
        context: ToolContext with dependencies (must include neo4j connection)

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If Neo4j database not available in context
    """
    neo4j = context.graph
    if not neo4j:
        raise ValueError("Neo4j database not available in context")

    config = context.config or {}
    duplicate_threshold = config.get("duplicate_threshold", 0.95)

    # Entity caches
    _requirements_cache: Optional[List] = None
    _business_objects_cache: Optional[List] = None
    _messages_cache: Optional[List] = None
    _schema_cache: Optional[Dict] = None

    def load_requirements() -> List:
        nonlocal _requirements_cache
        if _requirements_cache is None and neo4j:
            query = """
            MATCH (r:Requirement)
            RETURN r.rid AS rid, r.name AS name, r.text AS text,
                   r.type AS type, r.goBDRelevant AS gobd_relevant,
                   r.complianceStatus AS compliance_status
            LIMIT 1000
            """
            _requirements_cache = neo4j.execute_query(query)
        return _requirements_cache or []

    def load_business_objects() -> List:
        nonlocal _business_objects_cache
        if _business_objects_cache is None and neo4j:
            query = """
            MATCH (bo:BusinessObject)
            RETURN bo.boid AS boid, bo.name AS name, bo.description AS description,
                   bo.domain AS domain
            LIMIT 500
            """
            _business_objects_cache = neo4j.execute_query(query)
        return _business_objects_cache or []

    def load_messages() -> List:
        nonlocal _messages_cache
        if _messages_cache is None and neo4j:
            query = """
            MATCH (m:Message)
            RETURN m.mid AS mid, m.name AS name, m.description AS description,
                   m.direction AS direction
            LIMIT 500
            """
            _messages_cache = neo4j.execute_query(query)
        return _messages_cache or []

    def get_schema() -> Dict:
        nonlocal _schema_cache
        if _schema_cache is None and neo4j:
            # Neo4jDB uses get_schema(), old interface uses get_database_schema()
            _schema_cache = neo4j.get_schema() if hasattr(neo4j, 'get_schema') else neo4j.get_database_schema()
        return _schema_cache or {}

    @tool
    def execute_cypher_query(query: str) -> str:
        """Execute a Cypher query against the Neo4j database.

        Args:
            query: A valid Cypher query string

        Returns:
            String representation of query results (up to 50 records)

        Use this to explore the graph, find entities, check relationships.
        """
        if not neo4j:
            return "Error: No Neo4j connection available"

        try:
            results = neo4j.execute_query(query)

            if not results:
                return "Query executed successfully but returned no results."

            limited_results = results[:50]
            formatted = []
            for i, record in enumerate(limited_results, 1):
                formatted.append(f"Record {i}: {record}")

            result_str = "\n".join(formatted)

            if len(results) > 50:
                result_str += f"\n\n... and {len(results) - 50} more results"

            return result_str

        except Exception as e:
            return f"Error executing query: {str(e)}"

    @tool
    def get_database_schema() -> str:
        """Get the Neo4j database schema.

        Returns:
            Schema information with node labels, relationship types, properties
        """
        schema = get_schema()
        if not schema:
            return "Error: Could not retrieve schema"

        schema_str = "Neo4j Database Schema:\n\n"
        schema_str += f"Node Labels ({len(schema.get('node_labels', []))}):\n"
        for label in schema.get('node_labels', []):
            schema_str += f"  - {label}\n"

        schema_str += f"\nRelationship Types ({len(schema.get('relationship_types', []))}):\n"
        for rel_type in schema.get('relationship_types', []):
            schema_str += f"  - {rel_type}\n"

        schema_str += f"\nProperty Keys ({len(schema.get('property_keys', []))}):\n"
        for prop in schema.get('property_keys', [])[:30]:
            schema_str += f"  - {prop}\n"

        if len(schema.get('property_keys', [])) > 30:
            schema_str += f"  ... and {len(schema['property_keys']) - 30} more\n"

        return schema_str

    @tool
    def find_similar_requirements(text: str, threshold: float = 0.7) -> str:
        """Find existing requirements similar to the given text.

        Args:
            text: Requirement text to compare
            threshold: Minimum similarity score (0.0-1.0), default 0.7

        Returns:
            List of similar requirements with similarity scores
        """
        existing = load_requirements()
        similar = []
        text_lower = text.lower().strip()

        for req in existing:
            req_text = (req.get("text") or "").lower().strip()
            if not req_text:
                continue

            similarity = SequenceMatcher(None, text_lower, req_text).ratio()

            if similarity >= threshold:
                similar.append({
                    "rid": req.get("rid"),
                    "name": req.get("name"),
                    "text": req.get("text", "")[:200],
                    "similarity_score": round(similarity, 3),
                })

        similar.sort(key=lambda x: x["similarity_score"], reverse=True)
        similar = similar[:10]

        result = f"Found {len(similar)} similar requirements (threshold: {threshold}):\n\n"
        for i, s in enumerate(similar, 1):
            result += f"{i}. [{s['rid']}] {s['name']}\n"
            result += f"   Similarity: {s['similarity_score']:.1%}\n"
            result += f"   Text: {s['text'][:100]}...\n\n"

        if not similar:
            result = "No similar requirements found."

        return result

    @tool
    def check_for_duplicates(text: str) -> str:
        """Check if a requirement text is a duplicate.

        Uses high-threshold similarity (95%) to identify near-exact duplicates.

        Args:
            text: Requirement text to check

        Returns:
            Duplicate check result with recommendation
        """
        existing = load_requirements()
        text_lower = text.lower().strip()

        for req in existing:
            req_text = (req.get("text") or "").lower().strip()
            if not req_text:
                continue

            similarity = SequenceMatcher(None, text_lower, req_text).ratio()

            if similarity >= duplicate_threshold:
                return f"""DUPLICATE DETECTED:

Existing Requirement: {req['rid']}
Name: {req['name']}
Similarity: {similarity:.1%}

RECOMMENDATION: REJECT as duplicate of {req['rid']}"""

        return "No duplicates found. The requirement appears to be unique."

    @tool
    def resolve_business_object(mention: str) -> str:
        """Resolve a business object mention to an existing graph entity.

        Args:
            mention: Text mention of a business object (e.g., "Customer", "Invoice")

        Returns:
            Matched entity details or no match message
        """
        existing = load_business_objects()
        mention_lower = mention.lower().strip()

        best_match = None
        best_score = 0.0

        for bo in existing:
            name = (bo.get("name") or "").lower()

            if mention_lower == name:
                return f"""Exact match found:
  BOID: {bo['boid']}
  Name: {bo['name']}
  Domain: {bo.get('domain', 'N/A')}
  Description: {bo.get('description', 'N/A')[:100]}"""

            if mention_lower in name or name in mention_lower:
                score = 0.8
                if score > best_score:
                    best_match = bo
                    best_score = score
                continue

            score = SequenceMatcher(None, mention_lower, name).ratio()
            if score > best_score:
                best_match = bo
                best_score = score

        if best_match and best_score >= 0.6:
            return f"""Best match for '{mention}':
  BOID: {best_match['boid']}
  Name: {best_match['name']}
  Match Score: {best_score:.1%}
  Domain: {best_match.get('domain', 'N/A')}"""

        return f"No matching BusinessObject found for '{mention}'"

    @tool
    def resolve_message(mention: str) -> str:
        """Resolve a message mention to an existing graph entity.

        Args:
            mention: Text mention of a message (e.g., "CreateOrderRequest")

        Returns:
            Matched entity details or no match message
        """
        existing = load_messages()
        mention_lower = mention.lower().strip()

        best_match = None
        best_score = 0.0

        for msg in existing:
            name = (msg.get("name") or "").lower()

            if mention_lower == name:
                return f"""Exact match found:
  MID: {msg['mid']}
  Name: {msg['name']}
  Direction: {msg.get('direction', 'N/A')}"""

            if mention_lower in name or name in mention_lower:
                score = 0.8
                if score > best_score:
                    best_match = msg
                    best_score = score
                continue

            score = SequenceMatcher(None, mention_lower, name).ratio()
            if score > best_score:
                best_match = msg
                best_score = score

        if best_match and best_score >= 0.6:
            return f"""Best match for '{mention}':
  MID: {best_match['mid']}
  Name: {best_match['name']}
  Match Score: {best_score:.1%}"""

        return f"No matching Message found for '{mention}'"

    @tool
    def validate_schema_compliance(check_type: str = "all") -> str:
        """Run metamodel compliance checks against the graph.

        Args:
            check_type: Type of checks to run:
                - "all": All checks (default)
                - "structural": Node labels and properties (A1-A3)
                - "relationships": Relationship types and directions (B1-B3)
                - "quality": Quality gates (C1-C5)

        Returns:
            Compliance report with pass/fail status and violations
        """
        if not neo4j:
            return "Error: No Neo4j connection available"

        try:
            from src.utils.metamodel_validator import MetamodelValidator, Severity
            validator = MetamodelValidator(neo4j)

            if check_type == "all":
                report = validator.run_all_checks()
            elif check_type == "structural":
                report = validator.run_structural_checks()
            elif check_type == "relationships":
                report = validator.run_relationship_checks()
            elif check_type == "quality":
                report = validator.run_quality_gate_checks()
            else:
                return "Invalid check_type. Use 'all', 'structural', 'relationships', or 'quality'."

            lines = [
                "## Metamodel Compliance Report",
                f"**Status:** {'PASSED' if report.passed else 'FAILED'}",
                f"**Errors:** {report.error_count} | **Warnings:** {report.warning_count}",
                "",
            ]

            if report.error_count > 0:
                lines.append("### Errors (Must Fix)")
                for result in report.results:
                    if not result.passed and result.severity == Severity.ERROR:
                        lines.append(f"\n**{result.check_id}: {result.check_name}**")
                        lines.append(f"- {result.message}")
                        if result.violations:
                            for v in result.violations[:3]:
                                lines.append(f"  - {v}")

            if report.warning_count > 0:
                lines.append("\n### Warnings")
                for result in report.results:
                    if not result.passed and result.severity == Severity.WARNING:
                        lines.append(f"\n**{result.check_id}: {result.check_name}**")
                        lines.append(f"- {result.message}")

            return "\n".join(lines)

        except ImportError:
            return "Error: MetamodelValidator not available"
        except Exception as e:
            return f"Error running compliance check: {str(e)}"

    @tool
    def create_requirement_node(
        rid: str,
        name: str,
        text: str,
        req_type: str = "functional",
        priority: str = "medium",
        gobd_relevant: bool = False,
        gdpr_relevant: bool = False,
        compliance_status: str = "open",
    ) -> str:
        """Create a new Requirement node in the Neo4j graph.

        Args:
            rid: Requirement ID (e.g., "R-0042")
            name: Short descriptive name
            text: Full requirement text
            req_type: Type (functional, non_functional, constraint, compliance)
            priority: Priority (high, medium, low)
            gobd_relevant: GoBD relevance flag
            gdpr_relevant: GDPR relevance flag
            compliance_status: Status (open, partial, fulfilled)

        Returns:
            Creation result with node ID
        """
        if not neo4j:
            return "Error: No Neo4j connection available"

        try:
            query = """
            CREATE (r:Requirement {
                rid: $rid,
                name: $name,
                text: $text,
                type: $type,
                priority: $priority,
                status: 'active',
                goBDRelevant: $gobd_relevant,
                gdprRelevant: $gdpr_relevant,
                complianceStatus: $compliance_status,
                createdAt: datetime(),
                createdBy: 'validator_agent'
            })
            RETURN r.rid AS rid
            """

            results = neo4j.execute_query(query, {
                "rid": rid,
                "name": name,
                "text": text,
                "type": req_type,
                "priority": priority,
                "gobd_relevant": gobd_relevant,
                "gdpr_relevant": gdpr_relevant,
                "compliance_status": compliance_status,
            })

            if results:
                # Clear cache so new requirement is included in searches
                nonlocal _requirements_cache
                _requirements_cache = None
                return f"Successfully created Requirement node: {results[0]['rid']}"
            return "Requirement created but could not retrieve RID"

        except Exception as e:
            return f"Error creating requirement: {str(e)}"

    @tool
    def create_fulfillment_relationship(
        requirement_rid: str,
        entity_id: str,
        entity_type: str,
        relationship_type: str,
        confidence: float = 0.5,
        evidence: str = "",
    ) -> str:
        """Create a fulfillment relationship between a Requirement and an entity.

        Args:
            requirement_rid: Source requirement RID (e.g., "R-0042")
            entity_id: Target entity ID (boid for BusinessObject, mid for Message)
            entity_type: "BusinessObject" or "Message"
            relationship_type: FULFILLED_BY_OBJECT, NOT_FULFILLED_BY_OBJECT, etc.
            confidence: Confidence score (0.0-1.0)
            evidence: Evidence text for the relationship

        Returns:
            Creation result
        """
        if not neo4j:
            return "Error: No Neo4j connection available"

        try:
            id_field = "boid" if entity_type == "BusinessObject" else "mid"

            query = f"""
            MATCH (r:Requirement {{rid: $rid}})
            MATCH (e:{entity_type} {{{id_field}: $entity_id}})
            CREATE (r)-[rel:{relationship_type} {{
                confidence: $confidence,
                evidence: $evidence,
                validatedAt: datetime(),
                validatedByAgent: 'validator'
            }}]->(e)
            RETURN type(rel) AS rel_type
            """

            results = neo4j.execute_query(query, {
                "rid": requirement_rid,
                "entity_id": entity_id,
                "confidence": confidence,
                "evidence": evidence,
            })

            if results:
                return f"Created {results[0]['rel_type']} from {requirement_rid} to {entity_id}"
            return "Relationship created"

        except Exception as e:
            return f"Error creating relationship: {str(e)}"

    @tool
    def generate_requirement_id() -> str:
        """Generate a new unique requirement ID.

        Returns:
            New RID following the R-XXXX pattern
        """
        if not neo4j:
            return "Error: No Neo4j connection available"

        try:
            query = """
            MATCH (r:Requirement)
            WHERE r.rid STARTS WITH 'R-'
            RETURN r.rid AS rid
            ORDER BY r.rid DESC
            LIMIT 1
            """
            results = neo4j.execute_query(query)

            if results and results[0].get("rid"):
                last_rid = results[0]["rid"]
                match = re.search(r"R-(\d+)", last_rid)
                if match:
                    next_num = int(match.group(1)) + 1
                    return f"Generated RID: R-{next_num:04d}"

            return "Generated RID: R-0001"

        except Exception as e:
            return f"Error generating RID: {str(e)}"

    @tool
    def get_entity_relationships(entity_id: str, entity_type: str) -> str:
        """Get all relationships for a BusinessObject or Message.

        Args:
            entity_id: Entity ID (boid or mid)
            entity_type: "BusinessObject" or "Message"

        Returns:
            List of relationships involving the entity
        """
        if not neo4j:
            return "Error: No Neo4j connection available"

        try:
            id_field = "boid" if entity_type == "BusinessObject" else "mid"

            query = f"""
            MATCH (e:{entity_type} {{{id_field}: $entity_id}})
            OPTIONAL MATCH (r:Requirement)-[rel]->(e)
            RETURN r.rid AS req_rid, r.name AS req_name, type(rel) AS rel_type,
                   rel.confidence AS confidence
            """

            results = neo4j.execute_query(query, {"entity_id": entity_id})

            if not results or not results[0].get("req_rid"):
                return f"No requirement relationships found for {entity_type} {entity_id}"

            output = f"Relationships for {entity_type} {entity_id}:\n\n"
            for r in results:
                if r.get("req_rid"):
                    output += f"- [{r['req_rid']}] {r['req_name']}\n"
                    output += f"  Relationship: {r['rel_type']}\n"
                    if r.get("confidence"):
                        output += f"  Confidence: {r['confidence']:.1%}\n"
                    output += "\n"

            return output

        except Exception as e:
            return f"Error getting relationships: {str(e)}"

    @tool
    def count_graph_statistics() -> str:
        """Get statistics about the current graph state.

        Returns:
            Counts of nodes and relationships by type
        """
        if not neo4j:
            return "Error: No Neo4j connection available"

        try:
            # Count nodes
            node_query = """
            MATCH (n)
            RETURN labels(n)[0] AS label, count(n) AS count
            ORDER BY count DESC
            """
            node_results = neo4j.execute_query(node_query)

            # Count relationships
            rel_query = """
            MATCH ()-[r]->()
            RETURN type(r) AS type, count(r) AS count
            ORDER BY count DESC
            """
            rel_results = neo4j.execute_query(rel_query)

            output = "## Graph Statistics\n\n"
            output += "### Node Counts\n"
            for r in node_results:
                output += f"- {r['label']}: {r['count']}\n"

            output += "\n### Relationship Counts\n"
            for r in rel_results:
                output += f"- {r['type']}: {r['count']}\n"

            return output

        except Exception as e:
            return f"Error getting statistics: {str(e)}"

    return [
        execute_cypher_query,
        get_database_schema,
        find_similar_requirements,
        check_for_duplicates,
        resolve_business_object,
        resolve_message,
        validate_schema_compliance,
        create_requirement_node,
        create_fulfillment_relationship,
        generate_requirement_id,
        get_entity_relationships,
        count_graph_statistics,
    ]
