"""
Main Workflow Script
Orchestrates the requirement checking workflow against Neo4j database.
Uses the LangGraph iterative agent for requirement analysis.
"""

import os
import sys
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
from dotenv import load_dotenv

from src.core.neo4j_utils import create_neo4j_connection
from src.core.csv_processor import RequirementProcessor
from src.agents.graph_agent import create_graph_agent


class RequirementWorkflow:
    """
    Main workflow orchestrator for processing requirements against Neo4j database.
    Uses the LangGraph iterative agent for comprehensive requirement analysis.
    """

    def __init__(self, csv_path: str, output_dir: str = "output"):
        """
        Initialize the workflow.

        Args:
            csv_path: Path to CSV file containing requirements
            output_dir: Directory for output files
        """
        self.csv_path = csv_path
        self.output_dir = output_dir
        self.neo4j = None
        self.agent = None
        self.processor = None

        # Create output directory
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    def setup(self) -> bool:
        """
        Set up all components for the workflow.

        Returns:
            True if setup successful, False otherwise
        """
        print("\n" + "="*80)
        print("NEO4J DATABASE REQUIREMENT CHECKER")
        print("="*80 + "\n")

        # Step 1: Connect to Neo4j
        print("Step 1: Connecting to Neo4j database...")
        try:
            self.neo4j = create_neo4j_connection()
            if not self.neo4j.connect():
                print("✗ Failed to connect to Neo4j database")
                return False
        except ValueError as e:
            print(f"✗ Configuration error: {str(e)}")
            return False

        # Step 2: Load requirements from CSV
        print("\nStep 2: Loading requirements from CSV...")
        try:
            self.processor = RequirementProcessor(self.csv_path)
            requirements = self.processor.load_requirements()
            if not requirements:
                print("✗ No requirements found in CSV file")
                return False
        except (FileNotFoundError, ValueError) as e:
            print(f"✗ Error loading requirements: {str(e)}")
            return False

        # Step 3: Initialize agent
        print("\nStep 3: Initializing LangGraph agent...")
        try:
            self.agent = create_graph_agent(self.neo4j)
            print("✓ LangGraph iterative agent initialized successfully")
        except Exception as e:
            print(f"✗ Error initializing agent: {str(e)}")
            return False

        # Step 4: Display database schema
        print("\nStep 4: Database schema information:")
        schema = self.neo4j.get_database_schema()
        print(f"  - Node Labels: {len(schema['node_labels'])} ({', '.join(schema['node_labels'][:5])}...)")
        print(f"  - Relationship Types: {len(schema['relationship_types'])} ({', '.join(schema['relationship_types'][:5])}...)")
        print(f"  - Property Keys: {len(schema['property_keys'])} ({', '.join(schema['property_keys'][:5])}...)")

        print("\n✓ Setup complete!\n")
        return True

    def process_all_requirements(self) -> List[Dict[str, Any]]:
        """
        Process all requirements from the CSV file.

        Returns:
            List of processing results for all requirements
        """
        requirements = self.processor.requirements
        results = []
        max_iterations = int(os.getenv('AGENT_MAX_ITERATIONS', '5'))

        print(f"\n{'='*80}")
        print(f"Processing {len(requirements)} requirements...")
        print(f"{'='*80}\n")

        for idx, requirement in enumerate(requirements, 1):
            print(f"\n[{idx}/{len(requirements)}] Processing requirement...")

            # Get requirement text
            req_text = self.processor.format_requirement_with_metadata(requirement)

            # Process the requirement with the agent
            try:
                result = self.agent.process_requirement(req_text, max_iterations=max_iterations)
                result['requirement_id'] = idx
                result['metadata'] = self.processor.get_requirement_metadata(requirement)
                results.append(result)

                print(f"✓ Requirement {idx} processed successfully\n")

            except Exception as e:
                print(f"✗ Error processing requirement {idx}: {str(e)}\n")
                results.append({
                    'requirement_id': idx,
                    'original_requirement': req_text,
                    'error': str(e),
                    'metadata': self.processor.get_requirement_metadata(requirement)
                })

        return results

    def save_results(self, results: List[Dict[str, Any]], format: str = "json") -> str:
        """
        Save processing results to file.

        Args:
            results: List of processing results
            format: Output format ('json' or 'txt')

        Returns:
            Path to saved file
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if format == "json":
            output_file = os.path.join(self.output_dir, f"results_{timestamp}.json")
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, indent=2, default=str)

        elif format == "txt":
            output_file = os.path.join(self.output_dir, f"results_{timestamp}.txt")
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write("="*80 + "\n")
                f.write("NEO4J REQUIREMENT CHECK RESULTS\n")
                f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("="*80 + "\n\n")

                for result in results:
                    f.write(f"\nRequirement #{result.get('requirement_id', 'N/A')}:\n")
                    f.write("-"*80 + "\n")
                    f.write(f"Original: {result.get('original_requirement', 'N/A')}\n\n")

                    if 'error' in result:
                        f.write(f"ERROR: {result['error']}\n")
                    else:
                        f.write(f"Plan:\n{result.get('plan', 'N/A')}\n\n")
                        f.write(f"Iterations: {result.get('iterations', 0)}\n\n")
                        f.write(f"Analysis:\n{result.get('analysis', 'N/A')}\n")

                    f.write("\n" + "="*80 + "\n")

        print(f"\n✓ Results saved to: {output_file}")
        return output_file

    def cleanup(self):
        """Clean up resources."""
        if self.neo4j:
            self.neo4j.close()

    def run(self, save_format: str = "both") -> List[Dict[str, Any]]:
        """
        Run the complete workflow.

        Args:
            save_format: Output format ('json', 'txt', or 'both')

        Returns:
            List of processing results
        """
        try:
            # Setup
            if not self.setup():
                print("\n✗ Setup failed. Exiting.")
                return []

            # Process all requirements
            results = self.process_all_requirements()

            # Save results
            if save_format in ["json", "both"]:
                self.save_results(results, format="json")
            if save_format in ["txt", "both"]:
                self.save_results(results, format="txt")

            # Summary
            print(f"\n{'='*80}")
            print("WORKFLOW COMPLETE")
            print(f"{'='*80}")
            print(f"Total requirements processed: {len(results)}")
            print(f"Successful: {len([r for r in results if 'error' not in r])}")
            print(f"Failed: {len([r for r in results if 'error' in r])}")
            print(f"{'='*80}\n")

            return results

        finally:
            self.cleanup()


def main():
    """Main entry point for the workflow."""
    # Load environment variables
    load_dotenv()

    # Get configuration from environment
    csv_path = os.getenv('CSV_FILE_PATH')
    if not csv_path:
        print("Error: CSV_FILE_PATH not set in environment variables")
        sys.exit(1)

    print(f"\n{'='*80}")
    print("Running LangGraph Agent Workflow")
    print("Iterative: plan -> query -> reason -> repeat")
    print(f"{'='*80}\n")

    # Create and run workflow
    workflow = RequirementWorkflow(csv_path=csv_path)
    results = workflow.run(save_format="both")

    # Exit with appropriate code
    sys.exit(0 if results else 1)


if __name__ == "__main__":
    main()
