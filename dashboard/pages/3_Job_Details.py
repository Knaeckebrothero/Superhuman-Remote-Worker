"""Job Details Page.

Detailed view of a specific job with progress, requirements, and actions.
"""

import streamlit as st

import sys
sys.path.insert(0, "..")

from db import (
    get_job,
    get_job_progress,
    get_requirements,
    get_requirement_statistics,
    delete_job,
    cancel_job,
    assign_to_creator,
    assign_to_validator,
)


st.set_page_config(
    page_title="Job Details",
    page_icon="",
    layout="wide",
)

# Get job_id from query params
job_id = st.query_params.get("job_id")

if not job_id:
    st.warning("No job selected")
    st.caption("Select a job from the dashboard or provide a job_id in the URL")

    # Allow manual entry
    manual_id = st.text_input("Enter Job ID:")
    if manual_id:
        st.query_params["job_id"] = manual_id
        st.rerun()

    if st.button("Go to Dashboard"):
        st.switch_page("app.py")
    st.stop()

# Load job data
try:
    job = get_job(job_id)
    progress = get_job_progress(job_id)
    req_stats = get_requirement_statistics(job_id)
except Exception as e:
    st.error(f"Error loading job: {e}")
    st.stop()

if not job:
    st.error(f"Job not found: {job_id}")
    if st.button("Go to Dashboard"):
        st.switch_page("app.py")
    st.stop()

# Header
st.header("Job Details")
st.caption(f"ID: `{job['id']}`")

# Status overview
col1, col2, col3, col4 = st.columns(4)

status_colors = {
    "created": "",
    "processing": "",
    "completed": "",
    "failed": "",
    "cancelled": "",
}

with col1:
    status = job["status"]
    st.metric("Status", f"{status_colors.get(status, '')} {status}")
with col2:
    st.metric("Creator", job["creator_status"])
with col3:
    st.metric("Validator", job["validator_status"])
with col4:
    if progress.get("progress_percent") is not None:
        st.metric("Progress", f"{progress['progress_percent']}%")

# Progress bar
if progress.get("requirements"):
    total = progress["requirements"]["total"]
    if total > 0:
        integrated = progress["requirements"]["integrated"]
        rejected = progress["requirements"]["rejected"]
        processed = integrated + rejected
        st.progress(processed / total, text=f"{processed}/{total} processed")

st.divider()

# Prompt and document
st.subheader("Task")
st.text(job["prompt"])

if job["document_path"]:
    st.caption(f"Document: {job['document_path']}")

if job.get("context"):
    with st.expander("Context"):
        st.json(job["context"])

st.divider()

# Timeline
st.subheader("Timeline")
col1, col2, col3 = st.columns(3)
with col1:
    st.caption(f"Created: {job['created_at']}")
with col2:
    st.caption(f"Updated: {job['updated_at']}")
with col3:
    if job["completed_at"]:
        st.caption(f"Completed: {job['completed_at']}")
    elif progress.get("eta_seconds"):
        eta_min = progress["eta_seconds"] / 60
        st.caption(f"ETA: ~{eta_min:.0f} min")

# Error info
if job["error_message"]:
    st.error(f"Error: {job['error_message']}")
    if job.get("error_details"):
        with st.expander("Error Details"):
            st.code(str(job["error_details"]))

st.divider()

# Requirements
st.subheader("Requirements")

if req_stats:
    # Summary metrics
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Total", req_stats.get("total", 0))
    with col2:
        st.metric("Pending", req_stats.get("pending", 0))
    with col3:
        st.metric("Integrated", req_stats.get("integrated", 0))
    with col4:
        st.metric("Rejected", req_stats.get("rejected", 0))
    with col5:
        st.metric("Failed", req_stats.get("failed", 0))

    # Additional stats
    with st.expander("Detailed Statistics"):
        col1, col2 = st.columns(2)
        with col1:
            st.caption("**By Priority**")
            st.write(f"High: {req_stats.get('high_priority', 0)}")
            st.write(f"Medium: {req_stats.get('medium_priority', 0)}")
            st.write(f"Low: {req_stats.get('low_priority', 0)}")
        with col2:
            st.caption("**Compliance Relevance**")
            st.write(f"GoBD: {req_stats.get('gobd_relevant', 0)}")
            st.write(f"GDPR: {req_stats.get('gdpr_relevant', 0)}")

# Requirements list
try:
    requirements = get_requirements(job_id)
except Exception as e:
    st.error(f"Error loading requirements: {e}")
    requirements = []

if requirements:
    with st.expander(f"View Requirements ({len(requirements)})"):
        for req in requirements:
            status_icon = {
                "pending": "",
                "validating": "",
                "integrated": "",
                "rejected": "",
                "failed": "",
            }.get(req["status"], "")

            st.markdown(f"{status_icon} **{req['name'] or req['requirement_id'] or 'Unnamed'}**")
            st.text(req["text"][:200] + "..." if len(req["text"] or "") > 200 else req["text"])

            col1, col2 = st.columns(2)
            with col1:
                st.caption(f"Type: {req['type']} | Priority: {req['priority']}")
            with col2:
                tags = []
                if req["gobd_relevant"]:
                    tags.append("GoBD")
                if req["gdpr_relevant"]:
                    tags.append("GDPR")
                if tags:
                    st.caption(f"Tags: {', '.join(tags)}")

            if req["rejection_reason"]:
                st.warning(f"Rejected: {req['rejection_reason']}")
            if req["neo4j_id"]:
                st.caption(f"Neo4j ID: {req['neo4j_id']}")

            st.divider()
else:
    st.info("No requirements extracted yet")

st.divider()

# Actions
st.subheader("Actions")

col1, col2, col3, col4 = st.columns(4)

with col1:
    if st.button("Back to Dashboard", use_container_width=True):
        st.switch_page("app.py")

with col2:
    # Reassign based on current state
    if job["status"] in ("created", "failed"):
        if st.button("Assign to Creator", use_container_width=True):
            try:
                if assign_to_creator(job_id):
                    st.success("Assigned to Creator")
                    st.rerun()
                else:
                    st.error("Assignment failed")
            except Exception as e:
                st.error(f"Error: {e}")
    elif job["creator_status"] == "completed" and job["validator_status"] != "completed":
        if st.button("Assign to Validator", use_container_width=True):
            try:
                if assign_to_validator(job_id):
                    st.success("Assigned to Validator")
                    st.rerun()
                else:
                    st.error("Assignment failed")
            except Exception as e:
                st.error(f"Error: {e}")

with col3:
    if job["status"] == "processing":
        if st.button("Cancel Job", use_container_width=True):
            try:
                if cancel_job(job_id):
                    st.success("Job cancelled")
                    st.rerun()
                else:
                    st.error("Cancellation failed")
            except Exception as e:
                st.error(f"Error: {e}")

with col4:
    if st.button("Delete Job", use_container_width=True, type="primary"):
        try:
            if delete_job(job_id):
                st.success("Job deleted")
                st.switch_page("app.py")
            else:
                st.error("Deletion failed")
        except Exception as e:
            st.error(f"Error: {e}")

# Refresh
st.divider()
if st.button("Refresh", use_container_width=True):
    st.rerun()
