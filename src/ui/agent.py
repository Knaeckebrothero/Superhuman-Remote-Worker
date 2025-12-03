"""
Agent page for requirement analysis using LangGraph iterative agent.
"""
import json
import traceback

import streamlit as st

from src.agents.graph_agent import RequirementGraphAgent
from src.ui import get_neo4j_connection, require_connection, load_config, load_prompt


# Custom CSS for styling
CUSTOM_CSS = """
<style>
    .query-box {
        background-color: #1e1e1e;
        color: #d4d4d4;
        padding: 10px;
        border-radius: 5px;
        font-family: monospace;
        margin: 5px 0;
        white-space: pre-wrap;
    }
</style>
"""


def render():
    """Render the agent analysis page."""
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    st.title("Agent Analysis")
    st.markdown("""
    Analyze requirements using the **LangGraph iterative agent**.
    The agent can plan, execute queries, reason about results, and refine its approach.
    """)

    # Check connection
    if not require_connection():
        return

    conn = get_neo4j_connection()

    # Load config
    try:
        config = load_config("agent_config.json")
        system_prompt = load_prompt("agent_system.txt")
    except FileNotFoundError as e:
        st.error(f"Configuration file not found: {e}")
        return

    # Initialize session state
    if "agent_running" not in st.session_state:
        st.session_state.agent_running = False
    if "agent_result" not in st.session_state:
        st.session_state.agent_result = None

    # Input area
    st.subheader("Requirement Input")
    requirement_input = st.text_area(
        "Enter requirement to analyze:",
        height=100,
        placeholder="e.g., Which requirements are marked as GoBD-relevant?",
        disabled=st.session_state.agent_running
    )

    # Run button
    col1, col2 = st.columns([1, 5])
    with col1:
        run_button = st.button(
            "Run Analysis",
            type="primary",
            disabled=st.session_state.agent_running or not requirement_input
        )

    # Execute analysis
    if run_button and requirement_input:
        st.session_state.agent_running = True
        st.session_state.agent_result = None

        # Initialize agent
        agent = RequirementGraphAgent(
            neo4j_connection=conn,
            llm_model=config.get("model", "gpt-4o-mini"),
            temperature=config.get("temperature", 0.0),
            system_prompt=system_prompt
        )

        # Create containers for progress
        st.subheader("Analysis Progress")
        plan_expander = st.expander("Analysis Plan", expanded=True)
        steps_container = st.container()
        result_container = st.container()

        # Stream processing
        try:
            stream = agent.process_requirement_stream(
                requirement=requirement_input,
                max_iterations=config.get("max_iterations", 5)
            )

            final_result = {
                "requirement": requirement_input,
                "plan": "",
                "queries": [],
                "analysis": "",
                "iterations": 0
            }

            for event in stream:
                for node_name, state in event.items():

                    # Planner Node
                    if node_name == "planner":
                        plan = state.get("plan", "No plan generated.")
                        final_result["plan"] = plan
                        with plan_expander:
                            st.markdown(plan)
                            st.success("Plan created!")

                    # Agent/Tools Node
                    elif node_name in ("agent", "tools"):
                        messages = state.get("messages", [])
                        if messages:
                            last_msg = messages[-1]

                            # Tool call (AIMessage with tool_calls)
                            if hasattr(last_msg, 'tool_calls') and last_msg.tool_calls:
                                for tool_call in last_msg.tool_calls:
                                    with steps_container:
                                        with st.chat_message("assistant", avatar="🛠️"):
                                            st.write(f"**Executing Tool:** `{tool_call['name']}`")
                                            if 'query' in tool_call['args']:
                                                query = tool_call['args']['query']
                                                final_result["queries"].append(query)
                                                st.markdown(
                                                    f"<div class='query-box'>{query}</div>",
                                                    unsafe_allow_html=True
                                                )
                                            else:
                                                st.json(tool_call['args'])

                            # Tool result (ToolMessage)
                            elif last_msg.type == 'tool':
                                with steps_container:
                                    with st.chat_message("assistant", avatar="💾"):
                                        st.write("**Tool Result:**")
                                        with st.expander("View Result Data"):
                                            st.text(last_msg.content)

                            # Reasoning (AIMessage without tool calls)
                            elif last_msg.type == 'ai':
                                with steps_container:
                                    with st.chat_message("assistant", avatar="🤖"):
                                        st.markdown(last_msg.content)

                    # Report Node (Final)
                    elif node_name == "report":
                        analysis = state.get("analysis", "")
                        final_result["analysis"] = analysis
                        final_result["iterations"] = state.get("iteration", 0)

                        with result_container:
                            st.divider()
                            st.subheader("Final Analysis")
                            st.markdown(analysis)

                            queries = state.get("queries_executed", [])
                            st.caption(
                                f"Analysis completed in {state.get('iteration', 0)} iterations "
                                f"with {len(queries)} queries."
                            )

            # Store result for export
            st.session_state.agent_result = final_result

        except Exception as e:
            st.error(f"An error occurred: {str(e)}")
            st.code(traceback.format_exc())

        finally:
            st.session_state.agent_running = False

    # Show export button if result available
    if st.session_state.agent_result:
        st.divider()

        result_json = json.dumps(st.session_state.agent_result, indent=2, default=str)

        st.download_button(
            label="Export JSON",
            data=result_json,
            file_name="agent_analysis.json",
            mime="application/json"
        )
