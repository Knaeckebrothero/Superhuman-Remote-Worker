#!/usr/bin/env python3
"""
Metamodel Compliance Validator CLI

Run standalone validation checks against the Neo4j database.
Usage: python validate_metamodel.py [--check A1|A2|...|all] [--json]
"""

import argparse
import json
import sys
from dotenv import load_dotenv

from src.utils import create_neo4j_connection, MetamodelValidator


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Validate Neo4j graph against FINIUS metamodel"
    )
    parser.add_argument(
        "--check",
        choices=["all", "structural", "relationships", "quality",
                 "A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3"],
        default="all",
        help="Which checks to run (default: all)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of human-readable format"
    )
    args = parser.parse_args()

    # Connect to database
    try:
        connection = create_neo4j_connection()
        if not connection.connect():
            print("Failed to connect to Neo4j database", file=sys.stderr)
            sys.exit(1)
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        validator = MetamodelValidator(connection)

        # Run appropriate checks
        if args.check == "all":
            report = validator.run_all_checks()
        elif args.check == "structural":
            report = validator.run_structural_checks()
        elif args.check == "relationships":
            report = validator.run_relationship_checks()
        elif args.check == "quality":
            report = validator.run_quality_gate_checks()
        else:
            # Specific check ID
            report = validator.run_specific_check(args.check)

        # Output results
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(report.format_summary())
            print()

            # Print detailed results
            print("Detailed Results:")
            print("-" * 40)
            for result in report.results:
                status = "✓ PASS" if result.passed else ("✗ FAIL" if result.severity.value == "error" else "⚠ WARN")
                print(f"[{result.check_id}] {result.check_name}: {status}")
                if not result.passed and result.violations:
                    for v in result.violations[:5]:
                        print(f"       {v}")
                    if len(result.violations) > 5:
                        print(f"       ... and {len(result.violations) - 5} more")

        # Exit with appropriate code
        sys.exit(0 if report.passed else 1)

    finally:
        connection.close()


if __name__ == "__main__":
    main()
