"""Graph-RAG Dashboard - Landing Page.

Main dashboard showing job queue, agent status, and system overview.
Uses Streamlit's native multi-page app structure (pages/ folder).
"""

import streamlit as st

from db import (
    list_jobs,
    get_job_stats,
    delete_job,
    cancel_job,
    detect_stuck_jobs,
    get_daily_statistics,
)
from agents import get_all_agent_status


st.set_page_config(
    page_title="Graph-RAG Dashboard",
    page_icon="",
    layout="wide",
)


def render_sidebar():
    """Render the sidebar with agent status and quick stats."""
    with st.sidebar:
        st.title("Graph-RAG")

        # Agent Status
        st.subheader("Agent Status")
        agents = get_all_agent_status()

        for name, info in agents.items():
            status_icon = "" if info["online"] else ""
            st.markdown(f"{status_icon} **{name.title()}**")
            if info["online"]:
                st.caption(f"{info['url']}")
            else:
                st.caption("Offline")

        st.divider()

        # Quick Stats
        st.subheader("Quick Stats")
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

        # Auto-refresh
        auto_refresh = st.checkbox("Auto-refresh (30s)", value=False)
        if auto_refresh:
            import time
            time.sleep(30)
            st.rerun()


def render_processing_and_stuck():
    """Render processing jobs and stuck job warnings side by side."""
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Processing Jobs")
        try:
            processing_jobs = list_jobs(status_filter="processing", limit=10)
            if processing_jobs:
                for job in processing_jobs:
                    prompt = job["prompt"] or ""
                    display = prompt[:50] + "..." if len(prompt) > 50 else prompt
                    st.markdown(f"**{display}**")
                    st.caption(
                        f"Creator: {job['creator_status']} | "
                        f"Validator: {job['validator_status']}"
                    )
            else:
                st.info("No jobs currently processing")
        except Exception as e:
            st.error(f"Error: {e}")

    with col2:
        st.subheader("Stuck Jobs")
        try:
            stuck_jobs = detect_stuck_jobs(threshold_minutes=60)
            if stuck_jobs:
                for job in stuck_jobs:
                    st.warning(f"**{job['stuck_component'].upper()}**: {job['stuck_reason']}")
                    st.caption(f"Job: `{str(job['id'])[:8]}...` | Last update: {job['updated_at']}")
            else:
                st.success("No stuck jobs detected")
        except Exception as e:
            st.error(f"Error: {e}")


def render_job_queue():
    """Render the main job queue table."""
    st.subheader("Job Queue")

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
        jobs = list_jobs(status_filter=filter_value, limit=50)
    except Exception as e:
        st.error(f"Database error: {e}")
        return

    if not jobs:
        st.info("No jobs found.")
        return

    # Display jobs table
    for job in jobs:
        with st.container():
            col1, col2, col3, col4 = st.columns([4, 1, 1, 2])

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
                st.caption(f"C: {creator}")
                st.caption(f"V: {validator}")

            with col4:
                # Actions
                btn_col1, btn_col2, btn_col3 = st.columns(3)
                with btn_col1:
                    if st.button("View", key=f"view_{job['id']}"):
                        st.query_params["job_id"] = str(job["id"])
                        st.switch_page("pages/3_Job_Details.py")
                with btn_col2:
                    if job["status"] == "processing":
                        if st.button("Stop", key=f"stop_{job['id']}"):
                            if cancel_job(job["id"]):
                                st.success("Cancelled")
                                st.rerun()
                with btn_col3:
                    if st.button("Del", key=f"del_{job['id']}"):
                        if delete_job(job["id"]):
                            st.rerun()

            st.divider()


def render_daily_stats():
    """Render daily statistics summary."""
    st.subheader("Daily Statistics (7 days)")

    try:
        stats = get_daily_statistics(days=7)
        if stats:
            # Create a simple table view
            for day_stat in stats:
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.caption(str(day_stat["date"]))
                with col2:
                    st.metric("Created", day_stat["jobs_created"], label_visibility="collapsed")
                with col3:
                    st.metric("Completed", day_stat["jobs_completed"], label_visibility="collapsed")
                with col4:
                    st.metric("Failed", day_stat["jobs_failed"], label_visibility="collapsed")
        else:
            st.info("No job history yet")
    except Exception as e:
        st.error(f"Error loading statistics: {e}")


def main():
    """Main application entry point."""
    render_sidebar()

    st.header("Dashboard")

    # Processing and stuck jobs side by side
    render_processing_and_stuck()

    st.divider()

    # Job queue
    render_job_queue()

    st.divider()

    # Daily stats
    render_daily_stats()


if __name__ == "__main__":
    main()
