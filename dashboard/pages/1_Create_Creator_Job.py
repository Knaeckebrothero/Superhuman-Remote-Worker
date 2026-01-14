"""Create Creator Job Page.

Form to create a new job for the Creator agent to process.
"""

import streamlit as st
import json

import sys
sys.path.insert(0, "..")

from db import create_creator_job


st.set_page_config(
    page_title="Create Creator Job",
    page_icon="",
    layout="wide",
)

st.header("Create Creator Job")
st.caption("Submit a new job for the Creator agent to extract requirements from documents")

with st.form("creator_job_form"):
    # Prompt (required)
    prompt = st.text_area(
        "Prompt *",
        placeholder="Describe the requirement extraction task...\n\nExample: Extract all GoBD-relevant requirements from the attached document and identify compliance obligations.",
        height=150,
        help="Required. Describe what you want the Creator agent to extract or analyze.",
    )

    # Document upload (mockup)
    st.subheader("Documents")
    uploaded_files = st.file_uploader(
        "Upload documents (up to 10)",
        accept_multiple_files=True,
        type=["pdf", "docx", "txt", "md"],
        help="Upload documents for the agent to process. Currently a placeholder - file storage requires shared volume configuration.",
    )

    # TODO: Implement actual file storage when shared volume is configured
    # For now, files are uploaded but not persisted to disk.
    # When deployed on Kubernetes with shared storage (e.g., Longhorn),
    # files should be saved to the workspace volume accessible by agents.

    if uploaded_files:
        if len(uploaded_files) > 10:
            st.error("Maximum 10 files allowed per job")
        else:
            st.info(f"Selected {len(uploaded_files)} file(s):")
            for f in uploaded_files:
                st.caption(f"  - {f.name} ({f.size} bytes)")

    # Document path (alternative to upload)
    st.caption("Or specify existing document paths on the server:")
    document_path = st.text_input(
        "Document Path (optional)",
        placeholder="/app/data/document.pdf",
        help="Path to an existing document on the server/container",
    )

    # Context (optional)
    st.subheader("Additional Context")
    context_json = st.text_area(
        "Context JSON (optional)",
        placeholder='{"domain": "car_rental", "region": "EU", "focus": "GDPR"}',
        height=100,
        help="Optional JSON object with additional context for the agent",
    )

    # Submit
    submitted = st.form_submit_button("Create Job", use_container_width=True, type="primary")

    if submitted:
        # Validate
        if not prompt or not prompt.strip():
            st.error("Prompt is required")
        elif uploaded_files and len(uploaded_files) > 10:
            st.error("Maximum 10 files allowed")
        else:
            # Parse context
            context = None
            if context_json and context_json.strip():
                try:
                    context = json.loads(context_json)
                except json.JSONDecodeError as e:
                    st.error(f"Invalid JSON in context: {e}")
                    st.stop()

            # Build document paths
            doc_paths = []
            if document_path and document_path.strip():
                doc_paths.append(document_path.strip())

            # TODO: When file storage is implemented, save uploaded files and add paths
            if uploaded_files:
                st.warning(
                    "File upload is currently a mockup. Files are not persisted. "
                    "Use 'Document Path' to reference existing files on the server."
                )
                # Future implementation:
                # workspace_dir = Path("/app/workspace/uploads")
                # for f in uploaded_files:
                #     save_path = workspace_dir / f.name
                #     save_path.write_bytes(f.read())
                #     doc_paths.append(str(save_path))

            try:
                job_id = create_creator_job(
                    prompt=prompt.strip(),
                    document_paths=doc_paths if doc_paths else None,
                    context=context,
                )
                st.success(f"Job created successfully!")
                st.code(str(job_id))

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("View Job Details"):
                        st.query_params["job_id"] = str(job_id)
                        st.switch_page("pages/3_Job_Details.py")
                with col2:
                    if st.button("Create Another"):
                        st.rerun()

            except ValueError as e:
                st.error(f"Validation error: {e}")
            except Exception as e:
                st.error(f"Failed to create job: {e}")

# Help section
with st.expander("Help"):
    st.markdown("""
    ### What does the Creator agent do?

    The Creator agent processes documents and extracts requirements based on your prompt.
    It identifies:
    - Functional and non-functional requirements
    - GoBD/GDPR compliance obligations
    - Dependencies between requirements
    - Priority levels

    ### Tips for good prompts

    - Be specific about what type of requirements you're looking for
    - Mention the domain context (e.g., "car rental business")
    - Specify any compliance frameworks to focus on (GoBD, GDPR, etc.)
    - Indicate the expected output format if needed

    ### Example prompts

    1. "Extract all data retention requirements from this GDPR policy document"
    2. "Identify GoBD-relevant requirements for the invoicing module"
    3. "Analyze this contract and extract all technical requirements"
    """)
