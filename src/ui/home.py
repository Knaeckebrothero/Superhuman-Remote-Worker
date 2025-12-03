"""
Home page for the Requirement Analysis application.
Provides database connection settings and status.
"""
import os

import streamlit as st
from dotenv import load_dotenv

from src.core.neo4j_utils import Neo4jConnection

# Load environment variables for defaults
load_dotenv()


def render():
    """Render the home page."""
    st.title("Requirement Analysis System")
    st.markdown("""
    Analyze requirements against your Neo4j graph database using LLM-powered agents.

    **Features:**
    - **Agent Analysis**: Iterative LangGraph agent with tool-calling capabilities
    - **Chain Analysis**: Simple one-shot chain for comparison

    Configure your database connection below to get started.
    """)

    st.divider()

    # Database Connection Settings
    st.subheader("Database Connection")

    # Get defaults from environment
    default_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    default_username = os.getenv("NEO4J_USERNAME", "neo4j")
    default_password = os.getenv("NEO4J_PASSWORD", "")

    # Initialize session state for form values
    if "neo4j_uri" not in st.session_state:
        st.session_state.neo4j_uri = default_uri
    if "neo4j_username" not in st.session_state:
        st.session_state.neo4j_username = default_username
    if "neo4j_password" not in st.session_state:
        st.session_state.neo4j_password = default_password

    # Connection form
    col1, col2 = st.columns(2)

    with col1:
        uri = st.text_input(
            "Neo4j URI",
            value=st.session_state.neo4j_uri,
            help="Connection URI (e.g., bolt://localhost:7687)"
        )

        username = st.text_input(
            "Username",
            value=st.session_state.neo4j_username,
            help="Neo4j username"
        )

    with col2:
        password = st.text_input(
            "Password",
            value=st.session_state.neo4j_password,
            type="password",
            help="Neo4j password"
        )

    # Update session state
    st.session_state.neo4j_uri = uri
    st.session_state.neo4j_username = username
    st.session_state.neo4j_password = password

    # Connection buttons
    col1, col2, col3 = st.columns([1, 1, 3])

    with col1:
        connect_button = st.button("Connect", type="primary")

    with col2:
        disconnect_button = st.button("Disconnect")

    # Handle connect
    if connect_button:
        with st.spinner("Connecting to Neo4j..."):
            try:
                conn = Neo4jConnection(
                    uri=uri,
                    username=username,
                    password=password
                )
                if conn.connect():
                    st.session_state.neo4j_connection = conn
                    st.success("Connected to Neo4j successfully!")
                else:
                    st.error("Failed to connect to Neo4j. Please check your credentials.")
            except Exception as e:
                st.error(f"Connection error: {str(e)}")

    # Handle disconnect
    if disconnect_button:
        if "neo4j_connection" in st.session_state and st.session_state.neo4j_connection:
            try:
                st.session_state.neo4j_connection.close()
            except Exception:
                pass
            st.session_state.neo4j_connection = None
            st.info("Disconnected from Neo4j.")

    # Show connection status
    st.divider()
    st.subheader("Connection Status")

    conn = st.session_state.get("neo4j_connection", None)

    if conn is not None:
        st.success("Connected to Neo4j")

        # Show database info
        try:
            schema = conn.get_database_schema()
            col1, col2, col3 = st.columns(3)

            with col1:
                st.metric("Node Labels", len(schema.get("node_labels", [])))

            with col2:
                st.metric("Relationship Types", len(schema.get("relationship_types", [])))

            with col3:
                st.metric("Property Keys", len(schema.get("property_keys", [])))

            # Show labels
            with st.expander("Database Schema Details"):
                st.write("**Node Labels:**")
                st.write(", ".join(schema.get("node_labels", [])))

                st.write("**Relationship Types:**")
                st.write(", ".join(schema.get("relationship_types", [])))

        except Exception as e:
            st.warning(f"Could not retrieve schema: {str(e)}")
    else:
        st.info("Not connected. Enter your credentials and click Connect.")

    # Quick start guide
    st.divider()
    st.subheader("Quick Start")
    st.markdown("""
    1. **Connect** to your Neo4j database using the form above
    2. Navigate to **Agent** or **Chain** page using the sidebar
    3. Enter a requirement to analyze
    4. Click **Run Analysis** to start
    5. Export results as JSON when done
    """)
