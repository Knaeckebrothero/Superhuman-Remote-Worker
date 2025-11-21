#!/usr/bin/env python3
"""
Enhanced comparison script with verbose output and file saving
"""

import os
import sys
import json
from datetime import datetime
from dotenv import load_dotenv
from src.neo4j_utils import create_neo4j_connection
from src.requirement_agent import create_requirement_agent

# Load environment variables
load_dotenv()

def save_to_file(filename, content):
    """Save content to a file in the output directory."""
    os.makedirs("output/comparison", exist_ok=True)
    filepath = f"output/comparison/{filename}"
    with open(filepath, 'w') as f:
        f.write(content)
    print(f"  💾 Saved to: {filepath}")

def test_chain_approach(requirement_text: str):
    """Test the simple chain approach with detailed output."""
    print("\n" + "="*80)
    print("🔗 TEST 1: SIMPLE CHAIN APPROACH")
    print("="*80)
    print("Linear workflow: Refine -> Query -> Analyze")
    print("-"*80)

    results_log = []

    try:
        # Connect to Neo4j
        print("\n📡 Connecting to Neo4j...")
        neo4j = create_neo4j_connection()
        if not neo4j.connect():
            print("❌ Failed to connect to Neo4j")
            return None
        print("✅ Connected to Neo4j")

        # Create agent
        print("\n🤖 Creating requirement agent...")
        agent = create_requirement_agent(neo4j)
        print("✅ Agent created")

        # Process requirement
        print(f"\n📝 Original requirement: {requirement_text}")
        print("\n🔄 Processing requirement with chain approach...")
        print("-"*40)

        # Process with the agent (this will print intermediate steps)
        result = agent.process_requirement(requirement_text, refine=True)

        # Display results
        print("\n" + "="*40)
        print("📊 CHAIN APPROACH RESULTS:")
        print("="*40)

        print(f"\n1️⃣ REFINED REQUIREMENT:")
        print(f"   {result.get('refined_requirement', 'N/A')}")
        results_log.append(f"Refined: {result.get('refined_requirement', 'N/A')}")

        print(f"\n2️⃣ CYPHER QUERY GENERATED:")
        print(f"   {result.get('cypher_query', 'N/A')}")
        results_log.append(f"Query: {result.get('cypher_query', 'N/A')}")

        print(f"\n3️⃣ RESULTS COUNT: {result.get('result_count', 0)}")
        results_log.append(f"Results: {result.get('result_count', 0)} items")

        if result.get('results'):
            print(f"\n4️⃣ SAMPLE RESULTS (first 3):")
            for i, r in enumerate(result.get('results', [])[:3]):
                print(f"   [{i+1}] {r}")
                results_log.append(f"   Result {i+1}: {r}")

        print(f"\n5️⃣ ANALYSIS:")
        analysis = result.get('analysis', 'N/A')
        # Print first 500 chars of analysis
        print(f"   {analysis[:500]}..." if len(analysis) > 500 else f"   {analysis}")
        results_log.append(f"Analysis: {analysis}")

        # Save to files
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save full JSON result
        save_to_file(f"chain_result_{timestamp}.json", json.dumps(result, indent=2))

        # Save readable log
        save_to_file(f"chain_log_{timestamp}.txt", "\n".join(results_log))

        # Clean up
        neo4j.close()

        return result

    except Exception as e:
        print(f"\n❌ Error in chain approach: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def test_agent_approach(requirement_text: str):
    """Test the LangGraph agent approach with detailed output."""
    print("\n" + "="*80)
    print("🤖 TEST 2: LANGGRAPH AGENT APPROACH")
    print("="*80)
    print("Iterative reasoning with tools")
    print("-"*80)

    results_log = []

    try:
        # Import agent module
        from src.requirement_agent_graph import create_graph_agent

        # Connect to Neo4j
        print("\n📡 Connecting to Neo4j...")
        neo4j = create_neo4j_connection()
        if not neo4j.connect():
            print("❌ Failed to connect to Neo4j")
            return None
        print("✅ Connected to Neo4j")

        # Create agent
        print("\n🤖 Creating graph agent...")
        graph_agent = create_graph_agent(neo4j)
        print("✅ Graph agent created")

        # Process requirement
        print(f"\n📝 Original requirement: {requirement_text}")
        print("\n🔄 Processing requirement with agent approach...")
        print("   (Agent will iterate up to 5 times)")
        print("-"*40)

        # Process with the agent
        result = graph_agent.process_requirement(requirement_text, max_iterations=5)

        # Display results
        print("\n" + "="*40)
        print("📊 AGENT APPROACH RESULTS:")
        print("="*40)

        # Show iterations
        if 'queries_executed' in result:
            print(f"\n🔁 ITERATIONS: {len(result['queries_executed'])}")
            for i, query_info in enumerate(result['queries_executed']):
                print(f"\n  Iteration {i+1}:")
                print(f"    Query: {query_info.get('query', 'N/A')}")
                print(f"    Results: {query_info.get('result_count', 0)} items")
                results_log.append(f"Iteration {i+1}: {query_info.get('query', 'N/A')} -> {query_info.get('result_count', 0)} results")

        print(f"\n📋 PLAN DEVELOPED:")
        plan = result.get('plan', 'N/A')
        print(f"   {plan[:500]}..." if len(plan) > 500 else f"   {plan}")
        results_log.append(f"Plan: {plan}")

        print(f"\n📊 FINAL ANALYSIS:")
        analysis = result.get('analysis', 'N/A')
        print(f"   {analysis[:500]}..." if len(analysis) > 500 else f"   {analysis}")
        results_log.append(f"Analysis: {analysis}")

        # Save to files
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save full JSON result
        save_to_file(f"agent_result_{timestamp}.json", json.dumps(result, indent=2, default=str))

        # Save readable log
        save_to_file(f"agent_log_{timestamp}.txt", "\n".join(results_log))

        # Clean up
        neo4j.close()

        return result

    except ImportError:
        print("\n⚠️  LangGraph agent module not found")
        print("   Make sure requirement_agent_graph.py exists")
        return None
    except Exception as e:
        print(f"\n❌ Error in agent approach: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def main():
    """Main comparison function."""
    # Get requirement from command line or use default
    if len(sys.argv) > 1:
        requirement_text = " ".join(sys.argv[1:])
    else:
        requirement_text = "Which requirements are marked as GoBD-relevant?"

    print("\n" + "="*80)
    print("🔬 COMPARING LANGCHAIN APPROACHES")
    print("="*80)
    print(f"Requirement: {requirement_text}")

    # Test both approaches
    chain_result = test_chain_approach(requirement_text)
    agent_result = test_agent_approach(requirement_text)

    # Summary comparison
    print("\n" + "="*80)
    print("📊 COMPARISON SUMMARY")
    print("="*80)

    comparison = []

    if chain_result:
        print("\n✅ Chain Approach:")
        print(f"   - Query generated: {bool(chain_result.get('cypher_query'))}")
        print(f"   - Results found: {chain_result.get('result_count', 0)}")
        print(f"   - Analysis length: {len(chain_result.get('analysis', ''))}")
        comparison.append(f"Chain: {chain_result.get('result_count', 0)} results")
    else:
        print("\n❌ Chain Approach: Failed")
        comparison.append("Chain: Failed")

    if agent_result:
        print("\n✅ Agent Approach:")
        print(f"   - Iterations: {len(agent_result.get('queries_executed', []))}")
        print(f"   - Total queries: {len(agent_result.get('queries_executed', []))}")
        print(f"   - Analysis length: {len(agent_result.get('analysis', ''))}")
        comparison.append(f"Agent: {len(agent_result.get('queries_executed', []))} iterations")
    else:
        print("\n❌ Agent Approach: Failed or not available")
        comparison.append("Agent: Failed/Not available")

    # Save comparison summary
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_to_file(f"comparison_summary_{timestamp}.txt", "\n".join(comparison))

    print("\n✨ Comparison complete! Check output/comparison/ for detailed results.")

if __name__ == "__main__":
    main()