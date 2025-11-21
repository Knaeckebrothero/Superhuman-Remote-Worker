#!/usr/bin/env python3
"""
Simple test of the chain approach with step-by-step output
"""

import os
import json
from datetime import datetime
from dotenv import load_dotenv
from src.neo4j_utils import create_neo4j_connection
from src.requirement_agent import RequirementAgent

# Load environment
load_dotenv()

def test_chain_with_steps():
    """Test chain approach step by step."""
    print("\n" + "="*60)
    print("TESTING SIMPLE CHAIN APPROACH - STEP BY STEP")
    print("="*60)

    # Connect to Neo4j
    print("\nStep 1: Connecting to Neo4j...")
    neo4j = create_neo4j_connection()
    if not neo4j.connect():
        print("❌ Failed to connect")
        return
    print("✅ Connected!")

    # Get schema
    print("\nStep 2: Getting database schema...")
    schema = neo4j.get_database_schema()
    print(f"  Node labels: {schema.get('node_labels', [])[:5]}...")
    print(f"  Relationships: {schema.get('relationship_types', [])[:5]}...")
    print(f"  Properties: {schema.get('property_keys', [])[:5]}...")

    # Create agent
    print("\nStep 3: Creating agent...")
    agent = RequirementAgent(neo4j)
    print("✅ Agent ready!")

    # Test requirement
    requirement = "Which requirements are marked as GoBD-relevant?"
    print(f"\nStep 4: Processing requirement: '{requirement}'")
    print("-"*60)

    # Step 4a: Refine
    print("\n4a. REFINING requirement...")
    refined = agent.refine_requirement(requirement)
    print(f"    Original: {requirement}")
    print(f"    Refined:  {refined}")

    # Step 4b: Generate query
    print("\n4b. GENERATING Cypher query...")
    query = agent.generate_cypher_query(refined)
    print(f"    Query: {query}")

    # Step 4c: Execute query
    print("\n4c. EXECUTING query...")
    try:
        results = neo4j.execute_query(query)
        print(f"    ✅ Found {len(results)} results")
        if results:
            print(f"    First result: {results[0]}")
    except Exception as e:
        print(f"    ⚠️ Query warning/error: {e}")
        results = []

    # Step 4d: Analyze
    print("\n4d. ANALYZING results...")
    analysis = agent.analyze_results(refined, query, results)
    print(f"    Analysis preview (first 300 chars):")
    print(f"    {analysis[:300]}...")

    # Save everything
    print("\n" + "="*60)
    print("SAVING RESULTS")
    print("="*60)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("output/test_results", exist_ok=True)

    # Save detailed results
    full_result = {
        "timestamp": timestamp,
        "original_requirement": requirement,
        "refined_requirement": refined,
        "cypher_query": query,
        "result_count": len(results),
        "first_5_results": results[:5],
        "analysis": analysis
    }

    filepath = f"output/test_results/chain_test_{timestamp}.json"
    with open(filepath, 'w') as f:
        json.dump(full_result, f, indent=2, default=str)
    print(f"✅ Full results saved to: {filepath}")

    # Also save human-readable version
    filepath_txt = f"output/test_results/chain_test_{timestamp}.txt"
    with open(filepath_txt, 'w') as f:
        f.write("CHAIN APPROACH TEST RESULTS\n")
        f.write("="*60 + "\n\n")
        f.write(f"Timestamp: {timestamp}\n")
        f.write(f"Original: {requirement}\n")
        f.write(f"Refined: {refined}\n\n")
        f.write(f"Cypher Query:\n{query}\n\n")
        f.write(f"Results Found: {len(results)}\n\n")
        if results:
            f.write("First 5 Results:\n")
            for i, r in enumerate(results[:5]):
                f.write(f"  {i+1}. {r}\n")
        f.write(f"\nAnalysis:\n{analysis}\n")
    print(f"✅ Readable results saved to: {filepath_txt}")

    # Cleanup
    neo4j.close()
    print("\n✨ Test complete!")

if __name__ == "__main__":
    test_chain_with_steps()