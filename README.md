# Neo4j Database Requirement Checker

An intelligent workflow system that checks a Neo4j graph database against requirements specified in CSV files. This tool uses LangChain and Large Language Models (LLMs) to automatically generate Cypher queries, execute them against your Neo4j database, and provide comprehensive analysis of compliance and impact.

## Overview

This project provides an automated solution for:
- **Compliance Checking**: Verify that your data meets regulatory and business requirements
- **Impact Analysis**: Understand how changes or decisions affect different areas of your system
- **Database Auditing**: Automatically query and analyze your Neo4j database based on natural language requirements

The workflow leverages LangChain to create an intelligent agent that:
1. Reads requirements from a CSV file
2. Refines and clarifies each requirement
3. Generates appropriate Cypher queries based on your database schema
4. Executes queries against your Neo4j database
5. Analyzes results and provides detailed reports

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [CSV Format](#csv-format)
- [Output](#output)
- [Examples](#examples)
- [License](#license)
- [Contact](#contact)

## Features

- **Intelligent Query Generation**: Automatically generates Cypher queries from natural language requirements
- **Schema-Aware**: Analyzes your Neo4j database schema to create accurate queries
- **Batch Processing**: Process multiple requirements from a single CSV file
- **Detailed Analysis**: Provides comprehensive analysis with supporting evidence from data
- **Multiple Output Formats**: Results available in both JSON and human-readable text formats
- **Metadata Support**: Include additional context like priority, category, and compliance standards
- **Error Handling**: Robust error handling with detailed logging
- **Extensible**: Easy to customize for different LLM providers and query strategies

## Architecture

```
├── src/
│   ├── __init__.py              # Package initialization
│   ├── main.py                  # Main workflow orchestrator
│   ├── neo4j_utils.py           # Neo4j connection and query utilities
│   ├── csv_processor.py         # CSV file processing
│   └── requirement_agent.py     # LangChain agent for requirement processing
├── data/
│   └── requirements.csv         # Example requirements file
├── output/                      # Generated results (created automatically)
├── tests/                       # Test files
├── .env.example                 # Example environment configuration
├── requirements.txt             # Python dependencies
└── run_workflow.py              # Convenience script to run the workflow
```

## Installation

### Prerequisites

- Python 3.8 or higher
- Neo4j database (local or remote)
- OpenAI API key (or other supported LLM provider)

### Setup Steps

1. **Clone the repository**
   ```bash
   git clone https://github.com/Knaeckebrothero/Uni-Projekt-Graph-RAG.git
   cd Uni-Projekt-Graph-RAG
   ```

2. **Create a virtual environment** (recommended)
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your actual configuration
   ```

## Configuration

Create a `.env` file in the project root with the following variables:

```bash
# Neo4j Database Configuration
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your_password_here

# CSV Requirements File
CSV_FILE_PATH=data/requirements.csv

# LLM Configuration (OpenAI)
OPENAI_API_KEY=your_openai_api_key_here
LLM_MODEL=gpt-4-turbo-preview
LLM_TEMPERATURE=0.0
```

### Configuration Options

- **NEO4J_URI**: Connection string for your Neo4j database
- **NEO4J_USERNAME**: Database username (default: neo4j)
- **NEO4J_PASSWORD**: Database password
- **CSV_FILE_PATH**: Path to your requirements CSV file
- **OPENAI_API_KEY**: Your OpenAI API key
- **LLM_MODEL**: LLM model to use (default: gpt-4-turbo-preview)
- **LLM_TEMPERATURE**: Temperature setting for LLM (0.0-1.0, default: 0.0 for deterministic output)

## Usage

### Basic Usage

Run the workflow with the default configuration:

```bash
python run_workflow.py
```

Or use the main module directly:

```bash
python -m src.main
```

### Programmatic Usage

You can also use the workflow programmatically in your Python code:

```python
from dotenv import load_dotenv
from src.main import RequirementWorkflow

# Load environment variables
load_dotenv()

# Create workflow instance
workflow = RequirementWorkflow(
    csv_path="data/requirements.csv",
    output_dir="output"
)

# Run the workflow
results = workflow.run(
    refine=True,          # Refine requirements before processing
    save_format="both"    # Save as both JSON and TXT
)

# Access individual results
for result in results:
    print(f"Requirement: {result['original_requirement']}")
    print(f"Analysis: {result['analysis']}")
```

## CSV Format

Your requirements CSV file should have the following structure:

### Required Column
- **requirement**: The requirement text (natural language)

### Optional Columns
- **category**: Type of requirement (e.g., compliance, impact_analysis, planning)
- **priority**: Priority level (e.g., low, medium, high, critical)
- **compliance_standard**: Applicable compliance standard (e.g., RoHS, ISO 9001, FDA)
- Any other metadata columns you want to include

### Example CSV

```csv
requirement,category,priority,compliance_standard
"All products in the Electronics category must have a compliance certification for RoHS",compliance,high,RoHS
"Identify all suppliers that provide materials for products sold in the EU market",impact_analysis,medium,GDPR
"What departments would be affected by implementing a digital twin for our manufacturing process?",impact_analysis,high,
```

See `data/requirements.csv` for more examples.

## Output

The workflow generates two types of output files in the `output/` directory:

### JSON Output (`results_YYYYMMDD_HHMMSS.json`)

Structured data including:
- Original and refined requirements
- Generated Cypher queries
- Query results
- Analysis text
- Metadata

### Text Output (`results_YYYYMMDD_HHMMSS.txt`)

Human-readable report with:
- Formatted requirements
- Executed queries
- Analysis and recommendations
- Summary statistics

### Example Output Structure

```json
{
  "requirement_id": 1,
  "original_requirement": "All products in category X must comply with Y",
  "refined_requirement": "Verify that all products with category='X' have compliance_field='Y'",
  "cypher_query": "MATCH (p:Product) WHERE p.category='X' RETURN p",
  "result_count": 15,
  "results": [...],
  "analysis": "Based on the query results, 15 products were found...",
  "metadata": {
    "category": "compliance",
    "priority": "high"
  }
}
```

## Examples

### Example 1: Compliance Check

**Requirement**: "All products in the Electronics category must have RoHS certification"

**Generated Query**:
```cypher
MATCH (p:Product)
WHERE p.category = 'Electronics'
RETURN p.name, p.rohs_certified, p.id
```

**Analysis**: The system will check all electronics products and report which ones are missing RoHS certification.

### Example 2: Impact Analysis

**Requirement**: "What departments would be affected by implementing a digital twin for manufacturing?"

**Generated Query**:
```cypher
MATCH (d:Department)-[:INVOLVED_IN]->(p:Process)
WHERE p.type = 'manufacturing'
RETURN DISTINCT d.name, d.id, COUNT(p) as process_count
```

**Analysis**: The system identifies all departments connected to manufacturing processes.

## Project Structure

- **src/main.py**: Main workflow orchestration
- **src/neo4j_utils.py**: Neo4j database connection and operations (line 17-155)
- **src/csv_processor.py**: CSV file processing and requirement extraction (line 11-133)
- **src/requirement_agent.py**: LangChain agent implementation (line 18-253)

## License

This project is licensed under the terms of the Creative Commons Attribution 4.0 International License (CC BY 4.0) and the All Rights Reserved License. See the [LICENSE](LICENSE.txt) file for details.

## Contact

[Github](https://github.com/Knaeckebrothero)
[Mail](mailto:OverlyGenericAddress@pm.me)
