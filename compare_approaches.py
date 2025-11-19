#!/usr/bin/env python3
"""
Comparison Script: Chain vs Agent Approaches
Run both approaches on a sample requirement to compare their behavior.
"""

import os
import sys
from dotenv import load_dotenv

def compare_approaches(requirement_text: str):
    """
    Compare chain and agent approaches on a single requirement.

    Args:
        requirement_text: The requirement to analyze
    """
    load_dotenv()

    csv_path = os.getenv('CSV_FILE_PATH')
    if not csv_path:
        print("Error: CSV_FILE_PATH not set in environment variables")
        sys.exit(1)

    print("\n" + "="*80)
    print("COMPARING CHAIN VS AGENT APPROACHES")
    print("="*80)
    print(f"\nRequirement: {requirement_text}\n")

    results = {}

    # ========================================================================
    # Test 1: Simple Chain Approach
    # ========================================================================
    print("\n" + "="*80)
    print("TEST 1: SIMPLE CHAIN APPROACH")
    print("="*80)
    print("Linear workflow: Refine -> Query -> Analyze\n")

    try:
        from src.neo4j_utils import create_neo4j_connection
        from src.requirement_agent import create_requirement_agent

        neo4j = create_neo4j_connection()
        try:
            if not neo4j.connect():
                print("Failed to connect to Neo4j")
                results['chain'] = {'error': 'Failed to connect to Neo4j'}
                return results

            agent = create_requirement_agent(neo4j)
            result_chain = agent.process_requirement(requirement_text, refine=True)
            results['chain'] = result_chain

            print("\nChain Result Summary:")
            print(f"- Refined requirement: {result_chain.get('refined_requirement', 'N/A')[:80]}...")
            print(f"- Query executed: {result_chain.get('cypher_query', 'N/A')[:80]}...")
            print(f"- Results found: {result_chain.get('result_count', 0)}")
            print(f"- Analysis length: {len(result_chain.get('analysis', ''))} characters")

        finally:
            neo4j.close()

    except Exception as e:
        print(f"Error in chain approach: {str(e)}")
        results['chain'] = {'error': str(e)}

    # ========================================================================
    # Test 2: LangGraph Agent Approach
    # ========================================================================
    print("\n" + "="*80)
    print("TEST 2: LANGGRAPH AGENT APPROACH")
    print("="*80)
    print("Iterative workflow: Plan -> Query -> Reason -> Decide -> Repeat or Report\n")

    try:
        from src.neo4j_utils import create_neo4j_connection
        from src.requirement_agent_graph import create_graph_agent

        neo4j = create_neo4j_connection()
        try:
            if not neo4j.connect():
                print("Failed to connect to Neo4j")
                results['agent'] = {'error': 'Failed to connect to Neo4j'}
                return results

            graph_agent = create_graph_agent(neo4j)
            max_iterations = int(os.getenv('AGENT_MAX_ITERATIONS', '5'))
            result_agent = graph_agent.process_requirement(requirement_text, max_iterations=max_iterations)
            results['agent'] = result_agent

            print("\nAgent Result Summary:")
            print(f"- Plan: {result_agent.get('plan', 'N/A')[:80]}...")
            print(f"- Iterations used: {result_agent.get('iterations', 0)}/{max_iterations}")
            print(f"- Queries executed: {len(result_agent.get('queries_executed', []))}")
            print(f"- Analysis length: {len(result_agent.get('analysis', ''))} characters")

        finally:
            neo4j.close()

    except Exception as e:
        print(f"Error in agent approach: {str(e)}")
        results['agent'] = {'error': str(e)}

    # ========================================================================
    # Comparison Summary
    # ========================================================================
    print("\n" + "="*80)
    print("COMPARISON SUMMARY")
    print("="*80)

    if 'error' not in results.get('chain', {}) and 'error' not in results.get('agent', {}):
        print("\nChain Approach:")
        print(f"  - Single query execution")
        print(f"  - Linear, predictable workflow")
        print(f"  - Results found: {results['chain'].get('result_count', 0)}")
        print(f"  - Analysis: {len(results['chain'].get('analysis', ''))} chars")

        print("\nAgent Approach:")
        print(f"  - Iterative reasoning with {results['agent'].get('iterations', 0)} iterations")
        print(f"  - Can explore multiple angles")
        print(f"  - Queries executed: {len(results['agent'].get('queries_executed', []))}")
        print(f"  - Analysis: {len(results['agent'].get('analysis', ''))} chars")

        print("\nKey Differences:")
        print("  1. Chain: Faster, simpler, one-shot query")
        print("  2. Agent: Slower, more thorough, can refine approach")
        print("  3. Chain: Best for straightforward requirements")
        print("  4. Agent: Best for complex, exploratory analysis")

    print("\n" + "="*80)
    print("Comparison complete!")
    print("="*80 + "\n")

    return results


def main():
    """Main entry point for comparison."""

    # Example requirements to test
    example_requirements = [
        "Which requirements are marked as GoBD-relevant?",
        "What business objects would be impacted by implementing SEPA payment processing?",
        "Show all requirements related to invoice generation and their dependencies",
    ]

    if len(sys.argv) > 1:
        # User provided a requirement
        requirement = " ".join(sys.argv[1:])
        compare_approaches(requirement)
    else:
        # Use first example
        print("\nNo requirement provided. Using example:")
        print(f"  '{example_requirements[0]}'")
        print("\nTo test with your own requirement:")
        print(f"  python {sys.argv[0]} 'Your requirement here'\n")

        compare_approaches(example_requirements[0])


if __name__ == "__main__":
    main()
