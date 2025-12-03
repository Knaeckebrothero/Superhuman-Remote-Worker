"""
Requirement Analysis System - Streamlit Application

Main entry point for the multi-page Streamlit application.
Run with: streamlit run main.py
"""
import sys
from pathlib import Path

# Add project root to path for imports
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

import streamlit as st
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Page config
st.set_page_config(
    page_title="Requirement Analysis",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Sidebar navigation
st.sidebar.title("Navigation")

page = st.sidebar.radio(
    "Select Page",
    ["Home", "Agent", "Chain"],
    label_visibility="collapsed"
)

st.sidebar.divider()

# Connection status indicator in sidebar
if "neo4j_connection" in st.session_state and st.session_state.neo4j_connection:
    st.sidebar.success("Neo4j: Connected")
else:
    st.sidebar.warning("Neo4j: Not connected")

# Page routing
if page == "Home":
    from src.ui.home import render
    render()
elif page == "Agent":
    from src.ui.agent import render
    render()
elif page == "Chain":
    from src.ui.chain import render
    render()
