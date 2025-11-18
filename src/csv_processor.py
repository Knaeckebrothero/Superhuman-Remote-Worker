"""
CSV Requirements Processor
Handles reading and processing requirements from CSV files.
"""

import os
import pandas as pd
from typing import List, Dict, Any
from pathlib import Path


class RequirementProcessor:
    """
    Processes requirements from CSV files.
    Supports multiple columns for requirement metadata.
    """

    def __init__(self, csv_path: str):
        """
        Initialize the requirement processor.

        Args:
            csv_path: Path to the CSV file containing requirements
        """
        self.csv_path = csv_path
        self.requirements = []

    def load_requirements(self) -> List[Dict[str, Any]]:
        """
        Load requirements from CSV file.
        Supports both semicolon-delimited (Name;Description) and comma-delimited formats.

        Returns:
            List of requirement dictionaries

        Raises:
            FileNotFoundError: If CSV file doesn't exist
            ValueError: If CSV format is invalid
        """
        if not os.path.exists(self.csv_path):
            raise FileNotFoundError(f"CSV file not found: {self.csv_path}")

        try:
            # Try loading with semicolon delimiter first
            df = pd.read_csv(self.csv_path, sep=';', on_bad_lines='skip')

            # Check if we got valid columns
            if len(df.columns) >= 2 and df.columns[0].strip() and df.columns[1].strip():
                # Rename columns to standard format
                # First column is name, second is requirement text
                df.columns = ['name', 'requirement'] + list(df.columns[2:])
            else:
                # Fall back to comma delimiter
                df = pd.read_csv(self.csv_path)

                # Check for 'requirement' column
                if 'requirement' not in df.columns:
                    # If no 'requirement' column but has other text columns, try to use the first text column
                    text_columns = [col for col in df.columns if df[col].dtype == 'object']
                    if text_columns:
                        df = df.rename(columns={text_columns[0]: 'requirement'})
                    else:
                        raise ValueError(
                            "CSV must contain a 'requirement' column or text column. "
                            f"Found columns: {', '.join(df.columns)}"
                        )

            # Convert to list of dictionaries
            self.requirements = df.to_dict('records')

            # Remove any rows with empty requirements
            self.requirements = [
                req for req in self.requirements
                if pd.notna(req.get('requirement')) and str(req.get('requirement')).strip()
            ]

            print(f"✓ Loaded {len(self.requirements)} requirements from {self.csv_path}")
            return self.requirements

        except pd.errors.EmptyDataError:
            raise ValueError(f"CSV file is empty: {self.csv_path}")
        except Exception as e:
            raise ValueError(f"Error reading CSV file: {str(e)}")

    def get_requirement_text(self, requirement: Dict[str, Any]) -> str:
        """
        Extract the main requirement text from a requirement dictionary.

        Args:
            requirement: Requirement dictionary

        Returns:
            Requirement text as string
        """
        return str(requirement.get('requirement', '')).strip()

    def get_requirement_metadata(self, requirement: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract metadata from a requirement (all columns except 'requirement').

        Args:
            requirement: Requirement dictionary

        Returns:
            Dictionary of metadata
        """
        metadata = {k: v for k, v in requirement.items() if k != 'requirement'}
        return metadata

    def format_requirement_with_metadata(self, requirement: Dict[str, Any]) -> str:
        """
        Format requirement with its metadata for LLM processing.

        Args:
            requirement: Requirement dictionary

        Returns:
            Formatted string with requirement and metadata
        """
        text = self.get_requirement_text(requirement)
        metadata = self.get_requirement_metadata(requirement)

        if not metadata:
            return text

        # Format metadata as key-value pairs
        metadata_str = "\n".join([f"- {k}: {v}" for k, v in metadata.items() if pd.notna(v)])

        if metadata_str:
            return f"{text}\n\nAdditional context:\n{metadata_str}"
        return text


def load_requirements_from_env() -> List[Dict[str, Any]]:
    """
    Load requirements from CSV file specified in environment variables.

    Returns:
        List of requirement dictionaries

    Raises:
        ValueError: If CSV_FILE_PATH is not set or file is invalid
    """
    csv_path = os.getenv('CSV_FILE_PATH')

    if not csv_path:
        raise ValueError(
            "CSV_FILE_PATH environment variable not set. "
            "Please specify the path to your requirements CSV file."
        )

    processor = RequirementProcessor(csv_path)
    return processor.load_requirements()
