# Agent Tools Description

This document describes the tools available to the LangGraph requirement analysis agent.

## Overview

The agent has access to **6 tools** (5 core + 1 optional) defined in `src/agents/graph_agent.py`.

## Core Tools

| Tool | Purpose |
|------|---------|
| `execute_cypher_query` | Execute Cypher queries against Neo4j. Returns up to 50 records. Used to find requirements, explore relationships, count nodes, and investigate impacts. |
| `get_database_schema` | Get the Neo4j schema including node labels, relationship types, and property keys. Use before writing queries to understand available data. |
| `count_nodes_by_label` | Count nodes with a specific label (e.g., `Requirement`, `BusinessObject`, `Message`). Useful for understanding data scale. |
| `find_sample_nodes` | Get sample nodes (up to 10) of a given label to understand their structure and properties before writing complex queries. |
| `validate_schema_compliance` | Run metamodel compliance checks against the graph. Supports check types: `all`, `structural`, `relationships`, `quality`, or `specific` (with a check ID like A1, B2, C3). Returns violations, warnings, and pass/fail status. |

## Optional Tool

| Tool | Purpose |
|------|---------|
| `web_search` | Search the web via Tavily AI. Only available if `TAVILY_API_KEY` is set. Used for looking up GoBD regulations, compliance requirements, documentation, and current standards. |

---

## Validation Tool Deep Dive

The `validate_schema_compliance` tool wraps the `MetamodelValidator` class (`src/core/metamodel_validator.py`), which performs **deterministic Cypher-based checks** against the Neo4j database to verify conformance with the FINIUS metamodel.

### Architecture

```
Agent calls validate_schema_compliance(check_type, specific_check)
    │
    ▼
MetamodelValidator executes Cypher queries
    │
    ▼
ComplianceReport returned (pass/fail, violations, warnings)
    │
    ▼
format_report_for_agent() converts to readable markdown for LLM
```

### Check Categories

| Category | Severity | Checks |
|----------|----------|--------|
| **A: Structural** | ERROR | A1: Valid node labels, A2: Unique IDs, A3: Required properties |
| **B: Relationships** | ERROR | B1: Valid relationship types, B2: Correct directions, B3: No invalid self-loops |
| **C: Quality** | WARNING | C1: No orphan requirements, C2: Messages have content, C3: GoBD traceability |

### What Each Check Does

#### Category A (Structural - Hard errors)

- **A1 - Node Labels**: Ensures all nodes are `Requirement`, `BusinessObject`, or `Message`
- **A2 - Unique Constraints**: Ensures `rid`, `boid`, `mid` properties are unique within their label
- **A3 - Required Properties**: Ensures each node type has its required ID property

#### Category B (Relationships - Hard errors)

- **B1 - Relationship Types**: Only allowed relationship types exist (e.g., `REFINES`, `IMPACTS_OBJECT`)
- **B2 - Relationship Directions**: Relationships go in correct directions (e.g., `Requirement→BusinessObject`, not reversed)
- **B3 - Self Loops**: No invalid self-loops (only `Requirement→Requirement` allowed for `REFINES`, `DEPENDS_ON`, `TRACES_TO`)

#### Category C (Quality - Warnings)

- **C1 - Orphan Requirements**: Requirements should connect to at least one `BusinessObject` or `Message`
- **C2 - Message Content**: Messages should have `USES_OBJECT` relationships
- **C3 - GoBD Traceability**: GoBD-relevant requirements must have `IMPACTS_*` relationships for traceability

### Tool Parameters

```python
validate_schema_compliance(
    check_type: str = "all",      # "all", "structural", "relationships", "quality", "specific"
    specific_check: str = None    # Required when check_type="specific", e.g., "A1", "B2", "C3"
)
```

---

## Example: Agent Using the Validation Tool

**User requirement:** "Verify that our graph is ready for GoBD compliance audit"

**Agent's plan:**
1. Run full compliance validation to assess graph health
2. Investigate any violations found
3. Provide remediation recommendations

### Step 1: Run all checks

```python
validate_schema_compliance(check_type="all")
```

**Tool returns:**

```markdown
## Metamodel Compliance Report
**Generated:** 2025-01-15T10:30:00
**Overall Status:** FAILED
**Errors:** 0 | **Warnings:** 2

### Quality Warnings

**[C1] validate_orphan_requirements**
- Message: Found 3 orphan requirements (no BO/Message connection)
- Items:
  - {'rid': 'REQ-042', 'name': 'Archive retention policy'}
  - {'rid': 'REQ-089', 'name': 'Invoice numbering'}
  - {'rid': 'REQ-103', 'name': 'Audit log format'}

**[C3] validate_gobd_traceability**
- Message: Found 5 GoBD-relevant requirements lacking IMPACTS_* relationships
- Items:
  - {'rid': 'REQ-007', 'name': 'Tax calculation'}
  - {'rid': 'REQ-015', 'name': 'Receipt generation'}
  - ... and 3 more
```

### Step 2: Get details on GoBD requirements lacking traceability

```python
execute_cypher_query(query="""
  MATCH (r:Requirement)
  WHERE r.goBDRelevant = true
  AND NOT (r)-[:IMPACTS_OBJECT]->(:BusinessObject)
  AND NOT (r)-[:IMPACTS_MESSAGE]->(:Message)
  RETURN r.rid, r.name, r.text
""")
```

### Step 3: Agent's final report

> The graph has **no structural errors** but has **2 quality warnings** that affect GoBD audit readiness:
>
> 1. **3 orphan requirements** have no connection to business objects or messages
> 2. **5 GoBD-relevant requirements** lack impact traceability
>
> **Recommendations:**
> - Add `IMPACTS_OBJECT` or `IMPACTS_MESSAGE` relationships for REQ-007, REQ-015, etc.
> - Review orphan requirements REQ-042, REQ-089, REQ-103 for missing relationships
> - Example fix:
>   ```cypher
>   MATCH (r:Requirement {rid: 'REQ-007'}), (bo:BusinessObject {name: 'Invoice'})
>   CREATE (r)-[:IMPACTS_OBJECT]->(bo)
>   ```

---

## Allowed Metamodel Elements

### Node Labels

- `Requirement`
- `BusinessObject`
- `Message`

### Required Properties

| Label | Required Property |
|-------|-------------------|
| `Requirement` | `rid` |
| `BusinessObject` | `boid` |
| `Message` | `mid` |

### Allowed Relationships

| Source | Relationship | Target |
|--------|--------------|--------|
| `Requirement` | `REFINES` | `Requirement` |
| `Requirement` | `DEPENDS_ON` | `Requirement` |
| `Requirement` | `TRACES_TO` | `Requirement` |
| `Requirement` | `RELATES_TO_OBJECT` | `BusinessObject` |
| `Requirement` | `IMPACTS_OBJECT` | `BusinessObject` |
| `Requirement` | `RELATES_TO_MESSAGE` | `Message` |
| `Requirement` | `IMPACTS_MESSAGE` | `Message` |
| `Message` | `USES_OBJECT` | `BusinessObject` |
| `Message` | `PRODUCES_OBJECT` | `BusinessObject` |
