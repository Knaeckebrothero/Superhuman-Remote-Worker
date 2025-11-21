#!/usr/bin/env python3
"""
Simple test of the LangGraph agent approach
"""

import os
import json
from datetime import datetime
from dotenv import load_dotenv
from src.neo4j_utils import create_neo4j_connection
from src.requirement_agent_graph import create_graph_agent

# Load environment
load_dotenv()

def test_agent_simple():
    """Test agent approach with detailed output."""
    print("\n" + "="*60)
    print("TESTING LANGGRAPH AGENT - SIMPLE TEST")
    print("="*60)

    # Connect to Neo4j
    print("\nStep 1: Connecting to Neo4j...")
    neo4j = create_neo4j_connection()
    if not neo4j.connect():
        print("❌ Failed to connect")
        return
    print("✅ Connected!")

    # Create agent
    print("\nStep 2: Creating graph agent...")
    try:
        agent = create_graph_agent(neo4j)
        print("✅ Agent created!")
    except Exception as e:
        print(f"❌ Failed to create agent: {e}")
        return

    # Test requirement
    requirement = "Which requirements are marked as GoBD-relevant?"
    print(f"\nStep 3: Processing requirement: '{requirement}'")
    print("-"*60)

    try:
        # Process with limited iterations to avoid recursion
        print("\nProcessing with max_iterations=3...")
        result = agent.process_requirement(requirement, max_iterations=3)

        print("\n✅ Agent completed successfully!")

        # Show results
        print("\n" + "="*60)
        print("RESULTS:")
        print("="*60)

        print(f"\nPlan created: {bool(result.get('plan'))}")
        if result.get('plan'):
            print(f"Plan preview: {result['plan'][:200]}...")

        print(f"\nIterations completed: {result.get('iterations', 0)}")
        print(f"Queries executed: {len(result.get('queries_executed', []))}")

        if result.get('queries_executed'):
            print("\nQueries:")
            for i, q in enumerate(result['queries_executed'], 1):
                print(f"  {i}. {q}")

        print(f"\nAnalysis generated: {bool(result.get('analysis'))}")
        if result.get('analysis'):
            print(f"Analysis preview: {result['analysis'][:300]}...")

        # Save results
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs("output/test_results", exist_ok=True)

        filepath = f"output/test_results/agent_test_{timestamp}.json"
        with open(filepath, 'w') as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\n✅ Results saved to: {filepath}")

    except Exception as e:
        print(f"\n❌ Error during processing: {e}")
        import traceback
        traceback.print_exc()

    # Cleanup
    neo4j.close()
    print("\n✨ Test complete!")

if __name__ == "__main__":
    test_agent_simple()