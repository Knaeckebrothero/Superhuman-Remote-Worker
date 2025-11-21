#!/usr/bin/env python3
"""
Unified Comparison Script for LangChain Approaches
Combines all improvements and features from various test scripts
"""

import os
import sys
import json
import argparse
from datetime import datetime
from typing import Dict, Any, Optional
import traceback
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import project modules
from src.neo4j_utils import create_neo4j_connection
from src.requirement_agent import RequirementAgent


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Compare LangChain chain vs agent approaches for requirement analysis"
    )
    parser.add_argument(
        "requirement",
        nargs="?",
        default="Which requirements are marked as GoBD-relevant?",
        help="Requirement text to analyze (default: GoBD-relevant requirements query)"
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=int(os.getenv('AGENT_MAX_ITERATIONS', '3')),
        help="Maximum iterations for agent approach (default: 3 or env var)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show database schema and additional debug information"
    )
    parser.add_argument(
        "--no-files",
        action="store_true",
        help="Skip saving results to files (console output only)"
    )
    return parser.parse_args()


def save_to_file(filepath: str, content: str, verbose: bool = True) -> bool:
    """
    Save content to a file with error handling.

    Args:
        filepath: Full path to the file
        content: Content to write
        verbose: Whether to print save confirmation

    Returns:
        True if successful, False otherwise
    """
    try:
        # Ensure directory exists
        directory = os.path.dirname(filepath)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        if verbose:
            print(f"  [Saved] {filepath}")
        return True
    except Exception as e:
        print(f"  [Error] Failed to save {filepath}: {e}")
        return False


def format_results_for_display(results: list, limit: int = 5) -> str:
    """Format query results for console display."""
    if not results:
        return "  No results found"

    formatted = []
    for i, record in enumerate(results[:limit], 1):
        formatted.append(f"  [{i}] {record}")

    if len(results) > limit:
        formatted.append(f"  ... and {len(results) - limit} more results")

    return "\n".join(formatted)


def format_analysis_preview(analysis: str, max_chars: int = 500) -> str:
    """Format analysis text for preview display."""
    if not analysis:
        return "  No analysis generated"

    if len(analysis) <= max_chars:
        return f"  {analysis}"

    return f"  {analysis[:max_chars]}..."


def test_chain_approach(requirement_text: str, debug: bool = False) -> Dict[str, Any]:
    """
    Test the simple chain approach with detailed step-by-step output.

    Args:
        requirement_text: The requirement to analyze
        debug: Whether to show debug information

    Returns:
        Dictionary containing results and metadata
    """
    print("\n" + "="*80)
    print("CHAIN APPROACH TEST")
    print("="*80)
    print("Method: Linear workflow (Refine -> Query -> Analyze)")
    print("-"*80)

    results = {
        'timestamp': datetime.now().strftime("%Y%m%d_%H%M%S"),
        'approach': 'chain',
        'original_requirement': requirement_text,
        'success': False
    }

    try:
        # Step 1: Connect to Neo4j
        print("\nStep 1: Connecting to Neo4j...")
        neo4j = create_neo4j_connection()
        if not neo4j.connect():
            print("  [Failed] Unable to connect to Neo4j")
            results['error'] = "Failed to connect to Neo4j"
            return results
        print("  [Success] Connected to Neo4j")

        # Optional: Show database schema
        if debug:
            print("\n  Debug: Database Schema")
            schema = neo4j.get_database_schema()
            print(f"    Node labels: {schema.get('node_labels', [])[:10]}")
            print(f"    Relationships: {schema.get('relationship_types', [])[:10]}")
            print(f"    Properties: {schema.get('property_keys', [])[:10]}")

        # Step 2: Create agent
        print("\nStep 2: Creating requirement agent...")
        agent = RequirementAgent(neo4j)
        print("  [Success] Agent created")

        # Step 3: Process requirement
        print(f"\nStep 3: Processing requirement")
        print(f'  Input: "{requirement_text}"')
        print("-"*40)

        # Step 3a: Refine requirement
        print("\n  3a. Refining requirement...")
        refined = agent.refine_requirement(requirement_text)
        print(f"    Original: {requirement_text[:100]}...")
        print(f"    Refined: {refined[:100]}...")
        results['refined_requirement'] = refined

        # Step 3b: Generate Cypher query
        print("\n  3b. Generating Cypher query...")
        query = agent.generate_cypher_query(refined)
        print(f"    Query preview: {query[:200]}...")
        results['cypher_query'] = query

        # Step 3c: Execute query
        print("\n  3c. Executing query...")
        try:
            query_results = neo4j.execute_query(query)
            print(f"    [Success] Found {len(query_results)} results")
            results['result_count'] = len(query_results)
            results['results'] = query_results

            # Show sample results
            if query_results:
                print("\n  Sample results:")
                print(format_results_for_display(query_results, 3))
        except Exception as e:
            print(f"    [Warning] Query execution issue: {str(e)[:100]}")
            query_results = []
            results['result_count'] = 0
            results['results'] = []
            results['query_error'] = str(e)

        # Step 3d: Analyze results
        print("\n  3d. Analyzing results...")
        analysis = agent.analyze_results(refined, query, query_results)
        print("    Analysis preview:")
        print(format_analysis_preview(analysis, 300))
        results['analysis'] = analysis

        # Cleanup
        neo4j.close()
        results['success'] = True
        print("\n[Chain approach completed successfully]")

    except Exception as e:
        results['error'] = str(e)
        results['traceback'] = traceback.format_exc()
        print(f"\n[Error] Chain approach failed: {e}")
        if debug:
            print(traceback.format_exc())

    return results


def test_agent_approach(requirement_text: str, max_iterations: int = 3, debug: bool = False) -> Dict[str, Any]:
    """
    Test the LangGraph agent approach with iteration control.

    Args:
        requirement_text: The requirement to analyze
        max_iterations: Maximum number of iterations for the agent
        debug: Whether to show debug information

    Returns:
        Dictionary containing results and metadata
    """
    print("\n" + "="*80)
    print("LANGGRAPH AGENT APPROACH TEST")
    print("="*80)
    print(f"Method: Iterative reasoning with tools (max {max_iterations} iterations)")
    print("-"*80)

    results = {
        'timestamp': datetime.now().strftime("%Y%m%d_%H%M%S"),
        'approach': 'agent',
        'original_requirement': requirement_text,
        'max_iterations': max_iterations,
        'success': False
    }

    try:
        # Import agent module
        from src.requirement_agent_graph import create_graph_agent

        # Step 1: Connect to Neo4j
        print("\nStep 1: Connecting to Neo4j...")
        neo4j = create_neo4j_connection()
        if not neo4j.connect():
            print("  [Failed] Unable to connect to Neo4j")
            results['error'] = "Failed to connect to Neo4j"
            return results
        print("  [Success] Connected to Neo4j")

        # Optional: Show database schema
        if debug:
            print("\n  Debug: Database Schema")
            schema = neo4j.get_database_schema()
            print(f"    Node labels: {schema.get('node_labels', [])[:10]}")
            print(f"    Relationships: {schema.get('relationship_types', [])[:10]}")

        # Step 2: Create graph agent
        print("\nStep 2: Creating graph agent...")
        graph_agent = create_graph_agent(neo4j)
        print("  [Success] Graph agent created")

        # Step 3: Process requirement
        print(f"\nStep 3: Processing requirement with agent")
        print(f'  Input: "{requirement_text}"')
        print(f"  Max iterations: {max_iterations}")
        print("-"*40)

        # Process with the agent
        print("\n  Starting agent workflow...")
        result = graph_agent.process_requirement(requirement_text, max_iterations=max_iterations)

        # Extract and display results
        print("\n  Agent Results:")

        # Show plan
        if result.get('plan'):
            print(f"\n  Plan created: Yes")
            print(f"    Preview: {result['plan'][:200]}...")
            results['plan'] = result['plan']
        else:
            print(f"\n  Plan created: No")

        # Show iterations and queries
        iterations = result.get('iterations', 0)
        queries = result.get('queries_executed', [])
        print(f"\n  Iterations completed: {iterations}")
        print(f"  Queries executed: {len(queries)}")
        results['iterations'] = iterations
        results['queries_executed'] = queries

        if queries:
            print("\n  Query details:")
            for i, q in enumerate(queries[:3], 1):
                print(f"    [{i}] Iteration {q.get('iteration', '?')}: {q.get('query', 'N/A')[:100]}...")

        # Show analysis
        if result.get('analysis'):
            print(f"\n  Analysis generated: Yes")
            print("    Preview:")
            print(format_analysis_preview(result['analysis'], 300))
            results['analysis'] = result['analysis']
        else:
            print(f"\n  Analysis generated: No")

        # Cleanup
        neo4j.close()
        results['success'] = True
        print("\n[Agent approach completed successfully]")

    except ImportError:
        results['error'] = "LangGraph agent module not found"
        print("\n[Error] LangGraph agent module not found")
        print("  Make sure requirement_agent_graph.py exists in src/")
    except Exception as e:
        results['error'] = str(e)
        results['traceback'] = traceback.format_exc()
        print(f"\n[Error] Agent approach failed: {e}")
        if debug:
            print(traceback.format_exc())

    return results


def save_results(results: Dict[str, Any], approach: str, save_files: bool = True) -> None:
    """
    Save results to JSON and TXT files.

    Args:
        results: Results dictionary to save
        approach: 'chain' or 'agent'
        save_files: Whether to actually save files
    """
    if not save_files:
        return

    timestamp = results.get('timestamp', datetime.now().strftime("%Y%m%d_%H%M%S"))
    base_path = "output/comparison"

    # Save JSON
    json_path = f"{base_path}/unified_{approach}_{timestamp}.json"
    if save_to_file(json_path, json.dumps(results, indent=2, default=str)):
        print(f"  [JSON] Saved to {json_path}")

    # Create human-readable TXT version
    txt_content = []
    txt_content.append(f"UNIFIED COMPARISON - {approach.upper()} APPROACH")
    txt_content.append("="*60)
    txt_content.append(f"Timestamp: {timestamp}")
    txt_content.append(f"Requirement: {results.get('original_requirement', 'N/A')}")
    txt_content.append("")

    if approach == 'chain':
        txt_content.append(f"Refined: {results.get('refined_requirement', 'N/A')[:200]}...")
        txt_content.append("")
        txt_content.append(f"Cypher Query:\n{results.get('cypher_query', 'N/A')}")
        txt_content.append("")
        txt_content.append(f"Results Count: {results.get('result_count', 0)}")
        if results.get('results'):
            txt_content.append("\nFirst 5 Results:")
            for i, r in enumerate(results.get('results', [])[:5], 1):
                txt_content.append(f"  {i}. {r}")
    else:  # agent
        txt_content.append(f"Max Iterations: {results.get('max_iterations', 'N/A')}")
        txt_content.append(f"Iterations Completed: {results.get('iterations', 0)}")
        txt_content.append(f"Queries Executed: {len(results.get('queries_executed', []))}")
        txt_content.append("")
        if results.get('plan'):
            txt_content.append(f"Plan:\n{results.get('plan', 'N/A')[:500]}...")
        txt_content.append("")
        if results.get('queries_executed'):
            txt_content.append("Queries:")
            for q in results.get('queries_executed', []):
                txt_content.append(f"  - Iteration {q.get('iteration', '?')}: {q.get('query', 'N/A')[:100]}")

    txt_content.append("")
    txt_content.append(f"Analysis:\n{results.get('analysis', 'N/A')}")

    if results.get('error'):
        txt_content.append("")
        txt_content.append(f"Error: {results.get('error')}")
        if results.get('traceback'):
            txt_content.append("\nTraceback:")
            txt_content.append(results.get('traceback'))

    # Save TXT
    txt_path = f"{base_path}/unified_{approach}_{timestamp}.txt"
    if save_to_file(txt_path, "\n".join(txt_content)):
        print(f"  [TXT] Saved to {txt_path}")


def compare_and_summarize(chain_result: Dict, agent_result: Dict, save_files: bool = True) -> None:
    """
    Create a comparison summary of both approaches.

    Args:
        chain_result: Results from chain approach
        agent_result: Results from agent approach
        save_files: Whether to save summary to file
    """
    print("\n" + "="*80)
    print("COMPARISON SUMMARY")
    print("="*80)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary = []

    # Header
    summary.append("UNIFIED COMPARISON SUMMARY")
    summary.append("="*60)
    summary.append(f"Generated: {timestamp}")
    summary.append(f"Requirement: {chain_result.get('original_requirement', 'N/A')}")
    summary.append("")

    # Chain Approach Summary
    summary.append("CHAIN APPROACH:")
    summary.append("-"*40)
    if chain_result.get('success'):
        summary.append(f"  Status: Success")
        summary.append(f"  Query generated: Yes")
        summary.append(f"  Results found: {chain_result.get('result_count', 0)}")
        summary.append(f"  Analysis length: {len(chain_result.get('analysis', ''))} chars")
    else:
        summary.append(f"  Status: Failed")
        summary.append(f"  Error: {chain_result.get('error', 'Unknown')}")
    summary.append("")

    # Agent Approach Summary
    summary.append("AGENT APPROACH:")
    summary.append("-"*40)
    if agent_result.get('success'):
        summary.append(f"  Status: Success")
        summary.append(f"  Iterations: {agent_result.get('iterations', 0)}/{agent_result.get('max_iterations', 'N/A')}")
        summary.append(f"  Queries executed: {len(agent_result.get('queries_executed', []))}")
        summary.append(f"  Plan created: {'Yes' if agent_result.get('plan') else 'No'}")
        summary.append(f"  Analysis length: {len(agent_result.get('analysis', ''))} chars")
    else:
        summary.append(f"  Status: Failed or Not Available")
        summary.append(f"  Error: {agent_result.get('error', 'Unknown')}")
    summary.append("")

    # Key Differences
    summary.append("KEY DIFFERENCES:")
    summary.append("-"*40)

    if chain_result.get('success') and agent_result.get('success'):
        # Compare query counts
        chain_queries = 1 if chain_result.get('cypher_query') else 0
        agent_queries = len(agent_result.get('queries_executed', []))
        summary.append(f"  Queries: Chain={chain_queries}, Agent={agent_queries}")

        # Compare result counts
        chain_results = chain_result.get('result_count', 0)
        summary.append(f"  Results found: Chain={chain_results}")

        # Compare analysis lengths
        chain_analysis_len = len(chain_result.get('analysis', ''))
        agent_analysis_len = len(agent_result.get('analysis', ''))
        summary.append(f"  Analysis detail: Chain={chain_analysis_len} chars, Agent={agent_analysis_len} chars")

        # Execution approach
        summary.append(f"  Execution: Chain=Linear, Agent=Iterative")
    else:
        summary.append("  Cannot compare - one or both approaches failed")

    summary.append("")
    summary.append("FILES SAVED:")
    summary.append("-"*40)
    summary.append(f"  Chain JSON: output/comparison/unified_chain_{chain_result.get('timestamp')}.json")
    summary.append(f"  Chain TXT: output/comparison/unified_chain_{chain_result.get('timestamp')}.txt")
    summary.append(f"  Agent JSON: output/comparison/unified_agent_{agent_result.get('timestamp')}.json")
    summary.append(f"  Agent TXT: output/comparison/unified_agent_{agent_result.get('timestamp')}.txt")
    summary.append(f"  This summary: output/comparison/unified_summary_{timestamp}.txt")

    # Print to console
    print("\n".join(summary))

    # Save summary to file
    if save_files:
        summary_path = f"output/comparison/unified_summary_{timestamp}.txt"
        save_to_file(summary_path, "\n".join(summary), verbose=False)
        print(f"\n[Summary saved to {summary_path}]")


def main():
    """Main execution function."""
    # Parse arguments
    args = parse_arguments()

    print("\n" + "="*80)
    print("UNIFIED LANGCHAIN APPROACHES COMPARISON")
    print("="*80)
    print(f"Requirement: {args.requirement}")
    print(f"Max iterations: {args.max_iterations}")
    print(f"Debug mode: {args.debug}")
    print(f"Save files: {not args.no_files}")

    # Test both approaches
    chain_result = test_chain_approach(args.requirement, debug=args.debug)
    agent_result = test_agent_approach(
        args.requirement,
        max_iterations=args.max_iterations,
        debug=args.debug
    )

    # Save results
    if not args.no_files:
        print("\n" + "-"*80)
        print("SAVING RESULTS")
        print("-"*80)
        save_results(chain_result, 'chain', save_files=True)
        save_results(agent_result, 'agent', save_files=True)

    # Generate comparison summary
    compare_and_summarize(chain_result, agent_result, save_files=not args.no_files)

    print("\n" + "="*80)
    print("COMPARISON COMPLETE")
    print("="*80)

    if not args.no_files:
        print("\nAll results saved to: output/comparison/")
        print("Use --no-files flag to skip file saving in future runs")

    print("\n[Done]")


if __name__ == "__main__":
    main()