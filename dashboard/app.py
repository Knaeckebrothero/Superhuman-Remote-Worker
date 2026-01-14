"""Simple Streamlit dashboard for Graph-RAG job management."""

import streamlit as st

from db import (
    create_job,
    list_jobs,
    get_job,
    delete_job,
    assign_to_creator,
    assign_to_validator,
    get_requirements,
    get_requirement_summary,
    get_job_stats,
)
from agents import get_all_agent_status


st.set_page_config(
    page_title="Graph-RAG Dashboard",
    page_icon="",
    layout="wide",
)

# Initialize session state
if "selected_job_id" not in st.session_state:
    st.session_state.selected_job_id = None


def render_sidebar():
    """Render the sidebar with agent status and navigation."""
    with st.sidebar:
        st.title("Graph-RAG")

        # Agent Status
        st.subheader("Agent Status")
        agents = get_all_agent_status()

        for name, info in agents.items():
            status_icon = "" if info["online"] else ""
            st.markdown(f"{status_icon} **{name.title()}**")
            if info["online"]:
                st.caption(f"URL: {info['url']}")
            else:
                st.caption("Offline")

        st.divider()

        # Job Stats
        st.subheader("Job Statistics")
        try:
            stats = get_job_stats()
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Total", stats["total_jobs"])
                st.metric("Processing", stats["processing"])
            with col2:
                st.metric("Completed", stats["completed"])
                st.metric("Failed", stats["failed"])
        except Exception as e:
            st.error(f"DB Error: {e}")

        st.divider()

        # Navigation
        st.subheader("Navigation")
        if st.button("Jobs List", use_container_width=True):
            st.session_state.selected_job_id = None
            st.session_state.page = "list"
        if st.button("Create Job", use_container_width=True):
            st.session_state.selected_job_id = None
            st.session_state.page = "create"

        # Auto-refresh
        st.divider()
        auto_refresh = st.checkbox("Auto-refresh (30s)", value=False)
        if auto_refresh:
            st.rerun()


def render_job_list():
    """Render the jobs list view."""
    st.header("Jobs")

    # Filters
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        status_filter = st.selectbox(
            "Filter by status",
            ["All", "created", "processing", "completed", "failed", "cancelled"],
        )
    with col3:
        if st.button("Refresh"):
            st.rerun()

    # Get jobs
    try:
        filter_value = None if status_filter == "All" else status_filter
        jobs = list_jobs(status_filter=filter_value)
    except Exception as e:
        st.error(f"Database error: {e}")
        return

    if not jobs:
        st.info("No jobs found.")
        return

    # Display jobs table
    for job in jobs:
        with st.container():
            col1, col2, col3, col4 = st.columns([3, 1, 1, 2])

            with col1:
                # Truncate prompt
                prompt = job["prompt"] or ""
                display_prompt = prompt[:80] + "..." if len(prompt) > 80 else prompt
                st.markdown(f"**{display_prompt}**")
                st.caption(f"ID: `{job['id']}`")

            with col2:
                # Status badges
                status = job["status"]
                status_colors = {
                    "created": "",
                    "processing": "",
                    "completed": "",
                    "failed": "",
                    "cancelled": "",
                }
                st.markdown(f"{status_colors.get(status, '')} {status}")

            with col3:
                # Agent status
                creator = job["creator_status"]
                validator = job["validator_status"]
                st.caption(f"Creator: {creator}")
                st.caption(f"Validator: {validator}")

            with col4:
                # Actions
                btn_col1, btn_col2, btn_col3 = st.columns(3)
                with btn_col1:
                    if st.button("View", key=f"view_{job['id']}"):
                        st.session_state.selected_job_id = str(job["id"])
                        st.session_state.page = "detail"
                        st.rerun()
                with btn_col2:
                    if st.button("Assign", key=f"assign_{job['id']}"):
                        st.session_state.selected_job_id = str(job["id"])
                        st.session_state.page = "assign"
                        st.rerun()
                with btn_col3:
                    if st.button("Del", key=f"del_{job['id']}"):
                        if delete_job(job["id"]):
                            st.success("Deleted")
                            st.rerun()
                        else:
                            st.error("Failed")

            st.divider()


def render_create_job():
    """Render the job creation form."""
    st.header("Create New Job")

    with st.form("create_job_form"):
        prompt = st.text_area(
            "Prompt",
            placeholder="Enter the task prompt for the agent...",
            height=150,
        )
        document_path = st.text_input(
            "Document Path (optional)",
            placeholder="/app/data/document.pdf",
        )

        submitted = st.form_submit_button("Create Job", use_container_width=True)

        if submitted:
            if not prompt:
                st.error("Prompt is required.")
            else:
                try:
                    doc_path = document_path if document_path else None
                    job_id = create_job(prompt=prompt, document_path=doc_path)
                    st.success(f"Job created: `{job_id}`")
                    st.session_state.selected_job_id = str(job_id)
                    st.session_state.page = "detail"
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to create job: {e}")


def render_job_detail():
    """Render job detail view."""
    job_id = st.session_state.selected_job_id
    if not job_id:
        st.warning("No job selected.")
        return

    try:
        job = get_job(job_id)
    except Exception as e:
        st.error(f"Database error: {e}")
        return

    if not job:
        st.error("Job not found.")
        return

    # Header
    st.header(f"Job Details")
    st.caption(f"ID: `{job['id']}`")

    # Status overview
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Status", job["status"])
    with col2:
        st.metric("Creator", job["creator_status"])
    with col3:
        st.metric("Validator", job["validator_status"])

    # Timestamps
    st.subheader("Timeline")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.caption(f"Created: {job['created_at']}")
    with col2:
        st.caption(f"Updated: {job['updated_at']}")
    with col3:
        if job["completed_at"]:
            st.caption(f"Completed: {job['completed_at']}")

    # Prompt
    st.subheader("Prompt")
    st.text(job["prompt"])

    if job["document_path"]:
        st.caption(f"Document: {job['document_path']}")

    # Error info
    if job["error_message"]:
        st.error(f"Error: {job['error_message']}")

    # Requirements summary
    st.subheader("Requirements")
    try:
        summary = get_requirement_summary(job_id)
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            st.metric("Pending", summary["pending"])
        with col2:
            st.metric("Validating", summary["validating"])
        with col3:
            st.metric("Integrated", summary["integrated"])
        with col4:
            st.metric("Rejected", summary["rejected"])
        with col5:
            st.metric("Failed", summary["failed"])

        # Progress bar
        total = summary["total"]
        if total > 0:
            integrated = summary["integrated"]
            progress = integrated / total
            st.progress(progress, text=f"{integrated}/{total} integrated")

        # Requirements table
        requirements = get_requirements(job_id)
        if requirements:
            st.subheader("Requirement Details")
            for req in requirements:
                with st.expander(f"{req['name'] or req['requirement_id'] or 'Unnamed'} - {req['status']}"):
                    st.text(req["text"])
                    col1, col2 = st.columns(2)
                    with col1:
                        st.caption(f"Type: {req['type']}")
                        st.caption(f"Priority: {req['priority']}")
                    with col2:
                        if req["gobd_relevant"]:
                            st.caption("GoBD Relevant")
                        if req["gdpr_relevant"]:
                            st.caption("GDPR Relevant")
                    if req["rejection_reason"]:
                        st.warning(f"Rejection: {req['rejection_reason']}")

    except Exception as e:
        st.error(f"Failed to load requirements: {e}")

    # Actions
    st.divider()
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("Back to List", use_container_width=True):
            st.session_state.page = "list"
            st.rerun()
    with col2:
        if st.button("Assign to Agent", use_container_width=True):
            st.session_state.page = "assign"
            st.rerun()
    with col3:
        if st.button("Delete Job", use_container_width=True, type="primary"):
            if delete_job(job_id):
                st.success("Job deleted")
                st.session_state.selected_job_id = None
                st.session_state.page = "list"
                st.rerun()


def render_assign_job():
    """Render job assignment view."""
    job_id = st.session_state.selected_job_id
    if not job_id:
        st.warning("No job selected.")
        return

    try:
        job = get_job(job_id)
    except Exception as e:
        st.error(f"Database error: {e}")
        return

    if not job:
        st.error("Job not found.")
        return

    st.header("Assign Job to Agent")
    st.caption(f"Job ID: `{job['id']}`")

    # Current status
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Status", job["status"])
    with col2:
        st.metric("Creator", job["creator_status"])
    with col3:
        st.metric("Validator", job["validator_status"])

    st.divider()

    # Assignment options
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Creator Agent")
        st.caption("Extracts requirements from documents")
        can_assign_creator = job["status"] in ("created", "failed")
        if can_assign_creator:
            if st.button("Assign to Creator", use_container_width=True, type="primary"):
                if assign_to_creator(job_id):
                    st.success("Assigned to Creator")
                    st.rerun()
                else:
                    st.error("Assignment failed")
        else:
            st.info(f"Cannot assign: status is '{job['status']}'")

    with col2:
        st.subheader("Validator Agent")
        st.caption("Validates and integrates requirements into graph")
        can_assign_validator = job["creator_status"] == "completed"
        if can_assign_validator:
            if st.button("Assign to Validator", use_container_width=True, type="primary"):
                if assign_to_validator(job_id):
                    st.success("Assigned to Validator")
                    st.rerun()
                else:
                    st.error("Assignment failed")
        else:
            st.info("Creator must complete first")

    st.divider()
    if st.button("Back to Job Details"):
        st.session_state.page = "detail"
        st.rerun()


def main():
    """Main application entry point."""
    render_sidebar()

    # Determine which page to show
    page = st.session_state.get("page", "list")

    if page == "create":
        render_create_job()
    elif page == "detail" and st.session_state.selected_job_id:
        render_job_detail()
    elif page == "assign" and st.session_state.selected_job_id:
        render_assign_job()
    else:
        render_job_list()


if __name__ == "__main__":
    main()
