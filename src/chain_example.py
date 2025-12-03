"""
Simple LangChain Example - One-Shot Requirement Analysis

A demonstration chain that shows a simple, linear approach to requirement analysis.
This intentionally lacks the iterative reasoning and tool-calling capabilities
of the LangGraph agent, to demonstrate why the agent approach is superior.

The chain follows a 4-step process:
1. Plan: Generate a verification plan from the requirement
2. Query: Generate Cypher queries based on the plan and schema
3. Execute: Run the queries against Neo4j
4. Analyze: Generate a structured analysis from the results
"""

import os
import re
from typing import List, Optional
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from src.core.neo4j_utils import Neo4jConnection


# ============================================================================
# Pydantic Output Models (based on data/output_schema.json)
# ============================================================================

class Metadata(BaseModel):
    """Metadata about the requirement."""
    entity_name: str = Field(description="Internal model name for this requirement")
    requirement_type: str = Field(description="Classification (e.g., Business Requirement, Technical Constraint)")
    status: str = Field(description="Current status in the system (e.g., valid, draft)")
    source_origin: str = Field(description="Origin system of the data (e.g., Neo4J, XMI-Import)")


class Identification(BaseModel):
    """Clear identification and mirroring of the user input/requirement."""
    requirement_text: str = Field(description="The verbatim text of the requirement to be analyzed")
    metadata: Metadata = Field(description="Metadata about the requirement")


class KnowledgeRetrieval(BaseModel):
    """Strict separation of facts found in the knowledge graph versus missing information."""
    found_facts: List[str] = Field(description="List of specific nodes or documentation found in the database")
    missing_elements: List[str] = Field(description="What was searched for but NOT found")


class ComplianceCheck(BaseModel):
    """A single compliance check result."""
    criteria: str = Field(description="The specific aspect being checked")
    result: str = Field(description="Result of the check (e.g., Yes/No, Met/Not Met)")
    observation: str = Field(description="Detailed observation explaining the result")


class RiskAssessment(BaseModel):
    """Risk assessment for the requirement."""
    level: str = Field(description="Risk level (High, Medium, Low)")
    justification: str = Field(description="Explanation of the risk")


class Analysis(BaseModel):
    """Logic and reasoning layer where the model interprets the facts."""
    compliance_matrix: List[ComplianceCheck] = Field(description="Matrix of compliance checks")
    risk_assessment: RiskAssessment = Field(description="Risk assessment for the requirement")


class Evaluation(BaseModel):
    """Final verdict on whether the requirement is currently satisfied."""
    verdict: str = Field(description="Satisfied, Not Satisfied, or Partially Satisfied")
    summary_reasoning: str = Field(description="Synthesis of why this verdict was reached")


class Recommendations(BaseModel):
    """Concrete, actionable next steps for the user."""
    action_items: List[str] = Field(description="List of specific modeling tasks, relationships to create, or attributes to add")


class ConversationalSummary(BaseModel):
    """Short, catchy conclusion suitable for a chat interface."""
    message: str = Field(description="A natural language summary of the result for the end user")


class ChainOutput(BaseModel):
    """Complete structured output from the chain."""
    identification: Identification = Field(description="Identification of the requirement")
    knowledge_retrieval: KnowledgeRetrieval = Field(description="Findings from the knowledge graph")
    analysis: Analysis = Field(description="Analysis and interpretation")
    evaluation: Evaluation = Field(description="Final evaluation/verdict")
    recommendations: Recommendations = Field(description="Recommended actions")
    conversational_summary: ConversationalSummary = Field(description="User-friendly summary")


# ============================================================================
# Simple Chain Implementation
# ============================================================================

class SimpleChain:
    """
    Simple one-shot chain for requirement analysis.

    Uses a chat-based LLM interaction with Pydantic structured output.
    This is intentionally simple to contrast with the iterative LangGraph agent.
    """

    # Domain context for the system prompt
    DOMAIN_CONTEXT = """You are an analyst for a car rental business requirement traceability system.
The system uses a Neo4j graph database with the following metamodel:

Node Types:
- Requirement: Business requirements (properties: rid, name, text, type, priority, status, source, valueStream, goBDRelevant)
- BusinessObject: Business domain entities (properties: boid, name, description, domain, owner)
- Message: System messages (properties: mid, name, description, direction, format, protocol, version)

Relationships:
- Requirement → Requirement: REFINES, DEPENDS_ON, TRACES_TO
- Requirement → BusinessObject: RELATES_TO_OBJECT, IMPACTS_OBJECT
- Requirement → Message: RELATES_TO_MESSAGE, IMPACTS_MESSAGE
- Message → BusinessObject: USES_OBJECT, PRODUCES_OBJECT

GoBD (German accounting compliance) is a key concern for this system.

IMPORTANT Neo4j Query Guidelines:
- Use elementId(node) instead of id(node) - the id() function is deprecated
- Use property-based queries over ID-based queries
- Always use LIMIT for potentially large result sets"""

    def __init__(
        self,
        neo4j_connection: Neo4jConnection,
        model: Optional[str] = None,
        temperature: Optional[float] = None
    ):
        """
        Initialize the simple chain.

        Args:
            neo4j_connection: Active Neo4j connection
            model: LLM model to use (defaults to env variable or gpt-4o-mini)
            temperature: LLM temperature (defaults to env variable or 0.2)
        """
        self.neo4j = neo4j_connection
        self.model = model or os.getenv('LLM_MODEL', 'gpt-4o-mini')
        self.temperature = temperature if temperature is not None else float(os.getenv('LLM_TEMPERATURE', '0.2'))

        self.llm = ChatOpenAI(
            model=self.model,
            temperature=self.temperature,
            openai_api_key=os.getenv('OPENAI_API_KEY')
        )

        # Conversation history
        self.messages: List = []

    def run(self, requirement: str) -> ChainOutput:
        """
        Execute the chain and return structured output.

        Args:
            requirement: The requirement text to analyze

        Returns:
            ChainOutput with structured analysis results
        """
        # Reset conversation
        self.messages = []

        # Step 1: Generate plan
        plan = self._step1_generate_plan(requirement)

        # Step 2: Generate queries
        schema = self.neo4j.get_database_schema()
        queries = self._step2_generate_queries(plan, schema)

        # Step 3: Execute queries
        results = self._step3_execute_queries(queries)

        # Step 4: Generate structured analysis
        output = self._step4_generate_analysis(requirement, results)

        return output

    def _step1_generate_plan(self, requirement: str) -> str:
        """
        Step 1: Generate a verification plan from the requirement.

        Args:
            requirement: The requirement text

        Returns:
            Plan text
        """
        self.messages = [
            SystemMessage(content=self.DOMAIN_CONTEXT),
            HumanMessage(content=f"""Analyze this requirement and create a verification plan.

Requirement: {requirement}

Create a brief plan (3-5 steps) to verify this requirement against the database.
Focus on what data needs to be retrieved and what aspects need verification.""")
        ]

        response = self.llm.invoke(self.messages)
        self.messages.append(AIMessage(content=response.content))

        return response.content

    def _step2_generate_queries(self, plan: str, schema: dict) -> List[str]:
        """
        Step 2: Generate Cypher queries based on the plan.

        Args:
            plan: The verification plan
            schema: Database schema dict

        Returns:
            List of Cypher queries
        """
        # Format schema for the prompt
        schema_str = self._format_schema(schema)

        self.messages.append(HumanMessage(content=f"""Based on your plan, generate Cypher queries to retrieve the necessary data.

Database Schema:
{schema_str}

Generate 2-4 Cypher queries that will retrieve the data needed to verify the requirement.
Format each query in a ```cypher code block."""))

        response = self.llm.invoke(self.messages)
        self.messages.append(AIMessage(content=response.content))

        # Extract queries from response
        queries = self._extract_cypher_queries(response.content)

        return queries

    def _step3_execute_queries(self, queries: List[str]) -> List[dict]:
        """
        Step 3: Execute the Cypher queries.

        Args:
            queries: List of Cypher queries

        Returns:
            List of result dicts with query, success, and results/error
        """
        results = []

        for query in queries:
            try:
                query_results = self.neo4j.execute_query(query)
                results.append({
                    'query': query,
                    'success': True,
                    'results': query_results[:20],  # Limit results
                    'count': len(query_results)
                })
            except Exception as e:
                results.append({
                    'query': query,
                    'success': False,
                    'error': str(e)
                })

        return results

    def _step4_generate_analysis(self, requirement: str, query_results: List[dict]) -> ChainOutput:
        """
        Step 4: Generate structured analysis using Pydantic output.

        Args:
            requirement: Original requirement
            query_results: Results from query execution

        Returns:
            ChainOutput with structured analysis
        """
        # Format results for the prompt
        results_str = self._format_query_results(query_results)

        self.messages.append(HumanMessage(content=f"""Based on the query results, provide a comprehensive analysis.

Query Results:
{results_str}

Analyze these results and provide your final assessment of whether the requirement is met."""))

        # Use structured output for the final analysis
        structured_llm = self.llm.with_structured_output(ChainOutput)

        # Add system message for structured output
        analysis_messages = [
            SystemMessage(content=f"""You are analyzing requirement compliance for a car rental business system.

Original Requirement: {requirement}

Provide a comprehensive structured analysis based on the conversation and query results.
Fill in all fields of the output structure completely."""),
            *self.messages
        ]

        output = structured_llm.invoke(analysis_messages)

        return output

    def _format_schema(self, schema: dict) -> str:
        """Format schema dict as readable string."""
        lines = []
        lines.append(f"Node Labels: {', '.join(schema.get('node_labels', []))}")
        lines.append(f"Relationship Types: {', '.join(schema.get('relationship_types', []))}")
        lines.append(f"Properties: {', '.join(schema.get('property_keys', [])[:20])}")
        if len(schema.get('property_keys', [])) > 20:
            lines.append(f"  ... and {len(schema['property_keys']) - 20} more")
        return '\n'.join(lines)

    def _extract_cypher_queries(self, text: str) -> List[str]:
        """Extract Cypher queries from markdown code blocks."""
        pattern = r'```(?:cypher)?\s*(.*?)```'
        matches = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)
        queries = [m.strip() for m in matches if m.strip()]
        return queries

    def _format_query_results(self, results: List[dict]) -> str:
        """Format query results for the prompt."""
        lines = []
        for i, result in enumerate(results, 1):
            lines.append(f"\n--- Query {i} ---")
            lines.append(f"Query: {result['query']}")
            if result['success']:
                lines.append(f"Status: Success ({result['count']} results)")
                if result['results']:
                    lines.append("Results:")
                    for j, r in enumerate(result['results'][:10], 1):
                        lines.append(f"  {j}. {r}")
                    if result['count'] > 10:
                        lines.append(f"  ... and {result['count'] - 10} more")
                else:
                    lines.append("Results: No data returned")
            else:
                lines.append(f"Status: Failed")
                lines.append(f"Error: {result['error']}")
        return '\n'.join(lines)
