"""
LangGraph-Based Requirement Agent
An iterative, reasoning agent that can plan, query, and analyze Neo4j data
to answer complex requirements with multiple investigation rounds.
"""

import os
from typing import Dict, Any, List, TypedDict, Annotated, Sequence
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages
from langchain_core.runnables import RunnableConfig

from src.core.neo4j_utils import Neo4jConnection
from src.core.config import load_config
from src.core.metamodel_validator import MetamodelValidator, ComplianceReport, Severity

# Optional: Web search capability
try:
    from langchain_tavily import TavilySearch
    TAVILY_AVAILABLE = os.getenv('TAVILY_API_KEY') is not None
except ImportError:
    TAVILY_AVAILABLE = False
    TavilySearch = None


# ============================================================================
# Helper Functions
# ============================================================================

def format_report_for_agent(report: ComplianceReport) -> str:
    """
    Format a ComplianceReport as a structured string for the LLM agent.

    This provides the agent with all the information needed to explain
    violations, assess impact, and suggest remediations.
    """
    lines = [
        "## Metamodel Compliance Report",
        f"**Generated:** {report.timestamp.isoformat()}",
        f"**Overall Status:** {'PASSED' if report.passed else 'FAILED'}",
        f"**Errors:** {report.error_count} | **Warnings:** {report.warning_count}",
        "",
    ]

    if report.error_count > 0:
        lines.append("### Critical Violations (Errors)")
        for result in report.results:
            if not result.passed and result.severity == Severity.ERROR:
                lines.append(f"\n**[{result.check_id}] {result.check_name}**")
                lines.append(f"- Status: FAILED")
                lines.append(f"- Message: {result.message}")
                if result.violations:
                    lines.append("- Violations:")
                    for v in result.violations[:5]:  # Limit to first 5
                        lines.append(f"  - {v}")
                    if len(result.violations) > 5:
                        lines.append(f"  - ... and {len(result.violations) - 5} more")

    if report.warning_count > 0:
        lines.append("\n### Quality Warnings")
        for result in report.results:
            if not result.passed and result.severity == Severity.WARNING:
                lines.append(f"\n**[{result.check_id}] {result.check_name}**")
                lines.append(f"- Message: {result.message}")
                if result.violations:
                    lines.append("- Items:")
                    for v in result.violations[:5]:
                        lines.append(f"  - {v}")
                    if len(result.violations) > 5:
                        lines.append(f"  - ... and {len(result.violations) - 5} more")

    if report.passed and report.warning_count == 0:
        lines.append("All checks passed successfully. The graph conforms to the metamodel.")

    return "\n".join(lines)


# ============================================================================
# Agent State Definition
# ============================================================================

class AgentState(TypedDict):
    """State for the requirement analysis agent."""
    # Messages for the conversation
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # Requirement being analyzed
    requirement: str

    # Agent's plan
    plan: str

    # Queries executed so far
    queries_executed: List[Dict[str, Any]]

    # Current iteration count
    iteration: int

    # Maximum iterations allowed
    max_iterations: int

    # Final analysis result
    analysis: str

    # Whether the agent is done
    is_complete: bool


# ============================================================================
# Agent Tools
# ============================================================================

class Neo4jTools:
    """Tools for interacting with Neo4j database and external resources."""

    def __init__(self, neo4j_connection: Neo4jConnection, enable_web_search: bool = True):
        self.neo4j = neo4j_connection
        self.schema = neo4j_connection.get_database_schema()
        self.validator = MetamodelValidator(neo4j_connection)

        # Initialize web search if available and enabled
        self.web_search_tool = None
        if enable_web_search and TAVILY_AVAILABLE and TavilySearch:
            self.web_search_tool = TavilySearch(
                max_results=5,
                topic="general",
                include_answer=True,
            )

    def get_tools(self):
        """Return list of tools for the agent."""

        @tool
        def execute_cypher_query(query: str) -> str:
            """
            Execute a Cypher query against the Neo4j database and return results.

            Args:
                query: A valid Cypher query string

            Returns:
                String representation of query results (up to 50 records)

            Use this tool to:
            - Find requirements with specific properties (e.g., GoBD-relevant requirements)
            - Explore relationships between Requirements, BusinessObjects, and Messages
            - Count nodes or relationships
            - Investigate impacts and dependencies

            Example queries (use elementId() not id()):
            - MATCH (r:Requirement {goBDRelevant: true}) RETURN elementId(r) AS nodeId, r.name, r.text LIMIT 10
            - MATCH (r:Requirement)-[:IMPACTS_OBJECT]->(bo:BusinessObject) RETURN r.name, bo.name LIMIT 20
            - MATCH (r:Requirement) WHERE r.element_id = 'some_id' RETURN r  // Use property-based matching
            """
            try:
                results = self.neo4j.execute_query(query)

                if not results:
                    return "Query executed successfully but returned no results."

                # Limit results to avoid overwhelming the LLM
                limited_results = results[:50]

                # Format results nicely
                formatted = []
                for i, record in enumerate(limited_results, 1):
                    formatted.append(f"Record {i}: {record}")

                result_str = "\n".join(formatted)

                if len(results) > 50:
                    result_str += f"\n\n... and {len(results) - 50} more results (showing first 50)"

                return result_str

            except Exception as e:
                return f"Error executing query: {str(e)}\nQuery: {query}"

        @tool
        def get_database_schema() -> str:
            """
            Get the current Neo4j database schema including node labels, relationship types, and properties.

            Returns:
                Formatted string with schema information

            Use this tool to understand what's in the database before writing queries.
            """
            schema_str = "Neo4j Database Schema:\n\n"
            schema_str += f"Node Labels ({len(self.schema['node_labels'])}):\n"
            for label in self.schema['node_labels']:
                schema_str += f"  - {label}\n"

            schema_str += f"\nRelationship Types ({len(self.schema['relationship_types'])}):\n"
            for rel_type in self.schema['relationship_types']:
                schema_str += f"  - {rel_type}\n"

            schema_str += f"\nProperty Keys ({len(self.schema['property_keys'])}):\n"
            for prop in self.schema['property_keys'][:30]:  # Limit to first 30
                schema_str += f"  - {prop}\n"

            if len(self.schema['property_keys']) > 30:
                schema_str += f"  ... and {len(self.schema['property_keys']) - 30} more properties\n"

            return schema_str

        @tool
        def count_nodes_by_label(label: str) -> str:
            """
            Count the number of nodes with a specific label in the database.

            Args:
                label: Node label to count (e.g., "Requirement", "BusinessObject", "Message")

            Returns:
                Count of nodes with that label

            Use this to understand the scale of data before executing more complex queries.
            """
            try:
                # Validate label to prevent Cypher injection
                if not label.replace('_', '').isalnum():
                    return f"Error: Invalid label name '{label}'. Label must contain only alphanumeric characters and underscores."

                query = f"MATCH (n:{label}) RETURN count(n) as count"
                results = self.neo4j.execute_query(query)
                if results:
                    return f"There are {results[0]['count']} {label} nodes in the database."
                return f"Could not count {label} nodes."
            except Exception as e:
                return f"Error counting nodes: {str(e)}"

        @tool
        def find_sample_nodes(label: str, limit: int = 5) -> str:
            """
            Get a sample of nodes with a specific label to understand their structure.

            Args:
                label: Node label to sample (e.g., "Requirement", "BusinessObject", "Message")
                limit: Number of samples to return (default: 5, max: 10)

            Returns:
                Sample nodes with their properties

            Use this to understand the data structure before writing complex queries.
            """
            try:
                # Validate label to prevent Cypher injection
                if not label.replace('_', '').isalnum():
                    return f"Error: Invalid label name '{label}'. Label must contain only alphanumeric characters and underscores."

                limit = min(limit, 10)  # Cap at 10
                query = f"MATCH (n:{label}) RETURN n LIMIT {limit}"
                results = self.neo4j.execute_query(query)

                if not results:
                    return f"No {label} nodes found in the database."

                formatted = f"Sample {label} nodes:\n\n"
                for i, record in enumerate(results, 1):
                    node = record['n']
                    formatted += f"{i}. {dict(node)}\n"

                return formatted

            except Exception as e:
                return f"Error finding sample nodes: {str(e)}"

        @tool
        def validate_schema_compliance(
            check_type: str = "all",
            specific_check: str = None
        ) -> str:
            """
            Run metamodel compliance checks against the current graph state.

            Use this tool when you need to:
            - Verify if the graph conforms to the FINIUS metamodel
            - Check specific constraint categories (structural, relationships, quality)
            - Re-validate after discussing potential issues
            - Assess GoBD compliance readiness

            Args:
                check_type: Category of checks to run. Options:
                    - "all": Run all checks (A, B, C categories) - default
                    - "structural": Only node label and property checks (Category A: A1, A2, A3)
                    - "relationships": Only relationship type/direction checks (Category B: B1, B2, B3)
                    - "quality": Only semantic quality checks (Category C: C1, C2, C3)
                    - "specific": Run a single check by ID (requires specific_check parameter)
                specific_check: Check ID when check_type="specific" (e.g., "A1", "B2", "C3")
                    - A1: Node labels must be from allowed set
                    - A2: ID properties must be unique
                    - A3: Required properties must exist
                    - B1: Relationship types must be valid
                    - B2: Relationship directions must be correct
                    - B3: No invalid self-loops
                    - C1: Requirements should have connections (warning)
                    - C2: Messages should specify content (warning)
                    - C3: GoBD items need impact traceability (warning)

            Returns:
                Structured compliance report with pass/fail status, violations,
                warnings, and the Cypher queries used for audit trail.
            """
            try:
                if check_type == "all":
                    report = self.validator.run_all_checks()
                elif check_type == "structural":
                    report = self.validator.run_structural_checks()
                elif check_type == "relationships":
                    report = self.validator.run_relationship_checks()
                elif check_type == "quality":
                    report = self.validator.run_quality_gate_checks()
                elif check_type == "specific" and specific_check:
                    report = self.validator.run_specific_check(specific_check)
                else:
                    return "Invalid check_type. Use 'all', 'structural', 'relationships', 'quality', or 'specific' with a specific_check ID."

                return format_report_for_agent(report)

            except ValueError as e:
                return f"Error: {str(e)}"
            except Exception as e:
                return f"Error running compliance check: {str(e)}"

        @tool
        def web_search(query: str) -> str:
            """
            Search the web for information using Tavily AI search.

            Use this tool when you need to:
            - Look up current regulations, standards, or compliance requirements (e.g., GoBD)
            - Find documentation about specific technologies or frameworks
            - Research best practices or industry standards
            - Get up-to-date information beyond the knowledge cutoff
            - Verify or supplement information about German accounting principles

            Args:
                query: The search query. Be specific and include relevant context.
                    Good: "GoBD compliance requirements for digital invoices Germany 2024"
                    Bad: "GoBD"

            Returns:
                Search results with relevant content and sources.
            """
            if not self.web_search_tool:
                return "Web search is not available. Please set TAVILY_API_KEY environment variable and install langchain-tavily."

            try:
                results = self.web_search_tool.invoke({"query": query})

                # Format results for the agent
                if isinstance(results, str):
                    return results

                # Handle list of results
                if isinstance(results, list):
                    formatted = []
                    for i, result in enumerate(results, 1):
                        if isinstance(result, dict):
                            title = result.get('title', 'No title')
                            content = result.get('content', result.get('snippet', 'No content'))
                            url = result.get('url', '')
                            formatted.append(f"**Result {i}: {title}**\n{content}\nSource: {url}\n")
                        else:
                            formatted.append(f"**Result {i}:** {result}\n")
                    return "\n".join(formatted) if formatted else "No results found."

                return str(results)

            except Exception as e:
                return f"Error performing web search: {str(e)}"

        # Build tools list
        tools = [execute_cypher_query, get_database_schema, count_nodes_by_label, find_sample_nodes, validate_schema_compliance]

        # Add web search if available
        if self.web_search_tool:
            tools.append(web_search)

        return tools


# ============================================================================
# Agent Nodes
# ============================================================================

class RequirementGraphAgent:
    """LangGraph-based agent for iterative requirement analysis."""

    def __init__(self, neo4j_connection: Neo4jConnection, llm_model: str, temperature: float = 0.0, system_prompt: str = None, reasoning_level: str = "medium"):
        """
        Initialize the graph agent.

        Args:
            neo4j_connection: Active Neo4j connection
            llm_model: LLM model to use
            temperature: LLM temperature setting
            system_prompt: Optional custom system prompt
            reasoning_level: Reasoning effort level (low, medium, high) for gpt-oss models
        """
        self.neo4j = neo4j_connection
        self.llm_model = llm_model
        self.temperature = temperature
        self.system_prompt = system_prompt
        self.reasoning_level = reasoning_level

        # Initialize LLM (supports custom OpenAI-compatible endpoints like vLLM)
        llm_kwargs = {
            "model": self.llm_model,
            "temperature": self.temperature,
            "api_key": os.getenv('OPENAI_API_KEY'),
        }
        base_url = os.getenv('LLM_BASE_URL')
        if base_url:
            llm_kwargs["base_url"] = base_url

        self.llm = ChatOpenAI(**llm_kwargs)

        # Initialize tools
        self.neo4j_tools = Neo4jTools(neo4j_connection)
        self.tools = self.neo4j_tools.get_tools()

        # Bind tools to LLM
        self.llm_with_tools = self.llm.bind_tools(self.tools)

        # Build the graph
        self.graph = self.build_graph()

    def _build_reasoning_directive(self) -> str:
        """Build the reasoning directive based on configured level."""
        level = self.reasoning_level if self.reasoning_level in ("low", "medium", "high") else "medium"
        return f"Reasoning: {level}"

    def planner_node(self, state: AgentState) -> AgentState:
        """
        Plan how to approach the requirement analysis.
        """
        requirement = state["requirement"]
        reasoning_directive = self._build_reasoning_directive()

        if self.system_prompt:
            system_message = f"{reasoning_directive}\n\n{self.system_prompt}"
        else:
            system_message = f"""{reasoning_directive}

You are an expert analyst for Neo4j graph database requirements and metamodel compliance.

IMPORTANT Neo4j Query Guidelines:
- Use elementId(node) instead of id(node) - the id() function is deprecated
- Use element_id property when available instead of internal IDs
- For node matching, prefer property-based queries over ID-based queries

Database Metamodel (FINIUS):
- Requirement nodes: Business requirements with properties (rid, name, text, type, goBDRelevant, etc.)
- BusinessObject nodes: Business domain entities (boid, name, description, domain, owner)
- Message nodes: System messages (mid, name, description, direction, format, protocol)

Allowed Relationships:
- Requirement → Requirement: REFINES, DEPENDS_ON, TRACES_TO
- Requirement → BusinessObject: RELATES_TO_OBJECT, IMPACTS_OBJECT
- Requirement → Message: RELATES_TO_MESSAGE, IMPACTS_MESSAGE
- Message → BusinessObject: USES_OBJECT, PRODUCES_OBJECT

Compliance Checking:
You have access to a validate_schema_compliance tool that performs deterministic checks:
- Category A (ERROR): Structural constraints (node labels, unique IDs, required properties)
- Category B (ERROR): Relationship constraints (valid types, correct directions, no invalid self-loops)
- Category C (WARNING): Quality gates (orphan requirements, message content, GoBD traceability)

Use compliance checking when:
- Verifying if the graph conforms to the metamodel
- Assessing GoBD compliance readiness
- Investigating structural issues before deeper analysis
- Explaining violations in business terms

Web Search:
You may have access to a web_search tool for looking up external information.
Use web search when:
- Researching GoBD (Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern) requirements
- Looking up current compliance regulations or standards
- Finding documentation about specific technologies
- Verifying information about German accounting/tax principles

Your task is to create a plan for analyzing this requirement. Consider:
1. What information do you need from the database?
2. Should you run compliance checks first to understand graph health?
3. What queries might help answer this requirement?
4. What relationships should you explore?
5. How can you verify compliance or assess impact?

Create a step-by-step plan (3-5 steps) that you'll execute using the available tools."""

        messages = [
            SystemMessage(content=system_message),
            HumanMessage(content=f"Requirement to analyze:\n{requirement}\n\nCreate an analysis plan.")
        ]

        response = self.llm.invoke(messages)

        state["plan"] = response.content
        state["messages"] = [
            SystemMessage(content=system_message),
            HumanMessage(content=f"Requirement: {requirement}"),
            AIMessage(content=f"Analysis Plan:\n{response.content}")
        ]
        state["iteration"] = 0
        state["queries_executed"] = []
        state["is_complete"] = False

        return state

    def agent_node(self, state: AgentState) -> AgentState:
        """
        Agent decides what action to take next (query, analyze, or finish).
        """
        iteration = state.get("iteration", 0)
        max_iterations = state.get("max_iterations", 5)

        # Add context about where we are in the process
        if iteration == 0:
            context_msg = "You have created a plan. Now start executing it by using the available tools to query the database."
        elif iteration < max_iterations - 1:
            context_msg = f"Iteration {iteration}/{max_iterations}. Continue your analysis. You can execute more queries or move to final analysis if you have enough information."
        else:
            context_msg = f"Final iteration ({iteration}/{max_iterations}). Gather any last information needed, then provide your final analysis."

        state["messages"].append(HumanMessage(content=context_msg))

        # Agent decides what to do next
        response = self.llm_with_tools.invoke(state["messages"])
        state["messages"].append(response)

        return state

    def should_continue(self, state: AgentState) -> str:
        """
        Determine if the agent should continue querying or move to final report.
        """
        messages = state["messages"]
        last_message = messages[-1]

        # If there are tool calls, execute them
        if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            return "tools"

        # Check if we've hit max iterations
        if state.get("iteration", 0) >= state.get("max_iterations", 5):
            return "report"

        # Check if agent indicated completion
        if state.get("is_complete", False):
            return "report"

        # If we have executed queries, move to report after a few iterations
        if state.get("iteration", 0) >= 2 and len(state.get("queries_executed", [])) > 0:
            return "report"

        # If no tool calls and multiple iterations without progress, generate report
        if state.get("iteration", 0) >= 3:
            return "report"

        # Continue the agent loop
        return "continue"

    def increment_iteration(self, state: AgentState) -> AgentState:
        """
        Increment iteration counter after tool execution.
        """
        state["iteration"] = state.get("iteration", 0) + 1

        # Track queries executed - extract from the last tool call message
        messages = state["messages"]
        if len(messages) >= 2:
            # Check the message before the tool result (should be the AIMessage with tool calls)
            for msg in reversed(messages[-3:]):
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    for tool_call in msg.tool_calls:
                        if tool_call['name'] == 'execute_cypher_query':
                            query_info = {
                                'iteration': state["iteration"],
                                'query': tool_call.get('args', {}).get('query', 'Unknown'),
                                'tool': 'execute_cypher_query',
                                'result_count': 'See results'  # Would need to parse from tool result
                            }
                            state["queries_executed"].append(query_info)
                            break
                    break

        return state

    def report_generator_node(self, state: AgentState) -> AgentState:
        """
        Generate final comprehensive analysis report.
        """
        requirement = state["requirement"]
        queries = state.get("queries_executed", [])
        reasoning_directive = self._build_reasoning_directive()

        system_message = f"""{reasoning_directive}

You are an expert analyst for requirement traceability and compliance checking in a car rental business system.

Based on the requirement analysis you've conducted, provide a comprehensive final report with these sections:

1. **Summary**: Clear, concise answer to the requirement question
2. **Findings**: Key data points and evidence from your queries
3. **Compliance Status**: Whether requirement is met, partially met, or not met
4. **Metamodel Compliance**: If you ran schema compliance checks, summarize the results:
   - Structural issues (invalid labels, missing properties)
   - Relationship violations (invalid types, wrong directions)
   - Quality warnings (orphan nodes, missing traceability)
5. **Impact Assessment**: Which business objects, messages, or requirements are affected
6. **Recommendations**: Specific actions or considerations, including:
   - GoBD compliance remediation
   - Metamodel violation fixes (with example Cypher where helpful)
   - Quality improvements
7. **Risk Level**: Assessment of any risks or concerns identified

Format your analysis with clear section headers and reference specific data from your investigation."""

        final_message = f"""Based on your investigation, provide the final comprehensive analysis report for:

Requirement: {requirement}

You executed {len(queries)} queries during your investigation. Now synthesize all the information into a clear, actionable report."""

        messages = state["messages"] + [
            SystemMessage(content=system_message),
            HumanMessage(content=final_message)
        ]

        response = self.llm.invoke(messages)

        state["analysis"] = response.content
        state["is_complete"] = True

        return state

    def build_graph(self) -> StateGraph:
        """
        Build the LangGraph state machine.
        """
        # Create the graph
        workflow = StateGraph(AgentState)

        # Add nodes
        workflow.add_node("planner", self.planner_node)
        workflow.add_node("agent", self.agent_node)
        workflow.add_node("tools", ToolNode(self.tools))
        workflow.add_node("increment", self.increment_iteration)
        workflow.add_node("report", self.report_generator_node)

        # Set entry point
        workflow.set_entry_point("planner")

        # Add edges
        workflow.add_edge("planner", "agent")
        workflow.add_conditional_edges(
            "agent",
            self.should_continue,
            {
                "tools": "tools",
                "continue": "agent",
                "report": "report"
            }
        )
        workflow.add_edge("tools", "increment")
        workflow.add_edge("increment", "agent")
        workflow.add_edge("report", END)

        # Compile the workflow
        return workflow.compile()

    def process_requirement(self, requirement: str, max_iterations: int = 5) -> Dict[str, Any]:
        """
        Process a requirement using the graph agent.

        Args:
            requirement: Requirement text to analyze
            max_iterations: Maximum number of query iterations (default: 5)

        Returns:
            Dictionary with analysis results
        """
        print(f"\n{'='*80}")
        print(f"LangGraph Agent Processing:")
        print(f"{requirement}")
        print(f"{'='*80}\n")

        # Initialize state
        initial_state = AgentState(
            messages=[],
            requirement=requirement,
            plan="",
            queries_executed=[],
            iteration=0,
            max_iterations=max_iterations,
            analysis="",
            is_complete=False
        )

        # Run the graph with recursion limit configuration
        print("Starting agent workflow...\n")
        final_state = self.graph.invoke(
            initial_state,
            config={"recursion_limit": 50}
        )

        print("\n✓ Agent workflow complete\n")

        # Extract results
        return {
            'original_requirement': requirement,
            'plan': final_state.get('plan', ''),
            'queries_executed': final_state.get('queries_executed', []),
            'iterations': final_state.get('iteration', 0),
            'analysis': final_state.get('analysis', ''),
            'messages': [msg.content if hasattr(msg, 'content') else str(msg) for msg in final_state.get('messages', [])]
        }

    def process_requirement_stream(self, requirement: str, max_iterations: int = 5):
        """
        Process a requirement and yield state updates for streaming.

        Args:
            requirement: Requirement text to analyze
            max_iterations: Maximum number of query iterations

        Yields:
            Dictionary containing the current state of the workflow
        """
        # Initialize state
        initial_state = AgentState(
            messages=[],
            requirement=requirement,
            plan="",
            queries_executed=[],
            iteration=0,
            max_iterations=max_iterations,
            analysis="",
            is_complete=False
        )

        # Stream the graph
        return self.graph.stream(
            initial_state,
            config={"recursion_limit": 50}
        )


def create_graph_agent(neo4j_connection: Neo4jConnection) -> RequirementGraphAgent:
    """
    Create a requirement graph agent with configuration from config file.

    Args:
        neo4j_connection: Active Neo4j connection

    Returns:
        Configured RequirementGraphAgent instance
    """
    config = load_config("llm_config.json")["agent"]

    return RequirementGraphAgent(
        neo4j_connection=neo4j_connection,
        llm_model=config["model"],
        temperature=config.get("temperature", 0.0),
        reasoning_level=config.get("reasoning_level", "medium")
    )
