"""
LangChain Agent for Requirement Processing
Handles the analysis of requirements against the Neo4j database using LLM.
"""

import os
from typing import Dict, Any, List, Optional
from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_react_agent
from langchain.tools import Tool
from langchain.prompts import PromptTemplate
from langchain.schema import AgentAction, AgentFinish

from src.neo4j_utils import Neo4jConnection


class RequirementAgent:
    """
    LangChain agent that processes requirements and queries Neo4j database.
    Uses ReAct pattern to iteratively refine queries and analyze results.
    """

    def __init__(self, neo4j_connection: Neo4jConnection, llm_model: Optional[str] = None, temperature: float = 0.0):
        """
        Initialize the requirement agent.

        Args:
            neo4j_connection: Active Neo4j database connection
            llm_model: LLM model to use (defaults to env variable)
            temperature: LLM temperature setting
        """
        self.neo4j = neo4j_connection
        self.llm_model = llm_model or os.getenv('LLM_MODEL', 'gpt-4-turbo-preview')
        self.temperature = temperature

        # Initialize LLM
        self.llm = ChatOpenAI(
            model=self.llm_model,
            temperature=self.temperature,
            openai_api_key=os.getenv('OPENAI_API_KEY')
        )

        # Get database schema for context
        self.schema = self.neo4j.get_database_schema()

    def create_cypher_query_chain(self) -> LLMChain:
        """
        Create a chain for generating Cypher queries from requirements.

        Returns:
            LLMChain for Cypher query generation
        """
        template = """You are an expert in Neo4j Cypher query language. Given a requirement and database schema, generate an appropriate Cypher query.

Database Metamodel:
The database follows a specific metamodel for requirement traceability and impact analysis:

Node Types:
1. Requirement - Represents business/system requirements
   - Properties: rid (unique ID), name, text, type, priority, status, source, valueStream, goBDRelevant

2. BusinessObject - Represents business domain objects
   - Properties: boid (unique ID), name, description, domain, owner

3. Message - Represents system messages/communications
   - Properties: mid (unique ID), name, description, direction, format, protocol, version

Relationship Types:
- (Requirement)-[:REFINES]->(Requirement) - Requirement refinement hierarchy
- (Requirement)-[:DEPENDS_ON]->(Requirement) - Requirement dependencies
- (Requirement)-[:TRACES_TO]->(Requirement) - Traceability links
- (Requirement)-[:RELATES_TO_OBJECT]->(BusinessObject) - Requirement references an object
- (Requirement)-[:IMPACTS_OBJECT]->(BusinessObject) - Requirement impacts an object
- (Requirement)-[:RELATES_TO_MESSAGE]->(Message) - Requirement references a message
- (Requirement)-[:IMPACTS_MESSAGE]->(Message) - Requirement impacts a message
- (Message)-[:USES_OBJECT]->(BusinessObject) - Message uses object data
- (Message)-[:PRODUCES_OBJECT]->(BusinessObject) - Message produces/creates object

Current Database Schema:
- Node Labels: {node_labels}
- Relationship Types: {relationship_types}
- Property Keys: {property_keys}

Requirement to Analyze: {requirement}

Generate a Cypher query to check this requirement against the database. The query should:
1. Be syntactically correct Neo4j Cypher
2. Leverage the metamodel structure (Requirement, BusinessObject, Message nodes and their relationships)
3. Return relevant data to answer the requirement
4. Include appropriate MATCH, WHERE, and RETURN clauses
5. Use LIMIT 100 if the query might return many results
6. Consider using pattern matching to explore relationships when analyzing impacts or dependencies

Important: Only output the Cypher query, nothing else. No explanations or markdown formatting.

Cypher Query:"""

        prompt = PromptTemplate(
            input_variables=["requirement", "node_labels", "relationship_types", "property_keys"],
            template=template
        )

        return LLMChain(llm=self.llm, prompt=prompt)

    def create_analysis_chain(self) -> LLMChain:
        """
        Create a chain for analyzing query results against requirements.

        Returns:
            LLMChain for result analysis
        """
        template = """You are an expert analyst for requirement traceability and compliance checking in a car rental business system.

Context: The database contains Requirements, BusinessObjects, and Messages with their relationships,
supporting impact analysis, compliance checking, and requirement traceability (especially for GoBD - German accounting compliance).

Original Requirement: {requirement}

Cypher Query Executed: {query}

Query Results: {results}

Based on the query results, provide a comprehensive analysis that includes:

1. **Summary**: A clear, concise answer to the requirement question
2. **Findings**: Key data points and evidence from the query results
3. **Compliance Status**: Whether the requirement is met, partially met, or not met
4. **Impact Assessment**: If applicable, which business objects, messages, or other requirements are affected
5. **Recommendations**: Specific actions or considerations, especially for compliance requirements (GoBD, etc.)
6. **Risk Level**: Assessment of any risks or concerns identified

Format your analysis in clear sections with these headers. Be specific and reference actual data from the results.

Analysis:"""

        prompt = PromptTemplate(
            input_variables=["requirement", "query", "results"],
            template=template
        )

        return LLMChain(llm=self.llm, prompt=prompt)

    def refine_requirement(self, requirement: str) -> str:
        """
        Refine and clarify the requirement for better processing.

        Args:
            requirement: Original requirement text

        Returns:
            Refined requirement text
        """
        template = """You are an expert at understanding business requirements. Given a requirement, refine it to be:
1. Clear and unambiguous
2. Specific about what needs to be checked
3. Structured for database querying

Original Requirement: {requirement}

Provide a refined version that clearly states what needs to be verified in the database.

Refined Requirement:"""

        prompt = PromptTemplate(input_variables=["requirement"], template=template)
        chain = LLMChain(llm=self.llm, prompt=prompt)

        result = chain.run(requirement=requirement)
        return result.strip()

    def generate_cypher_query(self, requirement: str) -> str:
        """
        Generate a Cypher query for the given requirement.

        Args:
            requirement: Requirement text

        Returns:
            Generated Cypher query
        """
        chain = self.create_cypher_query_chain()

        query = chain.run(
            requirement=requirement,
            node_labels=", ".join(self.schema['node_labels']) if self.schema['node_labels'] else "No labels found",
            relationship_types=", ".join(self.schema['relationship_types']) if self.schema['relationship_types'] else "No relationships found",
            property_keys=", ".join(self.schema['property_keys']) if self.schema['property_keys'] else "No properties found"
        )

        return query.strip()

    def analyze_results(self, requirement: str, query: str, results: List[Dict[str, Any]]) -> str:
        """
        Analyze query results against the requirement.

        Args:
            requirement: Original requirement
            query: Cypher query that was executed
            results: Query results

        Returns:
            Analysis text
        """
        chain = self.create_analysis_chain()

        # Format results for better readability
        if not results:
            results_str = "No data found matching the query criteria."
        else:
            results_str = "\n".join([str(record) for record in results[:20]])  # Limit to first 20 results
            if len(results) > 20:
                results_str += f"\n... and {len(results) - 20} more results"

        analysis = chain.run(
            requirement=requirement,
            query=query,
            results=results_str
        )

        return analysis.strip()

    def process_requirement(self, requirement: str, refine: bool = True) -> Dict[str, Any]:
        """
        Process a requirement end-to-end.

        Args:
            requirement: Requirement text to process
            refine: Whether to refine the requirement first

        Returns:
            Dictionary containing the full processing results
        """
        print(f"\n{'='*80}")
        print(f"Processing Requirement:")
        print(f"{requirement}")
        print(f"{'='*80}\n")

        # Step 1: Refine requirement (optional)
        refined_requirement = requirement
        if refine:
            print("Step 1: Refining requirement...")
            refined_requirement = self.refine_requirement(requirement)
            print(f"Refined: {refined_requirement}\n")

        # Step 2: Generate Cypher query
        print("Step 2: Generating Cypher query...")
        cypher_query = self.generate_cypher_query(refined_requirement)
        print(f"Query: {cypher_query}\n")

        # Step 3: Execute query
        print("Step 3: Executing query on Neo4j...")
        try:
            results = self.neo4j.execute_query(cypher_query)
            print(f"✓ Query executed successfully. Found {len(results)} results.\n")
        except Exception as e:
            print(f"✗ Query execution failed: {str(e)}\n")
            results = []

        # Step 4: Analyze results
        print("Step 4: Analyzing results...")
        analysis = self.analyze_results(refined_requirement, cypher_query, results)
        print(f"Analysis complete.\n")

        return {
            'original_requirement': requirement,
            'refined_requirement': refined_requirement,
            'cypher_query': cypher_query,
            'result_count': len(results),
            'results': results,
            'analysis': analysis
        }


def create_requirement_agent(neo4j_connection: Neo4jConnection) -> RequirementAgent:
    """
    Create a requirement agent with configuration from environment variables.

    Args:
        neo4j_connection: Active Neo4j connection

    Returns:
        Configured RequirementAgent instance
    """
    llm_model = os.getenv('LLM_MODEL', 'gpt-4-turbo-preview')
    temperature = float(os.getenv('LLM_TEMPERATURE', '0.0'))

    return RequirementAgent(
        neo4j_connection=neo4j_connection,
        llm_model=llm_model,
        temperature=temperature
    )
