# Metamodel Compliance Verification Architecture

## Two-Phase Hybrid Pipeline for GraphRAG Domain Model Validation

**Project:** GraphRAG in Practice – Projekt Digitale Transformation  
**Partner:** FINIUS GmbH  
**Version:** 1.0  
**Date:** December 2025

---

## Executive Summary

This document describes a hybrid verification architecture for validating Neo4j graph databases against a defined metamodel. The approach combines **deterministic algorithmic checks** (for auditability and reproducibility) with **LLM-based semantic reasoning** (for interpretation and impact analysis).

The architecture is specifically designed to support:
- **Level 1:** Impact analysis comparing domain models against requirements
- **Level 2:** Metamodel-based reasoning with automated rule checking

Key benefits include GoBD-compliant audit trails, separation of concerns between structural and semantic validation, and seamless integration with the existing RequirementGraphAgent.

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Metamodel Overview](#2-metamodel-overview)
3. [Architecture Design](#3-architecture-design)
4. [Phase 1: Schema Gate](#4-phase-1-schema-gate)
5. [Phase 2: Agent Analysis](#5-phase-2-agent-analysis)
6. [Integration with RequirementGraphAgent](#6-integration-with-requirementgraphagent)
7. [Implementation Specification](#7-implementation-specification)
8. [Compliance Report Schema](#8-compliance-report-schema)
9. [Usage Patterns](#9-usage-patterns)
10. [Future Extensions](#10-future-extensions)

---

## 1. Problem Statement

### The Challenge

When working with domain models stored in Neo4j, we need to verify that the graph data conforms to a predefined metamodel. This verification must answer several questions:

1. **Structural Compliance:** Do all nodes and relationships match the allowed types?
2. **Constraint Compliance:** Are required properties present? Are unique constraints satisfied?
3. **Semantic Quality:** Are there orphan nodes? Missing connections? Incomplete traceability?
4. **Business Context:** What do violations mean? How should they be remediated?

### Why a Hybrid Approach?

| Approach | Strengths | Weaknesses |
|----------|-----------|------------|
| **Pure Cypher** | Fast, deterministic, auditable | Limited expressiveness, no explanations |
| **Pure LLM** | Flexible, explanatory, contextual | Non-deterministic, not auditable |
| **Hybrid** | Best of both worlds | More complex architecture |

For GoBD compliance and enterprise requirements, **auditability is non-negotiable**. An LLM saying "the graph looks compliant" cannot be used in an audit trail. However, an LLM explaining *why* a Cypher-detected violation matters and *how* to fix it adds significant value.

---

## 2. Metamodel Overview

The FINIUS metamodel defines a constrained graph schema for requirement traceability in a car rental business context with GoBD (German accounting compliance) focus.

### 2.1 Node Types (Labels)

| Label | Primary Key | Purpose |
|-------|-------------|---------|
| `Requirement` | `rid` (e.g., R-0001) | Business/functional requirements |
| `BusinessObject` | `boid` (e.g., BO-Rechnung) | Domain entities (Invoice, Payment, etc.) |
| `Message` | `mid` (e.g., MSG-ReservationRequest) | System messages/events |

### 2.2 Allowed Relationships

The metamodel strictly defines which relationships may exist between which node types:

```
┌─────────────────────────────────────────────────────────────────┐
│                    RELATIONSHIP MATRIX                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Requirement ──────────────────────────────► Requirement        │
│       │         REFINES                                         │
│       │         DEPENDS_ON                                      │
│       │         TRACES_TO                                       │
│       │                                                         │
│       │                                                         │
│       ├─────────────────────────────────────► BusinessObject    │
│       │         RELATES_TO_OBJECT                               │
│       │         IMPACTS_OBJECT                                  │
│       │                                                         │
│       │                                                         │
│       └─────────────────────────────────────► Message           │
│                 RELATES_TO_MESSAGE                              │
│                 IMPACTS_MESSAGE                                 │
│                                                                 │
│                                                                 │
│  Message ───────────────────────────────────► BusinessObject    │
│                 USES_OBJECT                                     │
│                 PRODUCES_OBJECT                                 │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 2.3 Quality Gates

The metamodel defines semantic quality checks (not hard constraints, but warnings):

| Gate | Query Intent | Severity |
|------|--------------|----------|
| Q1 | Requirements without connection to BO or Message | Warning |
| Q2 | Messages without `USES_OBJECT` relationship | Warning |
| Q3 | GoBD-relevant items without proper traceability | Warning |

---

## 3. Architecture Design

### 3.1 High-Level Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                         PHASE 1: Schema Gate                        │
│                      (Deterministic, Auditable)                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │
│  │  Constraint  │  │ Relationship │  │   Quality    │              │
│  │  Validator   │  │  Validator   │  │    Gates     │              │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘              │
│         │                 │                 │                       │
│         └────────────┬────┴─────────────────┘                       │
│                      ▼                                              │
│            ┌─────────────────┐                                      │
│            │ ComplianceReport │  ← Structured, JSON-serializable   │
│            │ {                │                                     │
│            │   passed: bool   │                                     │
│            │   violations: [] │                                     │
│            │   warnings: []   │                                     │
│            │   timestamp: ... │                                     │
│            │ }                │                                     │
│            └────────┬────────┘                                      │
│                     │                                               │
└─────────────────────┼───────────────────────────────────────────────┘
                      │
          ┌───────────┴───────────┐
          │                       │
          ▼                       ▼
    ┌───────────┐         ┌─────────────┐
    │   STOP    │         │  CONTINUE   │
    │ (if hard  │         │  to Phase 2 │
    │  errors)  │         │             │
    └───────────┘         └──────┬──────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    PHASE 2: Agent Analysis                          │
│                   (Semantic, Explanatory)                           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│   RequirementGraphAgent                                             │
│   ├── execute_cypher_query      (existing)                         │
│   ├── get_database_schema       (existing)                         │
│   ├── count_nodes_by_label      (existing)                         │
│   ├── find_sample_nodes         (existing)                         │
│   └── validate_schema_compliance (NEW)                             │
│                                                                     │
│   Agent receives ComplianceReport as context, then:                │
│   • Explains violations in business terms                          │
│   • Suggests remediation steps                                      │
│   • Traces impact chains                                            │
│   • Answers follow-up questions                                     │
│                                                                     │
│   Output: Natural language analysis + structured recommendations   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 Design Principles

1. **Separation of Concerns**
   - Phase 1 handles *what* is wrong (deterministic)
   - Phase 2 handles *why* it matters and *how* to fix it (semantic)

2. **Audit Trail First**
   - Every check produces a structured, timestamped result
   - Cypher queries are logged alongside results
   - GoBD compliance requires reproducibility

3. **Fail Fast, Explain Later**
   - Hard errors in Phase 1 can halt the pipeline
   - Warnings are collected but don't block Phase 2
   - Agent provides context for all findings

4. **Tool Reusability**
   - Phase 1 validator can run standalone (CI/CD, batch jobs)
   - Same validator is available as an agent tool
   - Consistent results regardless of invocation method

---

## 4. Phase 1: Schema Gate

### 4.1 Check Categories

Phase 1 implements three categories of checks, executed in order:

#### Category A: Structural Constraints (Severity: ERROR)

| Check ID | Name | Description |
|----------|------|-------------|
| `A1` | `validate_node_labels` | All nodes must have labels from {Requirement, BusinessObject, Message} |
| `A2` | `validate_unique_constraints` | IDs (rid, boid, mid) must be unique within their label |
| `A3` | `validate_required_properties` | Each node type must have its required ID property |

#### Category B: Relationship Constraints (Severity: ERROR)

| Check ID | Name | Description |
|----------|------|-------------|
| `B1` | `validate_relationship_types` | All relationship types must be from the allowed set |
| `B2` | `validate_relationship_directions` | Relationships must connect correct source→target label pairs |
| `B3` | `validate_no_self_loops` | No node should have a relationship to itself (except REFINES/DEPENDS_ON for Requirements) |

#### Category C: Quality Gates (Severity: WARNING)

| Check ID | Name | Description |
|----------|------|-------------|
| `C1` | `validate_orphan_requirements` | Requirements should connect to BO or Message |
| `C2` | `validate_message_content` | Messages should have USES_OBJECT relationships |
| `C3` | `validate_gobd_traceability` | GoBD-flagged items should have IMPACTS_* relationships |

### 4.2 Cypher Query Examples

**Check B2: Validate Relationship Directions**

This query finds any relationship that violates the metamodel:

```cypher
// Find relationships that don't match allowed (source, type, target) combinations
MATCH (a)-[r]->(b)
WHERE NOT (
  // Requirement → Requirement
  (a:Requirement AND b:Requirement AND type(r) IN ['REFINES','DEPENDS_ON','TRACES_TO'])
  OR
  // Requirement → BusinessObject
  (a:Requirement AND b:BusinessObject AND type(r) IN ['RELATES_TO_OBJECT','IMPACTS_OBJECT'])
  OR
  // Requirement → Message
  (a:Requirement AND b:Message AND type(r) IN ['RELATES_TO_MESSAGE','IMPACTS_MESSAGE'])
  OR
  // Message → BusinessObject
  (a:Message AND b:BusinessObject AND type(r) IN ['USES_OBJECT','PRODUCES_OBJECT'])
)
RETURN 
  labels(a)[0] AS source_label,
  type(r) AS relationship_type,
  labels(b)[0] AS target_label,
  a.rid AS source_id,
  b.boid AS target_id,
  count(*) AS violation_count
```

**Check C1: Orphan Requirements**

```cypher
// Find requirements with no connections to BusinessObject or Message
MATCH (r:Requirement)
WHERE NOT (r)--(:BusinessObject) AND NOT (r)--(:Message)
RETURN r.rid AS requirement_id, r.name AS name
LIMIT 100
```

### 4.3 Execution Flow

```
┌─────────────────────────────────────────────────────┐
│              Phase 1 Execution Flow                 │
├─────────────────────────────────────────────────────┤
│                                                     │
│  START                                              │
│    │                                                │
│    ▼                                                │
│  ┌─────────────────────┐                           │
│  │ Run Category A      │                           │
│  │ (Structural)        │                           │
│  └──────────┬──────────┘                           │
│             │                                       │
│             ▼                                       │
│       Any ERRORS? ──────YES────► Collect & Continue│
│             │                           │          │
│             NO                          │          │
│             │                           │          │
│             ▼                           │          │
│  ┌─────────────────────┐               │          │
│  │ Run Category B      │               │          │
│  │ (Relationships)     │               │          │
│  └──────────┬──────────┘               │          │
│             │                           │          │
│             ▼                           │          │
│       Any ERRORS? ──────YES────► Collect & Continue│
│             │                           │          │
│             NO                          │          │
│             │                           │          │
│             ▼                           │          │
│  ┌─────────────────────┐               │          │
│  │ Run Category C      │               │          │
│  │ (Quality Gates)     │               │          │
│  └──────────┬──────────┘               │          │
│             │                           │          │
│             ▼                           │          │
│       Any WARNINGS? ────YES────► Collect          │
│             │                           │          │
│             │                           │          │
│             ▼                           ▼          │
│  ┌─────────────────────────────────────────────┐  │
│  │         Generate ComplianceReport           │  │
│  │  {                                          │  │
│  │    timestamp: "2025-12-03T10:30:00Z",      │  │
│  │    passed: true/false,                      │  │
│  │    error_count: N,                          │  │
│  │    warning_count: M,                        │  │
│  │    checks: [...],                           │  │
│  │    violations: [...],                       │  │
│  │    warnings: [...]                          │  │
│  │  }                                          │  │
│  └──────────────────────┬──────────────────────┘  │
│                         │                          │
│                         ▼                          │
│                       END                          │
│                                                     │
└─────────────────────────────────────────────────────┘
```

---

## 5. Phase 2: Agent Analysis

### 5.1 Agent Context Injection

The ComplianceReport from Phase 1 is injected into the agent's initial context:

```python
initial_context = f"""
You are analyzing a Neo4j graph database for metamodel compliance.

## Phase 1 Compliance Report
The following automated checks have been performed:

**Overall Status:** {"PASSED" if report.passed else "FAILED"}
**Errors:** {report.error_count}
**Warnings:** {report.warning_count}

### Violations Found:
{format_violations(report.violations)}

### Warnings:
{format_warnings(report.warnings)}

Your task is to:
1. Explain the significance of each finding in business terms
2. Assess the impact on GoBD compliance
3. Provide specific remediation recommendations
4. Answer any follow-up questions about the findings
"""
```

### 5.2 New Tool: validate_schema_compliance

The agent receives a new tool that allows it to re-run compliance checks during its reasoning loop:

```python
@tool
def validate_schema_compliance(
    check_type: Literal["all", "structural", "relationships", "quality_gates", "specific"],
    specific_check: Optional[str] = None
) -> SchemaComplianceReport:
    """
    Run metamodel compliance checks against the current graph state.
    
    Use this tool when you need to:
    - Verify if the graph conforms to the metamodel
    - Check specific constraint categories
    - Re-validate after discussing potential issues
    
    Args:
        check_type: Category of checks to run
            - "all": Run all checks (A, B, C categories)
            - "structural": Only node label and property checks (Category A)
            - "relationships": Only relationship type/direction checks (Category B)
            - "quality_gates": Only semantic quality checks (Category C)
            - "specific": Run a single check by ID
        specific_check: Check ID when check_type="specific" (e.g., "A1", "B2", "C3")
    
    Returns:
        Structured compliance report with:
        - Overall pass/fail status
        - List of violations with details
        - List of warnings
        - Cypher queries used (for audit trail)
    """
```

### 5.3 Agent Capabilities

With the ComplianceReport context and the new tool, the agent can:

| Capability | Example Prompt | Agent Behavior |
|------------|----------------|----------------|
| **Explain Violations** | "Why is the REFERENCES relationship a problem?" | Explains that REFERENCES isn't in the metamodel, suggests RELATES_TO_OBJECT |
| **Impact Assessment** | "What's affected by the orphan requirements?" | Traces connections, explains traceability gaps |
| **Remediation Guidance** | "How do I fix the Q2 warnings?" | Suggests specific Cypher to create USES_OBJECT relationships |
| **What-If Analysis** | "If I delete BusinessObject BO-123, what breaks?" | Queries graph, explains downstream impacts |
| **GoBD Context** | "Are there compliance risks?" | Cross-references goBDRelevant flags with violations |

### 5.4 Example Agent Interaction

```
User: Is the graph compliant with the metamodel?

Agent: [Calls validate_schema_compliance(check_type="all")]

Agent Response:
The graph has structural issues that need attention:

**Critical (2 Errors):**

1. **Invalid Relationship Type:** Found 3 relationships of type `REFERENCES` 
   connecting Requirements to BusinessObjects. This relationship type isn't 
   defined in the metamodel. Based on the context, these should likely be 
   `RELATES_TO_OBJECT` relationships.
   
   To fix: 
   ```cypher
   MATCH (r:Requirement)-[old:REFERENCES]->(b:BusinessObject)
   CREATE (r)-[:RELATES_TO_OBJECT]->(b)
   DELETE old
   ```

2. **Missing Required Property:** 2 Requirement nodes are missing the `rid` 
   property. These cannot be uniquely identified.

**Warnings (3):**

- 5 Requirements have no connections to BusinessObjects or Messages (orphaned)
- 2 Messages don't specify which BusinessObjects they use
- 1 GoBD-relevant Requirement lacks IMPACTS_* relationships

The GoBD warning is concerning for compliance—Requirement R-0042 is flagged 
as GoBD-relevant but doesn't trace to any BusinessObject or Message it impacts. 
This creates an audit gap.

Would you like me to detail the remediation steps for any specific issue?
```

---

## 6. Integration with RequirementGraphAgent

### 6.1 Current Agent Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                   RequirementGraphAgent                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  State Machine:                                                 │
│  planner → agent → tools → increment → report → END            │
│                                                                 │
│  Tools:                                                         │
│  ┌─────────────────────┐ ┌─────────────────────┐               │
│  │ execute_cypher_query│ │ get_database_schema │               │
│  └─────────────────────┘ └─────────────────────┘               │
│  ┌─────────────────────┐ ┌─────────────────────┐               │
│  │ count_nodes_by_label│ │ find_sample_nodes   │               │
│  └─────────────────────┘ └─────────────────────┘               │
│                                                                 │
│  Workflow:                                                      │
│  1. Planning — LLM generates analysis plan                     │
│  2. Agent Loop — Iteratively calls tools                       │
│  3. Report — Synthesizes findings                              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 6.2 Extended Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│              Extended RequirementGraphAgent                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  NEW: Pre-flight Compliance Check (Optional)                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ MetamodelValidator.run_all_checks()                     │   │
│  │ → ComplianceReport injected into agent context          │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  State Machine: (unchanged)                                    │
│  planner → agent → tools → increment → report → END            │
│                                                                 │
│  Tools: (extended)                                             │
│  ┌─────────────────────┐ ┌─────────────────────┐               │
│  │ execute_cypher_query│ │ get_database_schema │               │
│  └─────────────────────┘ └─────────────────────┘               │
│  ┌─────────────────────┐ ┌─────────────────────┐               │
│  │ count_nodes_by_label│ │ find_sample_nodes   │               │
│  └─────────────────────┘ └─────────────────────┘               │
│  ┌─────────────────────────────────────────────┐               │
│  │ validate_schema_compliance (NEW)            │  ◄── NEW     │
│  └─────────────────────────────────────────────┘               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 6.3 Integration Points

```python
class ExtendedRequirementGraphAgent:
    def __init__(self, neo4j_driver, llm, metamodel_config):
        self.validator = MetamodelValidator(neo4j_driver, metamodel_config)
        self.agent = RequirementGraphAgent(neo4j_driver, llm)
        
        # Register new tool
        self.agent.add_tool(self._create_compliance_tool())
    
    def process_with_compliance(self, query: str, run_preflight: bool = True):
        """
        Process a query with optional pre-flight compliance check.
        """
        initial_context = ""
        
        if run_preflight:
            # Phase 1: Run deterministic checks
            report = self.validator.run_all_checks()
            initial_context = self._format_report_for_context(report)
            
            # Optionally halt on critical errors
            if report.error_count > 0 and self.config.halt_on_errors:
                return self._create_error_response(report)
        
        # Phase 2: Run agent with compliance context
        return self.agent.process_requirement_stream(
            query=query,
            initial_context=initial_context
        )
```

---

## 7. Implementation Specification

### 7.1 MetamodelValidator Class

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal, Optional
from neo4j import Driver


class Severity(Enum):
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
    """Complete compliance report from Phase 1."""
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
                }
                for r in self.results
            ]
        }


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
    
    def __init__(self, driver: Driver):
        self.driver = driver
    
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
        
        # Category C: Quality Gates
        results.append(self.check_c1_orphan_requirements())
        results.append(self.check_c2_message_content())
        results.append(self.check_c3_gobd_traceability())
        
        # Aggregate results
        error_count = sum(1 for r in results if not r.passed and r.severity == Severity.ERROR)
        warning_count = sum(1 for r in results if not r.passed and r.severity == Severity.WARNING)
        
        return ComplianceReport(
            timestamp=datetime.utcnow(),
            passed=(error_count == 0),
            error_count=error_count,
            warning_count=warning_count,
            checks_performed=[r.check_id for r in results],
            results=results,
        )
    
    def check_a1_node_labels(self) -> CheckResult:
        """Verify all nodes have allowed labels."""
        query = """
        MATCH (n)
        WITH labels(n) AS node_labels, n
        WHERE NONE(label IN node_labels WHERE label IN $allowed_labels)
        RETURN labels(n) AS labels, count(n) AS count
        """
        # ... implementation
    
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
             AND rel_type IN ['REFINES','DEPENDS_ON','TRACES_TO'])
            OR
            (source_label = 'Requirement' AND target_label = 'BusinessObject' 
             AND rel_type IN ['RELATES_TO_OBJECT','IMPACTS_OBJECT'])
            OR
            (source_label = 'Requirement' AND target_label = 'Message' 
             AND rel_type IN ['RELATES_TO_MESSAGE','IMPACTS_MESSAGE'])
            OR
            (source_label = 'Message' AND target_label = 'BusinessObject' 
             AND rel_type IN ['USES_OBJECT','PRODUCES_OBJECT'])
        )
        RETURN source_label, rel_type, target_label, count(*) AS violation_count
        """
        # ... implementation
    
    # ... additional check methods
```

### 7.2 LangChain Tool Wrapper

```python
from langchain.tools import tool
from pydantic import BaseModel, Field


class ComplianceCheckInput(BaseModel):
    check_type: Literal["all", "structural", "relationships", "quality_gates", "specific"] = Field(
        description="Category of checks to run"
    )
    specific_check: Optional[str] = Field(
        default=None,
        description="Check ID when check_type='specific' (e.g., 'A1', 'B2', 'C3')"
    )


@tool(args_schema=ComplianceCheckInput)
def validate_schema_compliance(check_type: str, specific_check: Optional[str] = None) -> str:
    """
    Run metamodel compliance checks against the current graph state.
    
    Use this tool when you need to verify if the graph conforms to the 
    metamodel, check specific constraint categories, or re-validate 
    after discussing potential issues.
    
    Returns a structured compliance report with pass/fail status,
    violations, warnings, and the Cypher queries used.
    """
    validator = get_validator()  # Get from dependency injection
    
    if check_type == "all":
        report = validator.run_all_checks()
    elif check_type == "structural":
        report = validator.run_structural_checks()
    elif check_type == "relationships":
        report = validator.run_relationship_checks()
    elif check_type == "quality_gates":
        report = validator.run_quality_gate_checks()
    elif check_type == "specific" and specific_check:
        report = validator.run_specific_check(specific_check)
    else:
        return "Invalid check_type or missing specific_check parameter"
    
    return format_report_for_agent(report)
```

---

## 8. Compliance Report Schema

### 8.1 JSON Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MetamodelComplianceReport",
  "type": "object",
  "required": ["timestamp", "passed", "error_count", "warning_count", "checks_performed", "results"],
  "properties": {
    "timestamp": {
      "type": "string",
      "format": "date-time",
      "description": "ISO 8601 timestamp of when the report was generated"
    },
    "passed": {
      "type": "boolean",
      "description": "True if no errors (warnings don't cause failure)"
    },
    "error_count": {
      "type": "integer",
      "minimum": 0
    },
    "warning_count": {
      "type": "integer",
      "minimum": 0
    },
    "checks_performed": {
      "type": "array",
      "items": { "type": "string" },
      "description": "List of check IDs that were executed"
    },
    "results": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["check_id", "check_name", "passed", "severity", "message"],
        "properties": {
          "check_id": { "type": "string" },
          "check_name": { "type": "string" },
          "passed": { "type": "boolean" },
          "severity": { "enum": ["error", "warning", "info"] },
          "message": { "type": "string" },
          "violations": {
            "type": "array",
            "items": { "type": "object" }
          },
          "query_used": { "type": "string" }
        }
      }
    }
  }
}
```

### 8.2 Example Report

```json
{
  "timestamp": "2025-12-03T14:30:00Z",
  "passed": false,
  "error_count": 2,
  "warning_count": 3,
  "checks_performed": ["A1", "A2", "A3", "B1", "B2", "C1", "C2", "C3"],
  "results": [
    {
      "check_id": "A1",
      "check_name": "validate_node_labels",
      "passed": true,
      "severity": "error",
      "message": "All nodes have valid labels",
      "violations": [],
      "query_used": "MATCH (n) WITH labels(n) AS node_labels..."
    },
    {
      "check_id": "B1",
      "check_name": "validate_relationship_types",
      "passed": false,
      "severity": "error",
      "message": "Found 3 relationships with invalid types",
      "violations": [
        {
          "relationship_type": "REFERENCES",
          "count": 3,
          "sample": "(:Requirement {rid:'R-0042'})-[:REFERENCES]->(:BusinessObject {boid:'BO-Rechnung'})"
        }
      ],
      "query_used": "MATCH ()-[r]->() WHERE NOT type(r) IN $allowed_types..."
    },
    {
      "check_id": "C1",
      "check_name": "validate_orphan_requirements",
      "passed": false,
      "severity": "warning",
      "message": "Found 5 requirements with no connections",
      "violations": [
        { "rid": "R-0101", "name": "Legacy requirement" },
        { "rid": "R-0102", "name": "Placeholder" }
      ],
      "query_used": "MATCH (r:Requirement) WHERE NOT (r)--(:BusinessObject)..."
    }
  ]
}
```

---

## 9. Usage Patterns

### 9.1 Standalone Validation (CI/CD)

```python
# In CI/CD pipeline or batch job
from metamodel_validator import MetamodelValidator
from neo4j import GraphDatabase

driver = GraphDatabase.driver(uri, auth=(user, password))
validator = MetamodelValidator(driver)

report = validator.run_all_checks()

if not report.passed:
    print(f"Validation failed with {report.error_count} errors")
    for result in report.results:
        if not result.passed and result.severity.value == "error":
            print(f"  [{result.check_id}] {result.message}")
    sys.exit(1)
else:
    print("Metamodel validation passed")
    if report.warning_count > 0:
        print(f"  ({report.warning_count} warnings)")
```

### 9.2 Agent-Driven Analysis

```python
# Interactive analysis with agent
agent = ExtendedRequirementGraphAgent(driver, llm, metamodel_config)

# Option 1: Full pipeline with pre-flight
response = agent.process_with_compliance(
    query="Analyze the graph for compliance issues and suggest fixes",
    run_preflight=True
)

# Option 2: Let agent decide when to validate
response = agent.process_requirement_stream(
    query="Check if the graph is ready for the GoBD audit"
)
# Agent will call validate_schema_compliance tool as needed
```

### 9.3 Targeted Validation

```python
# Check only specific categories
report = validator.run_checks(categories=["structural"])

# Check single rule
result = validator.run_specific_check("C3")  # GoBD traceability only
```

---

## 10. Future Extensions

### 10.1 Planned Enhancements

| Enhancement | Description | Priority |
|-------------|-------------|----------|
| **Cardinality Rules** | Min/max relationship counts per node | High |
| **Property Type Validation** | Ensure properties have correct types | Medium |
| **Custom Rule DSL** | Define new checks without code changes | Medium |
| **Historical Comparison** | Diff reports over time | Low |
| **Auto-Remediation** | Generate fix scripts automatically | Low |

### 10.2 Extensibility Points

The architecture supports adding new checks by:

1. Adding entries to `ALLOWED_RELATIONSHIPS` or `REQUIRED_PROPERTIES`
2. Implementing new `check_*` methods in `MetamodelValidator`
3. Registering checks in the appropriate category

```python
# Example: Adding a new quality gate
def check_c4_requirement_descriptions(self) -> CheckResult:
    """Verify all requirements have non-empty descriptions."""
    query = """
    MATCH (r:Requirement)
    WHERE r.text IS NULL OR r.text = ''
    RETURN r.rid AS rid, r.name AS name
    """
    # ... implementation
```

### 10.3 Integration Opportunities

- **Neo4j Triggers:** Run validation on graph mutations
- **GraphQL Layer:** Expose compliance status via API
- **Monitoring Dashboard:** Real-time compliance metrics
- **Export Formats:** Generate compliance reports in PDF/HTML for audits

---

## Appendix A: Complete Check Reference

| ID | Name | Category | Severity | Description |
|----|------|----------|----------|-------------|
| A1 | validate_node_labels | Structural | ERROR | All nodes must have metamodel-defined labels |
| A2 | validate_unique_constraints | Structural | ERROR | ID properties must be unique |
| A3 | validate_required_properties | Structural | ERROR | Required properties must exist |
| B1 | validate_relationship_types | Relationships | ERROR | Only allowed relationship types |
| B2 | validate_relationship_directions | Relationships | ERROR | Correct source→target combinations |
| B3 | validate_no_invalid_self_loops | Relationships | ERROR | No invalid self-referential edges |
| C1 | validate_orphan_requirements | Quality Gates | WARNING | Requirements should have connections |
| C2 | validate_message_content | Quality Gates | WARNING | Messages should use BusinessObjects |
| C3 | validate_gobd_traceability | Quality Gates | WARNING | GoBD items need IMPACTS_* relations |

---

## Appendix B: Cypher Query Reference

### B.1 Find All Invalid Relationships

```cypher
MATCH (a)-[r]->(b)
WITH a, r, b,
     labels(a)[0] AS src,
     type(r) AS rel,
     labels(b)[0] AS tgt
WHERE NOT (
    (src = 'Requirement' AND tgt = 'Requirement' AND rel IN ['REFINES','DEPENDS_ON','TRACES_TO'])
    OR (src = 'Requirement' AND tgt = 'BusinessObject' AND rel IN ['RELATES_TO_OBJECT','IMPACTS_OBJECT'])
    OR (src = 'Requirement' AND tgt = 'Message' AND rel IN ['RELATES_TO_MESSAGE','IMPACTS_MESSAGE'])
    OR (src = 'Message' AND tgt = 'BusinessObject' AND rel IN ['USES_OBJECT','PRODUCES_OBJECT'])
)
RETURN src, rel, tgt, count(*) AS violations
ORDER BY violations DESC
```

### B.2 GoBD Compliance Overview

```cypher
MATCH (r:Requirement)
WHERE r.goBDRelevant = true
OPTIONAL MATCH (r)-[:IMPACTS_OBJECT]->(b:BusinessObject)
OPTIONAL MATCH (r)-[:IMPACTS_MESSAGE]->(m:Message)
WITH r, collect(DISTINCT b.name) AS impacted_objects, collect(DISTINCT m.name) AS impacted_messages
RETURN 
    r.rid AS requirement,
    r.name AS name,
    impacted_objects,
    impacted_messages,
    CASE 
        WHEN size(impacted_objects) = 0 AND size(impacted_messages) = 0 
        THEN 'NO_TRACEABILITY' 
        ELSE 'OK' 
    END AS status
```

---

*Document generated for the GraphRAG in Practice project, December 2025*
