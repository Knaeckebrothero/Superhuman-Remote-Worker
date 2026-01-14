"""Create Validator Job Page.

Interface to trigger validation for jobs that have completed the creator phase.
"""

import streamlit as st

import sys
sys.path.insert(0, "..")

from db import (
    get_jobs_ready_for_validation,
    create_validator_job,
    get_requirement_summary,
)


st.set_page_config(
    page_title="Create Validator Job",
    page_icon="",
    layout="wide",
)

st.header("Trigger Validation")
st.caption("Select jobs to validate and integrate into the knowledge graph")

# Get jobs ready for validation
try:
    ready_jobs = get_jobs_ready_for_validation()
except Exception as e:
    st.error(f"Database error: {e}")
    ready_jobs = []

if not ready_jobs:
    st.info("No jobs are ready for validation.")
    st.caption("Jobs become ready for validation when the Creator agent has completed processing.")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Go to Dashboard"):
            st.switch_page("app.py")
    with col2:
        if st.button("Create New Creator Job"):
            st.switch_page("pages/1_Create_Creator_Job.py")
else:
    st.success(f"Found {len(ready_jobs)} job(s) ready for validation")

    # Display jobs
    for job in ready_jobs:
        with st.container():
            col1, col2, col3 = st.columns([4, 2, 1])

            with col1:
                prompt = job["prompt"] or ""
                display = prompt[:100] + "..." if len(prompt) > 100 else prompt
                st.markdown(f"**{display}**")
                st.caption(f"ID: `{job['id']}`")
                if job["document_path"]:
                    st.caption(f"Document: {job['document_path']}")

            with col2:
                st.metric("Requirements", job["requirement_count"])
                st.caption(f"Created: {job['created_at']}")

            with col3:
                if st.button("Validate", key=f"validate_{job['id']}", type="primary"):
                    try:
                        success = create_validator_job(job["id"])
                        if success:
                            st.success("Validation triggered!")
                            st.rerun()
                        else:
                            st.error("Failed to trigger validation")
                    except Exception as e:
                        st.error(f"Error: {e}")

                if st.button("View", key=f"view_{job['id']}"):
                    st.query_params["job_id"] = str(job["id"])
                    st.switch_page("pages/3_Job_Details.py")

            st.divider()

    # Bulk actions
    st.subheader("Bulk Actions")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Validate All", use_container_width=True):
            success_count = 0
            fail_count = 0
            for job in ready_jobs:
                try:
                    if create_validator_job(job["id"]):
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception:
                    fail_count += 1

            if success_count > 0:
                st.success(f"Triggered validation for {success_count} job(s)")
            if fail_count > 0:
                st.warning(f"Failed to trigger {fail_count} job(s)")
            st.rerun()

    with col2:
        if st.button("Refresh", use_container_width=True):
            st.rerun()


# Help section
with st.expander("Help"):
    st.markdown("""
    ### What does the Validator agent do?

    The Validator agent takes requirements extracted by the Creator and:

    1. **Relevance Check**: Determines if requirements are relevant to the FINIUS metamodel
    2. **Fulfillment Check**: Checks if requirements are already fulfilled by existing business objects
    3. **Graph Integration**: Integrates validated requirements into the Neo4j knowledge graph
    4. **Relationship Mapping**: Creates relationships (REFINES, DEPENDS_ON, TRACES_TO, etc.)

    ### Validation Status Flow

    ```
    pending → validating → integrated
                        → rejected (with reason)
                        → failed (error)
    ```

    ### When to trigger validation

    - After Creator has finished extracting requirements
    - When you want to integrate new requirements into the graph
    - To re-validate failed requirements after fixing issues
    """)
