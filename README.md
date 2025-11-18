# Neo4j Requirements Compliance & Impact Analysis System

An intelligent workflow system for analyzing business requirements against a Neo4j graph database. This tool uses LangChain and Large Language Models (LLMs) to automatically generate Cypher queries, execute them against your Neo4j database, and provide comprehensive compliance analysis and impact assessments.

## Overview

This repository contains a **Graph-RAG (Retrieval-Augmented Generation) system** specifically designed for **requirement traceability and compliance checking** in a car rental business context. The system is particularly focused on:

- **GoBD Compliance**: German accounting principles (Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern)
- **Requirement Traceability**: Tracking relationships between requirements, business objects, and system messages
- **Impact Analysis**: Understanding how new requirements or changes affect existing system components
- **Compliance Verification**: Automated checking of requirement fulfillment

### Use Cases

1. **Compliance Auditing**: "Which billing requirements are GoBD-compliant?"
2. **Impact Assessment**: "Which business objects would be affected by implementing SEPA payment processing?"
3. **Requirement Analysis**: "Show all requirements related to invoice generation and their dependencies"
4. **Gap Analysis**: "Which requirements lack connections to business objects or messages?"

### How It Works

The workflow leverages LangChain and a metamodel-based approach:
1. Reads business requirements from a CSV file (semicolon-delimited format)
2. Uses an LLM to refine and understand the requirement context
3. Generates appropriate Cypher queries based on the database metamodel
4. Executes queries against the Neo4j graph database
5. Analyzes results using the LLM to provide structured compliance reports

## Table of Contents

- [Features](#features)
- [Database Metamodel](#database-metamodel)
- [Repository Structure](#repository-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [Database Setup](#database-setup)
- [Usage](#usage)
- [CSV Format](#csv-format)
- [Output](#output)
- [Examples](#examples)
- [License](#license)
- [Contact](#contact)

## Features

- **Metamodel-Based Analysis**: Built on a structured metamodel for requirement traceability (Requirement, BusinessObject, Message nodes)
- **GoBD Compliance Focus**: Specialized support for German accounting compliance requirements
- **Intelligent Query Generation**: Automatically generates Cypher queries from natural language requirements using LLM
- **Impact Assessment**: Analyzes how requirements affect business objects and system messages
- **Requirement Traceability**: Tracks dependencies, refinements, and relationships between requirements
- **Batch Processing**: Process multiple requirements from CSV files (supports both semicolon and comma delimiters)
- **Comprehensive Reporting**: Generates detailed analysis with compliance status, findings, and recommendations
- **Multiple Output Formats**: Results available in both JSON (structured) and TXT (human-readable) formats
- **Flexible LLM Support**: Works with OpenAI (default), Anthropic Claude, Cohere, and other LangChain-compatible providers
- **Error Handling**: Robust error handling with detailed logging

## Database Metamodel

The system uses a specialized graph metamodel designed for requirement traceability and impact analysis:

### Node Types

1. **Requirement** - Business/system requirements
   - Properties: `rid` (unique ID), `name`, `text`, `type`, `priority`, `status`, `source`, `valueStream`, `goBDRelevant`
   - Examples: Invoice generation, SEPA payment processing, audit trail requirements

2. **BusinessObject** - Business domain entities
   - Properties: `boid` (unique ID), `name`, `description`, `domain`, `owner`
   - Examples: Invoice, Payment, Reservation, Customer

3. **Message** - System messages/events
   - Properties: `mid` (unique ID), `name`, `description`, `direction`, `format`, `protocol`, `version`
   - Examples: ReservationRequest, PaymentConfirmation, InvoiceGenerated

### Relationship Types

- `(Requirement)-[:REFINES]->(Requirement)` - Requirement refinement hierarchy
- `(Requirement)-[:DEPENDS_ON]->(Requirement)` - Requirement dependencies
- `(Requirement)-[:TRACES_TO]->(Requirement)` - Traceability links
- `(Requirement)-[:RELATES_TO_OBJECT]->(BusinessObject)` - Requirement references a business object
- `(Requirement)-[:IMPACTS_OBJECT]->(BusinessObject)` - Requirement impacts a business object (structural/compliance)
- `(Requirement)-[:RELATES_TO_MESSAGE]->(Message)` - Requirement references a message
- `(Requirement)-[:IMPACTS_MESSAGE]->(Message)` - Requirement impacts message generation/handling
- `(Message)-[:USES_OBJECT]->(BusinessObject)` - Message uses object data
- `(Message)-[:PRODUCES_OBJECT]->(BusinessObject)` - Message produces/creates object instances

### Metamodel Files

- **`database/metamodell.cql`**: Neo4j schema definition with constraints, indexes, and sample queries
- **`database/neo4j.dump`**: Database dump file for importing the complete graph structure
- **`metamodell.xml`**: XML representation of the metamodel (if applicable)

## Repository Structure

```
├── src/
│   ├── __init__.py              # Package initialization
│   ├── main.py                  # Main workflow orchestrator
│   ├── neo4j_utils.py           # Neo4j connection and query utilities
│   ├── csv_processor.py         # CSV file processing (supports semicolon and comma delimiters)
│   └── requirement_agent.py     # LangChain agent with metamodel-aware query generation
├── data/
│   └── requirements.csv         # Car rental business requirements (82 requirements)
├── database/
│   ├── metamodell.cql           # Neo4j metamodel schema definition
│   └── neo4j.dump               # Database dump file for import
├── output/                      # Generated analysis results (created automatically)
├── docker/
│   └── README.txt               # Docker setup information
├── metamodell.xml               # XML representation of the metamodel
├── .env.example                 # Environment configuration template
├── requirements.txt             # Python dependencies
├── run_workflow.py              # Convenience script to run the workflow
├── README.md                    # This file
└── LICENSE.txt                  # License information
```

## Installation

### Prerequisites

- **Python 3.8 or higher**
- **Neo4j 5.x** (local installation or Neo4j Desktop/AuraDB)
  - Download from: https://neo4j.com/download/
  - Minimum 2GB RAM recommended for the sample dataset
- **OpenAI API key** (or other supported LLM provider like Anthropic Claude)
  - Get your API key from: https://platform.openai.com/api-keys

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

3. **Install Python dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your actual configuration (see Configuration section below)
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
  - Local: `bolt://localhost:7687`
  - Neo4j Desktop: Usually `bolt://localhost:7687` (check your database settings)
  - AuraDB: `neo4j+s://xxxxx.databases.neo4j.io`
- **NEO4J_USERNAME**: Database username (default: `neo4j`)
- **NEO4J_PASSWORD**: Database password (set during Neo4j installation)
- **CSV_FILE_PATH**: Path to your requirements CSV file (default: `data/requirements.csv`)
- **OPENAI_API_KEY**: Your OpenAI API key for LLM access
- **LLM_MODEL**: LLM model to use (options: `gpt-4-turbo-preview`, `gpt-4`, `gpt-3.5-turbo`)
- **LLM_TEMPERATURE**: Temperature setting for LLM (0.0 = deterministic, 1.0 = creative; default: 0.0)

### Alternative LLM Providers

To use Anthropic Claude or other providers, modify your `.env`:

```bash
# For Anthropic Claude
ANTHROPIC_API_KEY=your_anthropic_key_here
LLM_MODEL=claude-3-opus-20240229
```

## Database Setup

You have two options for setting up the Neo4j database:

### Option 1: Import from Database Dump (Recommended)

The repository includes a complete database dump with sample data:

1. **Start your Neo4j database**
   - Using Neo4j Desktop: Start your database instance
   - Using command line: `neo4j start`

2. **Import the database dump**
   ```bash
   # Stop the database first
   neo4j stop

   # Load the dump file (replace <database-name> with your database name, e.g., neo4j)
   neo4j-admin database load --from-path=database/ <database-name> --overwrite-destination=true

   # Start the database
   neo4j start
   ```

3. **Verify the import**
   - Open Neo4j Browser: http://localhost:7474
   - Run: `MATCH (n) RETURN count(n)` to verify data exists

### Option 2: Create Schema Manually

If you prefer to start with an empty database and create the schema:

1. **Start Neo4j and open Neo4j Browser** (http://localhost:7474)

2. **Execute the metamodel schema**
   - Open the file `database/metamodell.cql`
   - Copy and paste the content into Neo4j Browser
   - Execute the commands to create constraints and indexes

3. **Optional: Add sample data**
   - You can manually create nodes and relationships following the examples in `metamodell.cql`
   - Or import requirements from your CSV file

### Verify Database Connection

Test your database connection:

```bash
python -c "from src.neo4j_utils import create_neo4j_connection; conn = create_neo4j_connection(); conn.connect()"
```

You should see: `✓ Successfully connected to Neo4j at bolt://localhost:7687`

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

The system supports both semicolon-delimited and comma-delimited CSV formats.

### Semicolon-Delimited Format (Current)

The provided `data/requirements.csv` uses semicolon delimiters:

```csv
Name;Description
Fahrzeugkatalog durchsuchen;Das System muss es Nutzern ermöglichen, den Fahrzeugkatalog nach verschiedenen Kriterien zu durchsuchen.
Rechnungserzeugung;Bei Abschluss des Wertstromabschnitts „Mobilität beenden" MUSS das System automatisch eine Rechnung zum Mietvorgang erzeugen.
GoBD-Compliance;Das System MUSS eine revisionssichere Archivierung aller steuerlich relevanten Unterlagen gewährleisten.
```

- **First column**: Requirement name/title
- **Second column**: Requirement description (analyzed by the LLM)
- The CSV processor automatically detects and handles semicolon delimiters

### Comma-Delimited Format (Alternative)

You can also use traditional comma-delimited CSV files:

```csv
requirement,category,priority,compliance_standard
"Vehicle catalog search capability",functional,high,
"Automatic invoice generation on rental completion",billing,critical,GoBD
"Audit trail for all accounting transactions",compliance,critical,GoBD
```

### Supported Columns

- **requirement** or **Description**: The main requirement text (required)
- **name** or **Name**: Short name/title for the requirement (optional)
- **category**: Type of requirement (e.g., functional, compliance, billing)
- **priority**: Priority level (e.g., low, medium, high, critical)
- **compliance_standard**: Applicable standard (e.g., GoBD, ISO 9001)
- Any other metadata columns will be preserved and included in the output

### Current Dataset

The included `data/requirements.csv` contains **82 requirements** for a car rental system, including:
- Vehicle catalog and reservation features (Requirements 1-10)
- User management and authentication (Requirements 11-18)
- Payment processing (Requirements 19-23)
- Billing and invoicing (Requirements 24-61)
- GoBD compliance and audit trail (Requirements 62-81)

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

### Example 1: GoBD Compliance Check

**Requirement**: "Which requirements are marked as GoBD-relevant and which business objects do they impact?"

**Generated Query**:
```cypher
MATCH (r:Requirement {goBDRelevant: true})
OPTIONAL MATCH (r)-[:IMPACTS_OBJECT]->(bo:BusinessObject)
RETURN r.name, r.text, collect(DISTINCT bo.name) as impacted_objects
LIMIT 100
```

**Expected Analysis**:
- **Summary**: Found X requirements marked as GoBD-relevant
- **Findings**: Requirements like "Revisionssichere Archivierung", "Rechnungserzeugung", etc.
- **Impact**: Business objects such as Invoice (Rechnung), Payment (Zahlung), Booking (Buchung)
- **Compliance Status**: All GoBD requirements must have clear object mappings
- **Recommendations**: Ensure all identified objects implement required audit trails

### Example 2: Impact Analysis for Invoice Generation

**Requirement**: "What business objects and messages are affected by the invoice generation requirement?"

**Generated Query**:
```cypher
MATCH (r:Requirement)
WHERE r.name CONTAINS 'Rechnung' OR r.text CONTAINS 'Rechnung'
OPTIONAL MATCH (r)-[:IMPACTS_OBJECT]->(bo:BusinessObject)
OPTIONAL MATCH (r)-[:IMPACTS_MESSAGE]->(m:Message)
RETURN r.name, r.rid,
       collect(DISTINCT bo.name) as business_objects,
       collect(DISTINCT m.name) as messages
LIMIT 100
```

**Expected Analysis**:
- **Summary**: Invoice-related requirements affect multiple system components
- **Business Objects**: Rechnung (Invoice), Zahlung (Payment), Reservierung (Reservation)
- **Messages**: InvoiceGenerated, PaymentReceived, BookingCompleted
- **Impact Assessment**: Changes to invoicing will cascade to billing, payment processing, and reporting
- **Recommendations**: Coordinate changes across all affected domains

### Example 3: Requirement Dependency Analysis

**Requirement**: "Show all requirements that depend on payment processing capabilities"

**Generated Query**:
```cypher
MATCH (r1:Requirement)-[:DEPENDS_ON]->(r2:Requirement)
WHERE r2.name CONTAINS 'Zahlung' OR r2.text CONTAINS 'payment'
RETURN r1.name as dependent_requirement,
       r2.name as payment_requirement,
       r1.priority as priority
ORDER BY r1.priority DESC
LIMIT 100
```

**Expected Analysis**:
- **Summary**: Multiple requirements have dependencies on payment functionality
- **Findings**: Billing, refunds, and deposit handling all depend on core payment processing
- **Risk Level**: High - Changes to payment processing affect critical business workflows
- **Recommendations**: Implement payment changes with comprehensive testing across dependent requirements

### Example 4: Metamodel Quality Check

**Requirement**: "Find all requirements that have no relationship to business objects or messages"

**Generated Query**:
```cypher
MATCH (r:Requirement)
WHERE NOT (r)-[:RELATES_TO_OBJECT|IMPACTS_OBJECT]->(:BusinessObject)
  AND NOT (r)-[:RELATES_TO_MESSAGE|IMPACTS_MESSAGE]->(:Message)
RETURN r.rid, r.name, r.text
LIMIT 100
```

**Expected Analysis**:
- **Summary**: Identifies orphaned requirements lacking traceability
- **Compliance Status**: Not met - Requirements should link to implementation artifacts
- **Recommendations**: Review each requirement and establish appropriate object/message relationships

## License

This project is licensed under the terms of the Creative Commons Attribution 4.0 International License (CC BY 4.0) and the All Rights Reserved License. See the [LICENSE](LICENSE.txt) file for details.

## Contact

[Github](https://github.com/Knaeckebrothero)
[Mail](mailto:OverlyGenericAddress@pm.me)
