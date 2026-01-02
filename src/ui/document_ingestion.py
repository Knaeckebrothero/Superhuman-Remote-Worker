"""
Document Ingestion page for multi-agent document processing pipeline.
Upload documents and extract requirements into the Neo4j knowledge graph.
"""

import json
import os
import tempfile
import traceback
from pathlib import Path

import streamlit as st

from src.ui import get_neo4j_connection, require_connection, load_config
from src.core.document_models import ProcessingOptions, ValidationStatus


# Custom CSS
CUSTOM_CSS = """
<style>
    .stage-box {
        padding: 15px;
        border-radius: 8px;
        margin: 10px 0;
    }
    .stage-pending {
        background-color: #2d2d2d;
        border-left: 4px solid #666;
    }
    .stage-running {
        background-color: #1e3a5f;
        border-left: 4px solid #3b82f6;
    }
    .stage-completed {
        background-color: #1e3d1e;
        border-left: 4px solid #22c55e;
    }
    .stage-failed {
        background-color: #3d1e1e;
        border-left: 4px solid #ef4444;
    }
    .metric-card {
        background-color: #1e1e1e;
        padding: 15px;
        border-radius: 8px;
        text-align: center;
    }
    .requirement-card {
        background-color: #2d2d2d;
        padding: 12px;
        border-radius: 6px;
        margin: 8px 0;
        border-left: 3px solid #3b82f6;
    }
    .requirement-accepted {
        border-left-color: #22c55e;
    }
    .requirement-review {
        border-left-color: #f59e0b;
    }
    .requirement-rejected {
        border-left-color: #ef4444;
    }
</style>
"""


def render():
    """Render the document ingestion page."""
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    st.title("Document Ingestion Pipeline")
    st.markdown("""
    Upload documents (PDF, DOCX, TXT) to automatically extract requirements
    and integrate them into the Neo4j knowledge graph.
    """)

    # Check connection
    if not require_connection():
        return

    conn = get_neo4j_connection()

    # Load config
    try:
        config = load_config("llm_config.json")
        doc_config = config.get("document_ingestion", config.get("agent", {}))
    except FileNotFoundError as e:
        st.error(f"Configuration file not found: {e}")
        return

    # Initialize session state
    if "pipeline_running" not in st.session_state:
        st.session_state.pipeline_running = False
    if "pipeline_result" not in st.session_state:
        st.session_state.pipeline_result = None
    if "uploaded_file_path" not in st.session_state:
        st.session_state.uploaded_file_path = None

    # Sidebar: Pipeline Configuration
    with st.sidebar:
        st.header("Pipeline Settings")

        extraction_mode = st.selectbox(
            "Extraction Mode",
            options=["balanced", "strict", "permissive"],
            index=0,
            help="strict: High precision, balanced: Mix of precision/recall, permissive: High recall"
        )

        min_confidence = st.slider(
            "Minimum Confidence",
            min_value=0.0,
            max_value=1.0,
            value=doc_config.get("min_confidence", 0.6),
            step=0.1,
            help="Minimum confidence score to include candidates"
        )

        chunking_strategy = st.selectbox(
            "Chunking Strategy",
            options=["legal", "technical", "general"],
            index=0,
            help="legal: Respects legal document structure, technical: For specs/docs, general: Simple chunking"
        )

        max_chunk_size = st.number_input(
            "Max Chunk Size (chars)",
            min_value=200,
            max_value=2000,
            value=doc_config.get("max_chunk_size", 1000),
            step=100
        )

        auto_integrate = st.checkbox(
            "Auto-integrate accepted requirements",
            value=doc_config.get("auto_integrate", False),
            help="If enabled, automatically add accepted requirements to the graph"
        )

        st.divider()
        st.caption("Advanced Settings")

        duplicate_threshold = st.slider(
            "Duplicate Threshold",
            min_value=0.8,
            max_value=1.0,
            value=doc_config.get("duplicate_similarity_threshold", 0.95),
            step=0.01,
            help="Similarity threshold for duplicate detection"
        )

    # Main content: File Upload
    st.subheader("Upload Document")

    uploaded_file = st.file_uploader(
        "Choose a document",
        type=["pdf", "docx", "txt", "html"],
        disabled=st.session_state.pipeline_running
    )

    # Show file info
    if uploaded_file:
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("File Name", uploaded_file.name)
        with col2:
            size_kb = uploaded_file.size / 1024
            st.metric("Size", f"{size_kb:.1f} KB")
        with col3:
            file_type = Path(uploaded_file.name).suffix.upper()
            st.metric("Type", file_type)

    # Process button
    process_button = st.button(
        "Process Document",
        type="primary",
        disabled=st.session_state.pipeline_running or not uploaded_file
    )

    # Process document
    if process_button and uploaded_file:
        st.session_state.pipeline_running = True
        st.session_state.pipeline_result = None

        # Save uploaded file temporarily
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=Path(uploaded_file.name).suffix
        ) as tmp_file:
            tmp_file.write(uploaded_file.getvalue())
            st.session_state.uploaded_file_path = tmp_file.name

        # Build processing options
        options = ProcessingOptions(
            extraction_mode=extraction_mode,
            min_confidence=min_confidence,
            chunking_strategy=chunking_strategy,
            max_chunk_size=max_chunk_size,
            chunk_overlap=200,
            auto_integrate=auto_integrate,
            duplicate_similarity_threshold=duplicate_threshold,
        )

        # Create progress containers
        st.divider()
        st.subheader("Pipeline Progress")

        stage_status = st.empty()
        progress_bar = st.progress(0)
        current_stage = st.empty()
        details_container = st.container()

        try:
            # Import here to avoid circular imports
            from src.agents.document_ingestion_supervisor import (
                create_document_ingestion_supervisor,
            )

            # Create supervisor
            supervisor = create_document_ingestion_supervisor(
                neo4j_connection=conn,
                options=options,
            )

            # Stream processing
            stages_completed = 0
            total_stages = 4

            stream = supervisor.process_document_stream(
                document_path=st.session_state.uploaded_file_path,
                options=options,
            )

            final_state = None

            for event in stream:
                for node_name, state in event.items():
                    final_state = state

                    # Update progress based on node
                    if node_name == "document_processing":
                        if state.get("document_processing_status") == "completed":
                            stages_completed = max(stages_completed, 1)
                            progress_bar.progress(0.25)
                            current_stage.info("Stage 1: Document Processing - Complete")
                            with details_container:
                                chunks = state.get("chunks", [])
                                st.success(f"Extracted {len(chunks)} chunks from document")

                    elif node_name == "extraction":
                        if state.get("extraction_status") == "completed":
                            stages_completed = max(stages_completed, 2)
                            progress_bar.progress(0.50)
                            current_stage.info("Stage 2: Requirement Extraction - Complete")
                            with details_container:
                                candidates = state.get("candidates", [])
                                stats = state.get("extraction_stats", {})
                                st.success(f"Extracted {len(candidates)} requirement candidates")
                                if stats:
                                    col1, col2, col3 = st.columns(3)
                                    with col1:
                                        st.metric("High Confidence", stats.get("high_confidence", 0))
                                    with col2:
                                        st.metric("Medium Confidence", stats.get("medium_confidence", 0))
                                    with col3:
                                        st.metric("GoBD Relevant", stats.get("gobd_relevant", 0))

                    elif node_name == "validation":
                        if state.get("validation_status") == "completed":
                            stages_completed = max(stages_completed, 3)
                            progress_bar.progress(0.75)
                            current_stage.info("Stage 3: Validation - Complete")
                            with details_container:
                                validated = state.get("validated_requirements", [])
                                rejected = state.get("rejected_candidates", [])
                                accepted = len([v for v in validated if v.get("validation_status") == "accepted"])
                                needs_review = len([v for v in validated if v.get("validation_status") == "needs_review"])
                                st.success(f"Validated: {accepted} accepted, {needs_review} need review, {len(rejected)} rejected")

                    elif node_name == "integration":
                        if state.get("integration_status") == "completed":
                            stages_completed = max(stages_completed, 4)
                            progress_bar.progress(1.0)
                            current_stage.info("Stage 4: Integration - Complete")
                            with details_container:
                                results = state.get("integration_results", [])
                                success_count = len([r for r in results if r.get("success")])
                                st.success(f"Integrated {success_count} requirements to graph")

                    elif node_name == "finalize":
                        progress_bar.progress(1.0)
                        current_stage.success("Pipeline Complete!")

            # Store final result
            if final_state:
                st.session_state.pipeline_result = supervisor._build_report(final_state)

        except Exception as e:
            st.error(f"Pipeline error: {str(e)}")
            with st.expander("Error Details"):
                st.code(traceback.format_exc())

        finally:
            st.session_state.pipeline_running = False
            # Cleanup temp file
            if st.session_state.uploaded_file_path and os.path.exists(st.session_state.uploaded_file_path):
                try:
                    os.unlink(st.session_state.uploaded_file_path)
                except Exception:
                    pass

    # Display results
    if st.session_state.pipeline_result:
        result = st.session_state.pipeline_result

        st.divider()
        st.subheader("Pipeline Results")

        # Summary metrics
        summary = result.get("summary", {})
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Chunks Processed", summary.get("chunks_processed", 0))
        with col2:
            st.metric("Candidates Extracted", summary.get("candidates_extracted", 0))
        with col3:
            st.metric("Accepted", summary.get("accepted_count", 0))
        with col4:
            st.metric("Integrated", summary.get("integrated_count", 0))

        # Tabs for detailed views
        tab1, tab2, tab3, tab4 = st.tabs([
            "Validated Requirements",
            "Rejected Candidates",
            "Integration Results",
            "Raw Report"
        ])

        with tab1:
            validated = result.get("validated_requirements", [])
            if validated:
                for req in validated:
                    status = req.get("validation_status", "unknown")
                    candidate = req.get("candidate", {})

                    status_color = {
                        "accepted": "requirement-accepted",
                        "needs_review": "requirement-review",
                        "rejected": "requirement-rejected",
                    }.get(status, "")

                    with st.expander(
                        f"[{req.get('suggested_rid', 'NEW')}] {candidate.get('text', '')[:80]}... "
                        f"({status.upper()})"
                    ):
                        st.markdown(f"**Full Text:** {candidate.get('text', '')}")
                        st.markdown(f"**Type:** {candidate.get('requirement_type', 'unknown')}")
                        st.markdown(f"**Confidence:** {candidate.get('confidence_score', 0):.2f}")
                        st.markdown(f"**Validation Score:** {req.get('validation_score', 0):.2f}")
                        st.markdown(f"**GoBD Relevant:** {candidate.get('gobd_relevant', False)}")

                        if req.get("similar_requirements"):
                            st.markdown("**Similar Requirements:**")
                            for sim in req["similar_requirements"][:3]:
                                st.markdown(f"  - [{sim['rid']}] {sim['name']} ({sim['similarity_score']:.1%})")

                        if req.get("resolved_objects"):
                            st.markdown("**Resolved Business Objects:**")
                            for obj in req["resolved_objects"]:
                                st.markdown(f"  - {obj['name']} ({obj['entity_id']})")

                        if req.get("compliance_warnings"):
                            st.warning("Compliance Warnings: " + ", ".join(req["compliance_warnings"]))
            else:
                st.info("No validated requirements")

        with tab2:
            rejected = result.get("rejected_candidates", [])
            if rejected:
                for rej in rejected:
                    candidate = rej.get("candidate", {})
                    with st.expander(f"[REJECTED] {candidate.get('text', '')[:80]}..."):
                        st.markdown(f"**Reason:** {rej.get('rejection_reason', 'unknown')}")
                        st.markdown(f"**Details:** {rej.get('rejection_details', '')}")
                        st.markdown(f"**Original Text:** {candidate.get('text', '')}")
            else:
                st.info("No rejected candidates")

        with tab3:
            integration = result.get("integration_results", [])
            if integration:
                for res in integration:
                    icon = ":white_check_mark:" if res.get("success") else ":x:"
                    st.markdown(f"{icon} **{res.get('requirement_id', 'unknown')}**")
                    if res.get("created_relationships"):
                        st.markdown(f"  Created relationships: {', '.join(res['created_relationships'])}")
                    if res.get("error_message"):
                        st.error(f"  Error: {res['error_message']}")
            else:
                st.info("No integration results (auto-integrate may be disabled)")

        with tab4:
            st.json(result)

        # Export button
        st.divider()
        result_json = json.dumps(result, indent=2, default=str)
        st.download_button(
            label="Export Full Report (JSON)",
            data=result_json,
            file_name="ingestion_report.json",
            mime="application/json"
        )

        # Show errors if any
        errors = result.get("errors", [])
        if errors:
            with st.expander(f"Errors ({len(errors)})", expanded=False):
                for error in errors:
                    st.error(error)
