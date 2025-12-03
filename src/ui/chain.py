"""
Chain page for requirement analysis using simple LangChain one-shot chain.
"""
import json
import traceback

import streamlit as st

from src.chain_example import SimpleChain
from src.ui import get_neo4j_connection, require_connection, load_config, load_prompt


def render():
    """Render the chain analysis page."""
    st.title("Chain Analysis")
    st.markdown("""
    Analyze requirements using the **simple one-shot chain**.
    This demonstrates a linear approach: plan → generate queries → execute → analyze.

    *Note: Unlike the Agent, the chain cannot refine its approach if queries fail.*
    """)

    # Check connection
    if not require_connection():
        return

    conn = get_neo4j_connection()

    # Load config
    try:
        config = load_config("chain_config.json")
        domain_context = load_prompt("chain_domain.txt")
    except FileNotFoundError as e:
        st.error(f"Configuration file not found: {e}")
        return

    # Initialize session state
    if "chain_running" not in st.session_state:
        st.session_state.chain_running = False
    if "chain_result" not in st.session_state:
        st.session_state.chain_result = None

    # Input area
    st.subheader("Requirement Input")
    requirement_input = st.text_area(
        "Enter requirement to analyze:",
        height=100,
        placeholder="e.g., Which requirements are marked as GoBD-relevant?",
        disabled=st.session_state.chain_running
    )

    # Run button
    col1, col2 = st.columns([1, 5])
    with col1:
        run_button = st.button(
            "Run Analysis",
            type="primary",
            disabled=st.session_state.chain_running or not requirement_input
        )

    # Execute analysis
    if run_button and requirement_input:
        st.session_state.chain_running = True
        st.session_state.chain_result = None

        # Initialize chain with custom domain context
        chain = SimpleChain(
            neo4j_connection=conn,
            model=config.get("model", "gpt-4o-mini"),
            temperature=config.get("temperature", 0.2)
        )

        # Override domain context if loaded
        if domain_context:
            chain.DOMAIN_CONTEXT = domain_context

        # Result tracking
        result_data = {
            "requirement": requirement_input,
            "plan": "",
            "queries": [],
            "query_results": [],
            "output": None
        }

        try:
            with st.status("Running analysis...", expanded=True) as status:
                # Step 1: Generate Plan
                st.write("**Step 1: Generating verification plan...**")
                plan = chain._step1_generate_plan(requirement_input)
                result_data["plan"] = plan

                with st.expander("View Plan", expanded=True):
                    st.markdown(plan)
                st.success("Plan generated!")

                # Step 2: Generate Queries
                st.write("**Step 2: Generating Cypher queries...**")
                schema = conn.get_database_schema()
                queries = chain._step2_generate_queries(plan, schema)
                result_data["queries"] = queries

                with st.expander("View Queries", expanded=True):
                    if queries:
                        for i, query in enumerate(queries, 1):
                            st.code(query, language="cypher")
                    else:
                        st.warning("No queries were generated.")
                st.success(f"Generated {len(queries)} queries!")

                # Step 3: Execute Queries
                st.write("**Step 3: Executing queries against database...**")
                query_results = chain._step3_execute_queries(queries)
                result_data["query_results"] = query_results

                with st.expander("View Query Results", expanded=True):
                    for i, result in enumerate(query_results, 1):
                        st.write(f"**Query {i}:**")
                        if result["success"]:
                            st.success(f"Success - {result['count']} results")
                            if result["results"]:
                                st.json(result["results"][:10])
                                if result["count"] > 10:
                                    st.caption(f"... and {result['count'] - 10} more")
                            else:
                                st.info("No data returned")
                        else:
                            st.error(f"Failed: {result['error']}")

                successful = sum(1 for r in query_results if r["success"])
                st.success(f"Executed {len(query_results)} queries ({successful} successful)")

                # Step 4: Generate Analysis
                st.write("**Step 4: Generating structured analysis...**")
                output = chain._step4_generate_analysis(requirement_input, query_results)
                result_data["output"] = output.model_dump()

                status.update(label="Analysis complete!", state="complete", expanded=False)

            # Display final output
            st.divider()
            st.subheader("Analysis Results")

            # Conversational Summary
            st.info(output.conversational_summary.message)

            # Evaluation
            col1, col2 = st.columns(2)
            with col1:
                verdict_color = {
                    "Satisfied": "green",
                    "Partially Satisfied": "orange",
                    "Not Satisfied": "red"
                }.get(output.evaluation.verdict, "gray")

                st.markdown(f"**Verdict:** :{verdict_color}[{output.evaluation.verdict}]")

            with col2:
                risk_color = {
                    "Low": "green",
                    "Medium": "orange",
                    "High": "red"
                }.get(output.analysis.risk_assessment.level, "gray")

                st.markdown(f"**Risk Level:** :{risk_color}[{output.analysis.risk_assessment.level}]")

            # Reasoning
            with st.expander("Evaluation Details"):
                st.markdown(output.evaluation.summary_reasoning)

            # Knowledge Retrieval
            with st.expander("Knowledge Retrieval"):
                st.write("**Found Facts:**")
                for fact in output.knowledge_retrieval.found_facts:
                    st.write(f"- {fact}")

                if output.knowledge_retrieval.missing_elements:
                    st.write("**Missing Elements:**")
                    for missing in output.knowledge_retrieval.missing_elements:
                        st.write(f"- {missing}")

            # Compliance Matrix
            with st.expander("Compliance Matrix"):
                for check in output.analysis.compliance_matrix:
                    result_icon = "✅" if check.result.lower() in ("yes", "met", "true") else "❌"
                    st.write(f"{result_icon} **{check.criteria}**: {check.result}")
                    st.caption(check.observation)

            # Recommendations
            with st.expander("Recommendations"):
                for item in output.recommendations.action_items:
                    st.write(f"- {item}")

            # Store result
            st.session_state.chain_result = result_data

        except Exception as e:
            st.error(f"An error occurred: {str(e)}")
            st.code(traceback.format_exc())

        finally:
            st.session_state.chain_running = False

    # Show export button if result available
    if st.session_state.chain_result:
        st.divider()

        result_json = json.dumps(st.session_state.chain_result, indent=2, default=str)

        st.download_button(
            label="Export JSON",
            data=result_json,
            file_name="chain_analysis.json",
            mime="application/json"
        )
