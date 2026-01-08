"""
Validator Agent page for requirement validation and graph integration.

This page provides a UI for:
- Browsing pending requirements from the cache
- Manually entering requirements for ad-hoc testing
- Submitting requirements for validation
- Viewing validation results and graph changes
"""
import json
import time
import traceback

import streamlit as st

from src.ui.agent_client import get_agent_client, AgentConfig


# Validation phases for progress display
PHASES = ["understanding", "relevance", "fulfillment", "planning", "integration", "documentation"]
PHASE_DESCRIPTIONS = {
    "understanding": "Parsing requirement",
    "relevance": "Checking relevance",
    "fulfillment": "Analyzing fulfillment",
    "planning": "Planning graph operations",
    "integration": "Integrating into graph",
    "documentation": "Documenting results",
}

# Requirement types
REQUIREMENT_TYPES = ["functional", "compliance", "constraint", "non_functional"]
PRIORITY_LEVELS = ["high", "medium", "low"]


def _render_service_status():
    """Render service connection status."""
    config = AgentConfig.from_env()
    client = get_agent_client()

    try:
        health = client.check_validator_health()
        if health.healthy:
            st.success(f"Validator Agent: Connected ({health.state or 'idle'})")
        else:
            st.error(f"Validator Agent: {health.message}")
            st.caption(f"URL: {config.validator_url}")
            st.stop()
    except Exception as e:
        st.error(f"Validator Agent: Connection failed - {str(e)}")
        st.caption(f"URL: {config.validator_url}")
        st.stop()


def _render_pending_requirements_tab():
    """Render the pending requirements browser tab."""
    client = get_agent_client()

    # Refresh button
    col1, col2 = st.columns([1, 4])
    with col1:
        if st.button("Refresh", key="refresh_pending"):
            st.rerun()

    # Fetch pending requirements
    try:
        response = client.list_pending_requirements(limit=50)
        requirements = response.get("requirements", [])
        total = response.get("total", 0)

        with col2:
            st.caption(f"Total pending: {total}")

    except Exception as e:
        st.error(f"Failed to fetch requirements: {str(e)}")
        return None

    if not requirements:
        st.info("No pending requirements in the cache. Use the Creator Agent to extract requirements first.")
        return None

    # Display requirements
    for req in requirements:
        with st.expander(
            f"[{req.get('type', '?').upper()}] {req.get('name', 'Unnamed')} - Confidence: {req.get('confidence', 0):.0%}",
            expanded=False,
        ):
            st.markdown(f"**Text:** {req.get('text', 'N/A')}")

            col1, col2 = st.columns(2)
            with col1:
                st.markdown(f"**Job ID:** `{req.get('job_id', 'N/A')[:8]}...`")
                st.markdown(f"**Priority:** {req.get('priority', 'N/A')}")

            with col2:
                gobd = "Yes" if req.get("gobd_relevant") else "No"
                gdpr = "Yes" if req.get("gdpr_relevant") else "No"
                st.markdown(f"**GoBD:** {gobd} | **GDPR:** {gdpr}")

            # Select button
            if st.button(
                "Select for Validation",
                key=f"select_{req['id']}",
                disabled=st.session_state.get("validator_running", False),
            ):
                st.session_state.selected_requirement_id = req["id"]
                st.session_state.selected_requirement_text = req.get("text", "")
                st.rerun()

    return None


def _render_manual_entry_tab():
    """Render the manual requirement entry tab."""
    st.markdown("Enter requirement details for ad-hoc validation testing.")

    # Requirement text
    text = st.text_area(
        "Requirement Text",
        height=150,
        placeholder="Enter the requirement text to validate...",
        disabled=st.session_state.get("validator_running", False),
    )

    # Metadata columns
    col1, col2 = st.columns(2)

    with col1:
        name = st.text_input(
            "Requirement Name",
            placeholder="Short descriptive name",
            disabled=st.session_state.get("validator_running", False),
        )

        req_type = st.selectbox(
            "Type",
            options=REQUIREMENT_TYPES,
            disabled=st.session_state.get("validator_running", False),
        )

        priority = st.selectbox(
            "Priority",
            options=PRIORITY_LEVELS,
            index=1,  # Default to medium
            disabled=st.session_state.get("validator_running", False),
        )

    with col2:
        gobd_relevant = st.checkbox(
            "GoBD Relevant",
            disabled=st.session_state.get("validator_running", False),
        )

        gdpr_relevant = st.checkbox(
            "GDPR Relevant",
            disabled=st.session_state.get("validator_running", False),
        )

        mentioned_objects = st.text_input(
            "Mentioned Objects",
            placeholder="Comma-separated list",
            help="Business objects mentioned in the requirement",
            disabled=st.session_state.get("validator_running", False),
        )

        mentioned_messages = st.text_input(
            "Mentioned Messages",
            placeholder="Comma-separated list",
            help="Messages mentioned in the requirement",
            disabled=st.session_state.get("validator_running", False),
        )

    return {
        "text": text,
        "name": name,
        "type": req_type,
        "priority": priority,
        "gobd_relevant": gobd_relevant,
        "gdpr_relevant": gdpr_relevant,
        "mentioned_objects": [o.strip() for o in mentioned_objects.split(",") if o.strip()],
        "mentioned_messages": [m.strip() for m in mentioned_messages.split(",") if m.strip()],
    }


def _render_input_section():
    """Render the input section with tabs."""
    st.subheader("Select Requirement")

    tab1, tab2 = st.tabs(["From Cache", "Manual Entry"])

    with tab1:
        _render_pending_requirements_tab()

    with tab2:
        manual_data = _render_manual_entry_tab()

    # Show selected requirement
    if st.session_state.get("selected_requirement_id"):
        st.info(f"Selected: `{st.session_state.selected_requirement_id}`")

    # Max iterations setting
    with st.expander("Advanced Settings"):
        max_iterations = st.slider(
            "Max Iterations",
            min_value=10,
            max_value=200,
            value=100,
            disabled=st.session_state.get("validator_running", False),
        )

    # Submit button
    col1, col2 = st.columns([1, 4])

    with col1:
        submit_button = st.button(
            "Validate",
            type="primary",
            disabled=st.session_state.get("validator_running", False),
        )

    with col2:
        if st.session_state.get("validator_running", False):
            if st.button("Cancel", type="secondary"):
                st.session_state.validator_running = False
                st.session_state.validator_request_id = None
                st.rerun()

    return submit_button, manual_data, max_iterations


def _submit_validation(manual_data, max_iterations):
    """Submit a validation request."""
    client = get_agent_client()

    try:
        if st.session_state.get("selected_requirement_id"):
            # Validate existing requirement
            response = client.submit_validation(
                requirement_id=st.session_state.selected_requirement_id,
                max_iterations=max_iterations,
            )
        elif manual_data.get("text"):
            # Validate manual entry
            response = client.submit_validation(
                text=manual_data["text"],
                name=manual_data.get("name"),
                req_type=manual_data.get("type"),
                priority=manual_data.get("priority"),
                mentioned_objects=manual_data.get("mentioned_objects"),
                mentioned_messages=manual_data.get("mentioned_messages"),
                gobd_relevant=manual_data.get("gobd_relevant", False),
                gdpr_relevant=manual_data.get("gdpr_relevant", False),
                max_iterations=max_iterations,
            )
        else:
            st.error("Please select a requirement from the cache or enter text manually.")
            return None

        return response.get("request_id")

    except Exception as e:
        st.error(f"Failed to submit validation: {str(e)}")
        return None


def _render_progress():
    """Render validation progress with polling."""
    if not st.session_state.get("validator_request_id"):
        return

    request_id = st.session_state.validator_request_id
    client = get_agent_client()

    st.subheader("Validation Progress")

    # Progress containers
    status_container = st.empty()
    progress_bar = st.progress(0)
    phase_container = st.empty()

    # Poll for status
    while st.session_state.get("validator_running", False):
        try:
            status = client.get_validation_status(request_id)

            # Update progress bar
            progress = status.get("progress_percent", 0) / 100
            progress_bar.progress(progress)

            # Update status
            validation_status = status.get("status", "unknown")

            if validation_status in ("completed", "rejected", "failed"):
                st.session_state.validator_running = False
                st.session_state.validator_result = status

                if validation_status == "completed":
                    status_container.success("Requirement validated and integrated into graph!")
                    progress_bar.progress(1.0)
                elif validation_status == "rejected":
                    reason = status.get("rejection_reason", "Unknown reason")
                    status_container.warning(f"Requirement rejected: {reason}")
                else:
                    error_msg = status.get("error", {}).get("message", "Unknown error")
                    status_container.error(f"Validation failed: {error_msg}")

                break

            else:
                # Show current phase
                current_phase = status.get("current_phase", "unknown")
                phase_desc = PHASE_DESCRIPTIONS.get(current_phase, current_phase)
                phase_container.info(f"Phase: {phase_desc}")

                phases_completed = status.get("phases_completed", [])
                status_container.info(
                    f"Status: {validation_status} | "
                    f"Iteration: {status.get('iteration', 0)}/{status.get('max_iterations', 100)} | "
                    f"Phases: {len(phases_completed)}/{len(PHASES)}"
                )

            # Wait before polling again
            time.sleep(2)

        except Exception as e:
            status_container.error(f"Error polling status: {str(e)}")
            time.sleep(5)


def _render_results():
    """Render validation results."""
    if not st.session_state.get("validator_result"):
        return

    result = st.session_state.validator_result

    st.divider()
    st.subheader("Validation Results")

    # Status badge
    status = result.get("status", "unknown")
    if status == "completed":
        st.success("Status: INTEGRATED")
    elif status == "rejected":
        st.warning(f"Status: REJECTED - {result.get('rejection_reason', 'N/A')}")
    else:
        st.error(f"Status: {status.upper()}")

    # Graph changes
    graph_changes = result.get("graph_changes", [])
    if graph_changes:
        st.markdown("**Graph Changes:**")
        for change in graph_changes:
            op = change.get("operation", "unknown")
            if op == "create_node":
                st.markdown(f"- Created node: `{change.get('node_type')}` (ID: {change.get('node_id', 'N/A')[:8]}...)")
            elif op == "create_relationship":
                st.markdown(f"- Created relationship: `{change.get('relationship_type')}`")
            else:
                st.markdown(f"- {op}: {change}")

    if result.get("graph_node_id"):
        st.markdown(f"**Graph Node ID:** `{result['graph_node_id']}`")

    # Related entities
    col1, col2 = st.columns(2)

    with col1:
        related_objects = result.get("related_objects", [])
        if related_objects:
            st.markdown("**Related Objects:**")
            for obj in related_objects[:10]:
                name = obj.get("name", obj.get("bo_name", "Unknown"))
                st.markdown(f"- {name}")
            if len(related_objects) > 10:
                st.caption(f"... and {len(related_objects) - 10} more")

    with col2:
        related_messages = result.get("related_messages", [])
        if related_messages:
            st.markdown("**Related Messages:**")
            for msg in related_messages[:10]:
                name = msg.get("name", msg.get("message_name", "Unknown"))
                st.markdown(f"- {name}")
            if len(related_messages) > 10:
                st.caption(f"... and {len(related_messages) - 10} more")

    # Fulfillment analysis
    fulfillment = result.get("fulfillment_analysis")
    if fulfillment:
        with st.expander("Fulfillment Analysis"):
            st.json(fulfillment)

    # Export button
    st.divider()
    result_json = json.dumps(result, indent=2, default=str)
    st.download_button(
        label="Export JSON",
        data=result_json,
        file_name=f"validation_result_{st.session_state.validator_request_id[:8]}.json",
        mime="application/json",
    )


def render():
    """Render the Validator Agent page."""
    st.title("Validator Agent")
    st.markdown("""
    Validate requirements against the Neo4j knowledge graph.
    The agent analyzes relevance, checks fulfillment by existing entities,
    and integrates validated requirements with appropriate relationships.
    """)

    st.divider()

    # Service status
    _render_service_status()

    st.divider()

    # Initialize session state
    if "validator_running" not in st.session_state:
        st.session_state.validator_running = False
    if "validator_request_id" not in st.session_state:
        st.session_state.validator_request_id = None
    if "validator_result" not in st.session_state:
        st.session_state.validator_result = None
    if "selected_requirement_id" not in st.session_state:
        st.session_state.selected_requirement_id = None
    if "selected_requirement_text" not in st.session_state:
        st.session_state.selected_requirement_text = ""

    # Input section
    submit_button, manual_data, max_iterations = _render_input_section()

    # Handle submission
    if submit_button:
        st.session_state.validator_result = None
        request_id = _submit_validation(manual_data, max_iterations)

        if request_id:
            st.session_state.validator_request_id = request_id
            st.session_state.validator_running = True
            st.session_state.selected_requirement_id = None  # Clear selection
            st.rerun()

    # Show progress if running
    if st.session_state.get("validator_running", False):
        _render_progress()

    # Show results
    _render_results()
