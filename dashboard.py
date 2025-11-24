import streamlit as st
import os
import json
from dotenv import load_dotenv
from src.neo4j_utils import create_neo4j_connection
from src.requirement_agent_graph import RequirementGraphAgent

# Load environment variables
load_dotenv()

# Page config
st.set_page_config(
    page_title="Requirement Analysis Agent",
    page_icon="🕵️‍♂️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .stChatMessage {
        padding: 1rem;
        border-radius: 0.5rem;
        margin-bottom: 1rem;
    }
    .stChatMessage.user {
        background-color: #f0f2f6;
    }
    .stChatMessage.assistant {
        background-color: #e8f0fe;
    }
    .query-box {
        background-color: #1e1e1e;
        color: #d4d4d4;
        padding: 10px;
        border-radius: 5px;
        font-family: monospace;
        margin: 5px 0;
        white-space: pre-wrap;
    }
    .step-box {
        border-left: 3px solid #4a90e2;
        padding-left: 10px;
        margin: 10px 0;
    }
</style>
""", unsafe_allow_html=True)

# Initialize session state
if "messages" not in st.session_state:
    st.session_state.messages = []
if "agent_running" not in st.session_state:
    st.session_state.agent_running = False

# Sidebar Configuration
with st.sidebar:
    st.title("Agent Configuration")
    
    st.subheader("Model Settings")
    model_options = ["gpt-5-mini-2025-08-07", "gpt-5.1-2025-11-13", "gpt-oss-120b", "gpt-4o", "gpt-3.5-turbo"]
    selected_model = st.selectbox(
        "LLM Model", 
        options=model_options,
        index=0
    )
    
    temperature = st.slider(
        "Temperature",
        min_value=0.0,
        max_value=1.0,
        value=0.0,
        step=0.1,
        help="Higher values make output more random, lower values more deterministic."
    )
    
    max_iterations = st.number_input(
        "Max Iterations",
        min_value=1,
        max_value=20,
        value=5,
        help="Maximum number of reasoning steps the agent can take."
    )
    
    st.subheader("System Prompt")
    default_system_prompt = """You are an expert analyst for Neo4j graph database requirements.

IMPORTANT Neo4j Query Guidelines:
- Use elementId(node) instead of id(node) - the id() function is deprecated
- Use element_id property when available instead of internal IDs
- For node matching, prefer property-based queries over ID-based queries

Database Metamodel:
- Requirement nodes: Business requirements with properties (rid, name, text, type, goBDRelevant, etc.)
- BusinessObject nodes: Business domain entities (boid, name, description, domain, owner)
- Message nodes: System messages (mid, name, description, direction, format, protocol)

Relationships:
- Requirements can REFINES, DEPENDS_ON, TRACES_TO other requirements
- Requirements can RELATES_TO_OBJECT or IMPACTS_OBJECT business objects
- Requirements can RELATES_TO_MESSAGE or IMPACTS_MESSAGE messages
- Messages can USES_OBJECT or PRODUCES_OBJECT business objects

Your task is to create a plan for analyzing this requirement. Consider:
1. What information do you need from the database?
2. What queries might help answer this requirement?
3. What relationships should you explore?
4. How can you verify compliance or assess impact?

Create a step-by-step plan (3-5 steps) that you'll execute using the available tools."""

    system_prompt = st.text_area(
        "Custom System Prompt",
        value=default_system_prompt,
        height=300
    )
    
    st.divider()
    st.info("Configure the agent settings above before running the analysis.")

# Main Content
st.title("🕵️‍♂️ Requirement Analysis Agent")
st.markdown("Analyze requirements against your Neo4j graph database using an autonomous agent.")

# Connection Management
@st.cache_resource
def get_neo4j_connection():
    conn = create_neo4j_connection()
    if conn.connect():
        return conn
    return None

neo4j_conn = get_neo4j_connection()

if not neo4j_conn:
    st.error("❌ Failed to connect to Neo4j database. Please check your .env configuration.")
    st.stop()
else:
    st.sidebar.success("✅ Connected to Neo4j")

# Input Area
requirement_input = st.text_area(
    "Enter Requirement to Analyze:",
    height=100,
    placeholder="e.g., Which requirements are marked as GoBD-relevant?"
)

col1, col2 = st.columns([1, 5])
with col1:
    run_button = st.button("Run Analysis", type="primary", disabled=st.session_state.agent_running)
with col2:
    if st.button("Clear History"):
        st.session_state.messages = []
        st.rerun()

# Execution Logic
if run_button and requirement_input:
    st.session_state.messages = []  # Clear previous run
    st.session_state.agent_running = True
    
    # Initialize Agent
    agent = RequirementGraphAgent(
        neo4j_connection=neo4j_conn,
        llm_model=selected_model,
        temperature=temperature,
        system_prompt=system_prompt
    )
    
    # Container for live updates
    status_container = st.container()
    
    with status_container:
        st.subheader("Analysis Progress")
        
        # Create placeholders for different stages
        plan_expander = st.expander("📋 Analysis Plan", expanded=True)
        steps_container = st.container()
        result_container = st.container()
        
        # Stream processing
        try:
            stream = agent.process_requirement_stream(
                requirement=requirement_input,
                max_iterations=max_iterations
            )
            
            current_step = 0
            
            for event in stream:
                # Handle different node updates
                for node_name, state in event.items():
                    
                    # Planner Node Update
                    if node_name == "planner":
                        with plan_expander:
                            st.markdown(state.get("plan", "No plan generated."))
                            st.success("Plan created!")
                    
                    # Agent/Tools Node Update
                    elif node_name == "agent" or node_name == "tools":
                        # Check for new messages to display reasoning or tool calls
                        messages = state.get("messages", [])
                        if messages:
                            last_msg = messages[-1]
                            
                            # If it's a tool call (AIMessage with tool_calls)
                            if hasattr(last_msg, 'tool_calls') and last_msg.tool_calls:
                                for tool_call in last_msg.tool_calls:
                                    with steps_container:
                                        with st.chat_message("assistant", avatar="🛠️"):
                                            st.write(f"**Executing Tool:** `{tool_call['name']}`")
                                            if 'query' in tool_call['args']:
                                                st.markdown(f"<div class='query-box'>{tool_call['args']['query']}</div>", unsafe_allow_html=True)
                                            else:
                                                st.json(tool_call['args'])
                            
                            # If it's a tool result (ToolMessage)
                            elif last_msg.type == 'tool':
                                with steps_container:
                                    with st.chat_message("assistant", avatar="💾"):
                                        st.write(f"**Tool Result:**")
                                        with st.expander("View Result Data"):
                                            st.text(last_msg.content)
                            
                            # If it's normal reasoning (AIMessage without tool calls)
                            elif last_msg.type == 'ai':
                                with steps_container:
                                    with st.chat_message("assistant", avatar="🤖"):
                                        st.markdown(last_msg.content)
                    
                    # Report Node Update (Final Result)
                    elif node_name == "report":
                        analysis = state.get("analysis", "")
                        with result_container:
                            st.divider()
                            st.subheader("📝 Final Analysis")
                            st.markdown(analysis)
                            
                            # Show stats
                            queries = state.get("queries_executed", [])
                            st.caption(f"Analysis completed in {state.get('iteration', 0)} iterations with {len(queries)} queries.")
                            
                            # Save result to history
                            st.session_state.messages.append({
                                "role": "assistant", 
                                "content": analysis,
                                "queries": len(queries)
                            })

        except Exception as e:
            st.error(f"An error occurred: {str(e)}")
            import traceback
            st.code(traceback.format_exc())
        
        finally:
            st.session_state.agent_running = False

# Display History (if not running)
if not st.session_state.agent_running and st.session_state.messages:
    st.divider()
    st.subheader("Previous Result")
    for msg in st.session_state.messages:
        st.markdown(msg["content"])
