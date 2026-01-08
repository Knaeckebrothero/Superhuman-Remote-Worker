"""
Creator Agent page for document processing and requirement extraction.

This page provides a UI for:
- Uploading documents (PDF, DOCX, TXT, HTML)
- Submitting processing jobs to the Creator Agent
- Monitoring job progress via polling
- Viewing extracted requirements
"""
import json
import time
import traceback

import streamlit as st

from src.ui.agent_client import get_agent_client, AgentConfig


# Processing phases for progress display
PHASES = ["preprocessing", "identification", "research", "formulation", "output"]
PHASE_DESCRIPTIONS = {
    "preprocessing": "Processing document",
    "identification": "Identifying candidates",
    "research": "Researching context",
    "formulation": "Formulating requirements",
    "output": "Writing results",
}


def _render_service_status():
    """Render service connection status."""
    config = AgentConfig.from_env()
    client = get_agent_client()

    try:
        health = client.check_creator_health()
        if health.healthy:
            st.success(f"Creator Agent: Connected ({health.state or 'idle'})")
        else:
            st.error(f"Creator Agent: {health.message}")
            st.caption(f"URL: {config.creator_url}")
            st.stop()
    except Exception as e:
        st.error(f"Creator Agent: Connection failed - {str(e)}")
        st.caption(f"URL: {config.creator_url}")
        st.stop()


def _render_input_form():
    """Render the job submission form."""
    st.subheader("Document Processing")

    # File upload
    uploaded_file = st.file_uploader(
        "Upload Document",
        type=["pdf", "docx", "txt", "html", "md"],
        help="Upload a document to extract requirements from",
        disabled=st.session_state.get("creator_running", False),
    )

    # Prompt input
    prompt = st.text_area(
        "Processing Prompt",
        value="Extract all requirements from this document, focusing on GoBD compliance requirements.",
        height=100,
        help="Describe what requirements to extract",
        disabled=st.session_state.get("creator_running", False),
    )

    # Advanced settings
    with st.expander("Advanced Settings"):
        max_iterations = st.slider(
            "Max Iterations",
            min_value=10,
            max_value=200,
            value=50,
            help="Maximum processing iterations",
            disabled=st.session_state.get("creator_running", False),
        )

        context_input = st.text_area(
            "Additional Context (JSON)",
            value='{"domain": "car_rental", "region": "EU"}',
            height=80,
            help="Additional context as JSON object",
            disabled=st.session_state.get("creator_running", False),
        )

    # Submit button
    col1, col2 = st.columns([1, 4])
    with col1:
        submit_button = st.button(
            "Process Document",
            type="primary",
            disabled=st.session_state.get("creator_running", False) or not prompt,
        )

    with col2:
        if st.session_state.get("creator_running", False):
            if st.button("Cancel", type="secondary"):
                st.session_state.creator_running = False
                st.session_state.creator_job_id = None
                st.rerun()

    return submit_button, uploaded_file, prompt, max_iterations, context_input


def _submit_job(uploaded_file, prompt, max_iterations, context_input):
    """Submit a job to the Creator Agent."""
    client = get_agent_client()

    # Parse context
    try:
        context = json.loads(context_input) if context_input.strip() else {}
    except json.JSONDecodeError:
        st.error("Invalid JSON in context field")
        return None

    # Prepare document
    document_bytes = None
    document_filename = None
    if uploaded_file:
        document_bytes = uploaded_file.read()
        document_filename = uploaded_file.name

    # Submit job
    try:
        response = client.submit_job(
            prompt=prompt,
            document_bytes=document_bytes,
            document_filename=document_filename,
            context=context,
            max_iterations=max_iterations,
        )
        return response.get("job_id")
    except Exception as e:
        st.error(f"Failed to submit job: {str(e)}")
        return None


def _render_progress():
    """Render job progress with polling."""
    if not st.session_state.get("creator_job_id"):
        return

    job_id = st.session_state.creator_job_id
    client = get_agent_client()

    st.subheader("Processing Progress")

    # Progress containers
    status_container = st.empty()
    progress_bar = st.progress(0)
    phase_container = st.empty()
    details_container = st.container()

    # Poll for status
    while st.session_state.get("creator_running", False):
        try:
            status = client.get_job_status(job_id)

            # Update progress bar
            progress = status.get("progress_percent", 0) / 100
            progress_bar.progress(progress)

            # Update status
            job_status = status.get("status", "unknown")
            creator_status = status.get("creator_status", "unknown")

            if job_status in ("completed", "failed", "cancelled"):
                st.session_state.creator_running = False

                if job_status == "completed":
                    status_container.success(f"Job completed successfully!")
                    progress_bar.progress(1.0)
                    # Fetch requirements
                    try:
                        req_response = client.get_job_requirements(job_id)
                        st.session_state.creator_result = req_response
                    except Exception as e:
                        st.error(f"Failed to fetch requirements: {str(e)}")
                elif job_status == "failed":
                    status_container.error(f"Job failed: {status.get('error', {}).get('message', 'Unknown error')}")
                else:
                    status_container.warning("Job cancelled")

                break

            else:
                # Show current phase
                current_phase = status.get("current_phase", "unknown")
                phase_desc = PHASE_DESCRIPTIONS.get(current_phase, current_phase)
                phase_container.info(f"Phase: {phase_desc}")

                status_container.info(
                    f"Status: {creator_status} | "
                    f"Iteration: {status.get('iteration', 0)}/{status.get('max_iterations', 50)} | "
                    f"Requirements: {status.get('requirements_created', 0)}"
                )

            # Wait before polling again
            time.sleep(2)

        except Exception as e:
            status_container.error(f"Error polling status: {str(e)}")
            time.sleep(5)


def _render_results():
    """Render extraction results."""
    if not st.session_state.get("creator_result"):
        return

    result = st.session_state.creator_result
    requirements = result.get("requirements", [])

    st.divider()
    st.subheader("Extracted Requirements")

    if not requirements:
        st.info("No requirements were extracted from the document.")
        return

    st.metric("Total Requirements", len(requirements))

    # Requirements table
    for i, req in enumerate(requirements, 1):
        with st.expander(
            f"[{req.get('type', 'unknown').upper()}] {req.get('name', f'Requirement {i}')}",
            expanded=i <= 3,
        ):
            col1, col2, col3 = st.columns([2, 1, 1])

            with col1:
                st.markdown(f"**Text:** {req.get('text', 'N/A')}")

            with col2:
                st.markdown(f"**Confidence:** {req.get('confidence', 0):.0%}")
                st.markdown(f"**Priority:** {req.get('priority', 'N/A')}")

            with col3:
                gobd = "Yes" if req.get("gobd_relevant") else "No"
                gdpr = "Yes" if req.get("gdpr_relevant") else "No"
                st.markdown(f"**GoBD:** {gobd}")
                st.markdown(f"**GDPR:** {gdpr}")

            if req.get("mentioned_objects"):
                st.markdown(f"**Objects:** {', '.join(req['mentioned_objects'])}")

            if req.get("mentioned_messages"):
                st.markdown(f"**Messages:** {', '.join(req['mentioned_messages'])}")

    # Export button
    st.divider()
    result_json = json.dumps(result, indent=2, default=str)
    st.download_button(
        label="Export JSON",
        data=result_json,
        file_name=f"creator_result_{st.session_state.creator_job_id[:8]}.json",
        mime="application/json",
    )


def render():
    """Render the Creator Agent page."""
    st.title("Creator Agent")
    st.markdown("""
    Upload documents and extract requirements using the Creator Agent.
    The agent processes documents, identifies requirement candidates,
    researches context, and formulates citation-backed requirements.
    """)

    st.divider()

    # Service status
    _render_service_status()

    st.divider()

    # Initialize session state
    if "creator_running" not in st.session_state:
        st.session_state.creator_running = False
    if "creator_job_id" not in st.session_state:
        st.session_state.creator_job_id = None
    if "creator_result" not in st.session_state:
        st.session_state.creator_result = None

    # Input form
    submit_button, uploaded_file, prompt, max_iterations, context_input = _render_input_form()

    # Handle submission
    if submit_button and prompt:
        st.session_state.creator_result = None
        job_id = _submit_job(uploaded_file, prompt, max_iterations, context_input)

        if job_id:
            st.session_state.creator_job_id = job_id
            st.session_state.creator_running = True
            st.rerun()

    # Show progress if running
    if st.session_state.get("creator_running", False):
        _render_progress()

    # Show results
    _render_results()
