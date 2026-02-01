"""Graph change routes for timeline visualization.

Provides endpoints to fetch and parse Neo4j graph changes from the MongoDB audit trail.
"""

import math
import re
from typing import Any

from fastapi import APIRouter, HTTPException

from database import MongoDB

# Shared MongoDB instance - will be connected via lifespan in main.py
# We need to import the instance from main to share the connection
# For now, create a module-level reference that will be set from main
_mongodb: MongoDB | None = None


def set_mongodb(mongodb: MongoDB) -> None:
    """Set the MongoDB instance from main.py."""
    global _mongodb
    _mongodb = mongodb


def get_mongodb() -> MongoDB | None:
    """Get the MongoDB instance."""
    return _mongodb

router = APIRouter(prefix="/api/graph", tags=["graph"])

# Snapshot configuration
MIN_SNAPSHOT_INTERVAL = 50
MAX_SNAPSHOT_INTERVAL = 100
MAX_DELTA_CHAIN = 50


@router.get("/changes/{job_id}")
async def get_graph_changes(job_id: str) -> dict[str, Any]:
    """Get parsed graph changes for a job.

    Fetches tool calls from MongoDB, parses Cypher queries,
    and returns snapshots + deltas for timeline visualization.

    Args:
        job_id: The job UUID to query

    Returns:
        Dict with jobId, timeRange, summary, snapshots, and deltas
    """
    mongodb = get_mongodb()
    if mongodb is None or not mongodb.is_available:
        raise HTTPException(
            status_code=503,
            detail="MongoDB not available",
        )

    try:
        # Get all audit entries for this job
        all_entries = await _get_all_tool_calls(job_id)

        # Filter to graph operations (execute_cypher_query)
        graph_calls = [
            entry for entry in all_entries
            if entry.get("tool", {}).get("name") == "execute_cypher_query"
        ]

        if not graph_calls:
            return {
                "jobId": job_id,
                "timeRange": None,
                "summary": {
                    "totalToolCalls": len(all_entries),
                    "graphToolCalls": 0,
                    "nodesCreated": 0,
                    "nodesDeleted": 0,
                    "nodesModified": 0,
                    "relationshipsCreated": 0,
                    "relationshipsDeleted": 0,
                },
                "snapshots": [],
                "deltas": [],
            }

        # Parse each Cypher query into a delta
        deltas = []
        for i, entry in enumerate(graph_calls):
            # Get query from tool arguments
            query = entry.get("tool", {}).get("arguments", {}).get("query", "")
            parsed = parse_cypher_query(query)

            deltas.append({
                "timestamp": entry["timestamp"],
                "toolCallIndex": i,
                "cypherQuery": query,
                "toolCallId": entry["_id"],
                "stepNumber": entry.get("step_number"),
                "changes": parsed,
            })

        # Build snapshots using sqrt(N) interval
        n = len(deltas)
        interval = max(MIN_SNAPSHOT_INTERVAL, min(MAX_SNAPSHOT_INTERVAL, int(math.sqrt(n))))
        snapshots = _build_snapshots(deltas, interval=interval)

        # Compute summary
        summary = _compute_summary(deltas, len(all_entries))

        return {
            "jobId": job_id,
            "timeRange": {
                "start": graph_calls[0]["timestamp"],
                "end": graph_calls[-1]["timestamp"],
            },
            "summary": summary,
            "snapshots": snapshots,
            "deltas": deltas,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


async def _get_all_tool_calls(job_id: str) -> list[dict[str, Any]]:
    """Get all tool_call and tool_result entries for a job.

    Fetches all pages from MongoDB to get complete audit trail.
    """
    mongodb = get_mongodb()
    if mongodb is None or not mongodb.is_available or mongodb._db is None:
        return []

    collection = mongodb._db["agent_audit"]

    # Query for tool-related entries
    # Note: archiver stores tool calls with step_type="tool"
    query = {
        "job_id": job_id,
        "step_type": "tool",
    }

    # Fetch all matching entries sorted by step_number
    cursor = collection.find(query).sort("step_number", 1)

    entries = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        if "timestamp" in doc:
            doc["timestamp"] = doc["timestamp"].isoformat() if hasattr(doc["timestamp"], "isoformat") else doc["timestamp"]
        entries.append(doc)

    return entries


def parse_cypher_query(query: str) -> dict[str, list[Any]]:
    """Parse a Cypher query and extract graph operations.

    Supports CREATE, MERGE, DELETE, DETACH DELETE, SET, and REMOVE operations.
    Returns structured change objects for nodes and relationships.

    Args:
        query: Cypher query string

    Returns:
        Dict with nodesCreated, nodesDeleted, nodesModified,
        relationshipsCreated, relationshipsDeleted, matchedVariables
    """
    changes: dict[str, list[Any]] = {
        "nodesCreated": [],
        "nodesDeleted": [],
        "nodesModified": [],
        "relationshipsCreated": [],
        "relationshipsDeleted": [],
        "matchedVariables": [],  # Variables bound in MATCH clauses
    }

    if not query:
        return changes

    # Extract MATCH variable bindings: MATCH (var:Label {props})
    # This regex finds all node patterns anywhere in the query (not just after MATCH)
    for match in re.finditer(
        r'\((\w+):(\w+)\s*(?:\{([^}]*)\})?\)',
        query,
        re.IGNORECASE,
    ):
        # Only add if it looks like a MATCH pattern (has properties that identify it)
        props = _parse_properties(match.group(3))
        if props:  # Only add if there are identifying properties
            changes["matchedVariables"].append({
                "variable": match.group(1),
                "label": match.group(2),
                "properties": props,
            })

    # CREATE node: CREATE (var:Label {props}) or CREATE (var:Label)
    for match in re.finditer(
        r'CREATE\s+\((\w+):(\w+)\s*(?:\{([^}]*)\})?\)',
        query,
        re.IGNORECASE,
    ):
        changes["nodesCreated"].append({
            "variable": match.group(1),
            "label": match.group(2),
            "properties": _parse_properties(match.group(3)),
        })

    # MERGE node: MERGE (var:Label {props}) or MERGE (var:Label)
    for match in re.finditer(
        r'MERGE\s+\((\w+):(\w+)\s*(?:\{([^}]*)\})?\)',
        query,
        re.IGNORECASE,
    ):
        changes["nodesCreated"].append({
            "variable": match.group(1),
            "label": match.group(2),
            "properties": _parse_properties(match.group(3)),
            "merge": True,
        })

    # DELETE: DELETE var or DETACH DELETE var
    for match in re.finditer(
        r'(DETACH\s+)?DELETE\s+(\w+)',
        query,
        re.IGNORECASE,
    ):
        changes["nodesDeleted"].append({
            "variable": match.group(2),
            "detach": bool(match.group(1)),
        })

    # CREATE relationship: CREATE (a)-[:TYPE {props}]->(b) or CREATE (a)-[var:TYPE]->(b)
    for match in re.finditer(
        r'CREATE\s+\((\w+)\)-\[(?:\w+)?:(\w+)\s*(?:\{([^}]*)\})?\]->\((\w+)\)',
        query,
        re.IGNORECASE,
    ):
        changes["relationshipsCreated"].append({
            "sourceVar": match.group(1),
            "type": match.group(2),
            "properties": _parse_properties(match.group(3)),
            "targetVar": match.group(4),
        })

    # MERGE relationship: MERGE (a)-[:TYPE]->(b) or MERGE (a)-[var:TYPE]->(b)
    for match in re.finditer(
        r'MERGE\s+\((\w+)\)-\[(?:\w+)?:(\w+)\s*(?:\{([^}]*)\})?\]->\((\w+)\)',
        query,
        re.IGNORECASE,
    ):
        changes["relationshipsCreated"].append({
            "sourceVar": match.group(1),
            "type": match.group(2),
            "properties": _parse_properties(match.group(3)),
            "targetVar": match.group(4),
            "merge": True,
        })

    # SET: SET var.prop = value
    for match in re.finditer(
        r'SET\s+(\w+)\.(\w+)\s*=\s*([^\s,;]+)',
        query,
        re.IGNORECASE,
    ):
        changes["nodesModified"].append({
            "variable": match.group(1),
            "property": match.group(2),
            "value": match.group(3).strip("'\""),
        })

    # SET with multiple properties: SET var = {props} or SET var += {props}
    for match in re.finditer(
        r'SET\s+(\w+)\s*\+?=\s*\{([^}]+)\}',
        query,
        re.IGNORECASE,
    ):
        props = _parse_properties(match.group(2))
        for prop_name, prop_value in props.items():
            changes["nodesModified"].append({
                "variable": match.group(1),
                "property": prop_name,
                "value": prop_value,
            })

    # REMOVE property: REMOVE var.prop
    for match in re.finditer(
        r'REMOVE\s+(\w+)\.(\w+)',
        query,
        re.IGNORECASE,
    ):
        changes["nodesModified"].append({
            "variable": match.group(1),
            "property": match.group(2),
            "removed": True,
        })

    # REMOVE label: REMOVE var:Label
    for match in re.finditer(
        r'REMOVE\s+(\w+):(\w+)',
        query,
        re.IGNORECASE,
    ):
        changes["nodesModified"].append({
            "variable": match.group(1),
            "labelRemoved": match.group(2),
        })

    # DELETE relationship in MATCH: MATCH ()-[r]->() DELETE r
    for match in re.finditer(
        r'DELETE\s+(\w+)\s*(?:,|\s|$)(?!.*DETACH)',
        query,
        re.IGNORECASE,
    ):
        # Check if this variable was defined as a relationship
        var = match.group(1)
        if re.search(rf'\[{var}(?::\w+)?\]', query, re.IGNORECASE):
            changes["relationshipsDeleted"].append({
                "variable": var,
            })

    return changes


def _parse_properties(props_str: str | None) -> dict[str, Any]:
    """Parse Neo4j property string: {key: 'value', key2: 123}

    Args:
        props_str: Property string without braces

    Returns:
        Dict of property name to value
    """
    if not props_str:
        return {}

    props: dict[str, Any] = {}

    # Handle quoted strings and unquoted values
    for match in re.finditer(
        r"(\w+)\s*:\s*(?:'([^']*)'|\"([^\"]*)\"|(\[[^\]]*\])|([^,}]+))",
        props_str,
    ):
        key = match.group(1)
        # Check each capture group for the value
        value = match.group(2) or match.group(3) or match.group(4) or match.group(5)
        if value:
            value = value.strip()
            # Try to parse as number
            if value.isdigit():
                props[key] = int(value)
            elif re.match(r'^-?\d+\.?\d*$', value):
                props[key] = float(value)
            elif value.lower() == 'true':
                props[key] = True
            elif value.lower() == 'false':
                props[key] = False
            elif value.lower() == 'null':
                props[key] = None
            else:
                props[key] = value

    return props


def _build_snapshots(deltas: list[dict[str, Any]], interval: int) -> list[dict[str, Any]]:
    """Build graph snapshots at regular intervals.

    Creates complete graph state snapshots at every `interval` operations,
    allowing fast seeking during timeline scrubbing.

    Args:
        deltas: List of parsed delta objects
        interval: Number of operations between snapshots

    Returns:
        List of snapshot objects with nodes and relationships
    """
    snapshots: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}
    relationships: dict[str, dict[str, Any]] = {}

    # Persistent variable to ID mappings (across all deltas)
    var_to_id: dict[str, str] = {}

    for i, delta in enumerate(deltas):
        changes = delta.get("changes", {})

        # First, process MATCH variable bindings to resolve references
        for matched in changes.get("matchedVariables", []):
            # Get ID from matched node properties
            matched_id = _get_node_id(matched)

            # If node with this ID exists, use it directly
            if matched_id in nodes:
                var_to_id[matched["variable"]] = matched_id
            else:
                # Try to find existing node by checking if it was created with this variable
                # in an earlier delta (the node exists but with a different ID derivation)
                existing_id = var_to_id.get(matched["variable"])
                if existing_id and existing_id in nodes:
                    # Keep the existing mapping - don't overwrite with unresolvable ID
                    pass
                else:
                    # No existing mapping, use the derived ID
                    var_to_id[matched["variable"]] = matched_id

        # Apply changes to build current state
        for node in changes.get("nodesCreated", []):
            # Generate ID from properties or variable name
            node_id = _get_node_id(node)
            var_to_id[node["variable"]] = node_id
            nodes[node_id] = {
                "id": node_id,
                "labels": [node["label"]],
                "properties": node.get("properties", {}),
                "createdAt": delta["toolCallIndex"],
                "modifiedAt": delta["toolCallIndex"],
                "visible": True,
            }

        for node in changes.get("nodesDeleted", []):
            # Try to resolve variable to ID
            node_id = var_to_id.get(node["variable"], node["variable"])
            if node_id in nodes:
                nodes[node_id]["visible"] = False
                nodes[node_id]["deletedAt"] = delta["toolCallIndex"]

                # Cascade: mark relationships referencing this node as invisible
                for rel_id, rel_data in relationships.items():
                    if rel_data["sourceId"] == node_id or rel_data["targetId"] == node_id:
                        rel_data["visible"] = False
                        rel_data["deletedAt"] = delta["toolCallIndex"]

        for mod in changes.get("nodesModified", []):
            node_id = var_to_id.get(mod["variable"], mod["variable"])
            if node_id in nodes:
                if mod.get("removed"):
                    nodes[node_id]["properties"].pop(mod["property"], None)
                elif "value" in mod:
                    nodes[node_id]["properties"][mod["property"]] = mod["value"]
                nodes[node_id]["modifiedAt"] = delta["toolCallIndex"]

        for rel in changes.get("relationshipsCreated", []):
            source_id = var_to_id.get(rel["sourceVar"], rel["sourceVar"])
            target_id = var_to_id.get(rel["targetVar"], rel["targetVar"])

            # Try to resolve node IDs - find matching nodes if exact ID doesn't exist
            source_id = _resolve_node_id(source_id, rel["sourceVar"], nodes)
            target_id = _resolve_node_id(target_id, rel["targetVar"], nodes)

            # Skip only if we truly can't find the nodes
            if source_id is None or target_id is None:
                continue

            rel_id = f"{source_id}-{rel['type']}-{target_id}"
            relationships[rel_id] = {
                "id": rel_id,
                "type": rel["type"],
                "sourceId": source_id,
                "targetId": target_id,
                "properties": rel.get("properties", {}),
                "createdAt": delta["toolCallIndex"],
                "visible": True,
            }

        for rel in changes.get("relationshipsDeleted", []):
            rel_var = rel["variable"]
            # Mark matching relationships as deleted
            for rel_id, rel_data in relationships.items():
                if rel_var in rel_id:
                    rel_data["visible"] = False
                    rel_data["deletedAt"] = delta["toolCallIndex"]

        # Create snapshot at intervals or after large operations
        should_snapshot = (
            i == 0 or  # First operation
            (i + 1) % interval == 0 or  # Regular interval
            i - (snapshots[-1]["toolCallIndex"] if snapshots else 0) >= MAX_DELTA_CHAIN or  # Chain limit
            len(changes.get("nodesCreated", [])) > 50 or  # Large create
            len(changes.get("nodesDeleted", [])) > 50  # Large delete
        )

        if should_snapshot:
            # Deep copy current state for snapshot
            snapshots.append({
                "timestamp": delta["timestamp"],
                "toolCallIndex": i,
                "nodes": {k: dict(v) for k, v in nodes.items()},
                "relationships": {k: dict(v) for k, v in relationships.items()},
            })

    return snapshots


def _resolve_node_id(
    derived_id: str,
    variable: str,
    nodes: dict[str, dict[str, Any]],
) -> str | None:
    """Resolve a node ID, trying to find a matching node if exact ID doesn't exist.

    Args:
        derived_id: The ID derived from var_to_id mapping
        variable: The original variable name from the Cypher query
        nodes: Current nodes dict

    Returns:
        Resolved node ID, or None if no match found
    """
    # Exact match
    if derived_id in nodes:
        return derived_id

    # Try to find node by variable name pattern (e.g., "Label_variable")
    for node_id in nodes:
        if node_id.endswith(f"_{variable}"):
            return node_id

    # Try to find node whose ID contains the derived_id or vice versa
    for node_id in nodes:
        if derived_id in node_id or node_id in derived_id:
            return node_id

    # Try to find node with matching property value
    for node_id, node_data in nodes.items():
        props = node_data.get("properties", {})
        for prop_value in props.values():
            if str(prop_value) == derived_id:
                return node_id

    # No match found
    return None


def _get_node_id(node: dict[str, Any]) -> str:
    """Extract or generate a node ID from its properties.

    Looks for common ID properties in order of preference.
    Falls back to label_variable if no ID found.

    Args:
        node: Node dict with variable, label, properties

    Returns:
        Node ID string
    """
    props = node.get("properties", {})

    # Try common ID properties (domain-specific IDs first, then generic)
    for id_prop in ["rid", "boid", "mid", "sid", "id", "uuid", "ID", "name", "title", "key"]:
        if id_prop in props and props[id_prop]:
            return str(props[id_prop])

    # Fallback to label + variable
    return f"{node['label']}_{node['variable']}"


def _compute_summary(deltas: list[dict[str, Any]], total_tool_calls: int) -> dict[str, int]:
    """Compute summary statistics from deltas.

    Args:
        deltas: List of parsed delta objects
        total_tool_calls: Total number of tool calls (not just graph operations)

    Returns:
        Summary dict with counts
    """
    summary = {
        "totalToolCalls": total_tool_calls,
        "graphToolCalls": len(deltas),
        "nodesCreated": 0,
        "nodesDeleted": 0,
        "nodesModified": 0,
        "relationshipsCreated": 0,
        "relationshipsDeleted": 0,
    }

    for delta in deltas:
        changes = delta.get("changes", {})
        summary["nodesCreated"] += len(changes.get("nodesCreated", []))
        summary["nodesDeleted"] += len(changes.get("nodesDeleted", []))
        summary["nodesModified"] += len(changes.get("nodesModified", []))
        summary["relationshipsCreated"] += len(changes.get("relationshipsCreated", []))
        summary["relationshipsDeleted"] += len(changes.get("relationshipsDeleted", []))

    return summary
