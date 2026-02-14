# Graph-RAG Requirements Compliance System

Dieses System automatisiert die Extraktion und Validierung regulatorischer Anforderungen mithilfe eines Graph-RAG (Retrieval-Augmented Generation) Ansatzes. Es verarbeitet Compliance-Dokumente (z.B. GoBD, DSGVO), extrahiert einzelne Anforderungen mittels LLM-basierter Agents und integriert diese in einen Neo4j Knowledge Graph zur Nachverfolgbarkeit und Gap-Analyse.

Die Architektur folgt einem **Universal Agent** Pattern: eine einzige LangGraph-basierte Agent Codebase, die entweder als **Creator** (Dokumentenverarbeitung und Anforderungsextraktion) oder als **Validator** (Anforderungsvalidierung und Graph-Integration) deployed werden kann, gesteuert ausschließlich durch Konfiguration. Die Agents koordinieren sich über eine gemeinsame PostgreSQL-Datenbank, mit MongoDB für optionales Audit Logging und einem Cockpit Debug Dashboard zur Echtzeit-Visualisierung der Ausführung.

## Inhaltsverzeichnis

- [Voraussetzungen](#voraussetzungen)
- [Produktiv-Deployment](#produktiv-deployment)
- [Entwicklungsumgebung](#entwicklungsumgebung)
- [Architektur](#architektur)
- [Cockpit (Debug Dashboard)](#cockpit-debug-dashboard)
- [Debugging](#debugging)
- [Lizenz](#lizenz)

## Voraussetzungen

- **Docker oder Podman** mit Compose-Unterstützung
- **Git**
- **Python 3.11+** (nur für Entwicklung)

## Produktiv-Deployment

Deployment des Gesamtsystems mit vorgefertigten Container Images.

### 1. Repository klonen und konfigurieren

```bash
git clone https://github.com/Knaeckebrothero/Uni-Projekt-Graph-RAG.git
cd Uni-Projekt-Graph-RAG
cp .env.example .env
```

### 2. Umgebungsvariablen bearbeiten

`.env` mit der gewünschten Konfiguration bearbeiten:

**Erforderlich:**
- `OPENAI_API_KEY` - OpenAI API Key oder kompatibler API Key
- `LLM_BASE_URL` - Custom Endpoint URL (bei Verwendung selbst gehosteter Modelle; siehe `docker/` für vLLM-, SGLang- und llama.cpp-Konfigurationen)

**Optional:**
- `TAVILY_API_KEY` - Für Web Search Funktionalität im Creator Agent
- `MONGODB_URL` - MongoDB Connection String (erforderlich für Cockpit Debug Dashboard und LLM Conversation Viewer)
- `NEO4J_PASSWORD`, `POSTGRES_PASSWORD` - Eigene Datenbankpasswörter
- `LOG_LEVEL` - DEBUG, INFO, WARNING, ERROR (Standard: INFO)
- Port-Overrides: `CREATOR_PORT`, `VALIDATOR_PORT`, `DASHBOARD_PORT`, usw.

### 3. Alle Services starten

```bash
podman-compose up -d
```

Folgende Services werden gestartet:
- **PostgreSQL** - Job Tracking und Anforderungs-Cache
- **Neo4j** - Knowledge Graph Speicher
- **Creator Agent** - Dokumentenverarbeitung und Anforderungsextraktion
- **Validator Agent** - Anforderungsvalidierung und Graph-Integration
- **Dashboard** - Streamlit UI zur Job-Verwaltung

> **Optional:** `MONGODB_URL` in `.env` setzen, um LLM Request Logging und Agent Audit Trails zu aktivieren. MongoDB wird für die meisten [Cockpit](#cockpit-debug-dashboard) Debug Features und den CLI Conversation Viewer benötigt.

### 4. Services aufrufen

| Service | URL |
|---------|-----|
| Dashboard | http://localhost:8501 |
| Cockpit | http://localhost:4000 |
| Neo4j Browser | http://localhost:7474 |
| Creator API | http://localhost:8001 |
| Validator API | http://localhost:8002 |

### 5. Häufige Operationen

```bash
# Logs anzeigen
podman-compose logs -f
podman-compose logs -f creator validator

# Service-Status prüfen
podman-compose ps

# Services neu starten
podman-compose restart

# Alle Services stoppen
podman-compose down

# Stoppen und alle Daten entfernen
podman-compose down -v
```

## Entwicklungsumgebung

Datenbanken in Containern betreiben, während Agents lokal mit Python entwickelt werden.

### 1. Repository klonen und Python-Umgebung einrichten

```bash
git clone https://github.com/Knaeckebrothero/Uni-Projekt-Graph-RAG.git
cd Uni-Projekt-Graph-RAG

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt
pip install -e ./citation_tool[full]  # Citation Management für AI Agents
```

### 2. Umgebung konfigurieren

```bash
cp .env.example .env
# .env mit API Credentials bearbeiten
```

### 3. Datenbanken starten

```bash
podman-compose -f docker-compose.dev.yaml up -d
```

Folgende Services werden gestartet:
- **PostgreSQL**, **Neo4j**, **MongoDB** — Datenbanken
- **Cockpit** — Debug Dashboard (API + Frontend + MCP)

Nur Datenbanken starten (ohne Cockpit):

```bash
podman-compose -f docker-compose.dev.yaml up -d postgres neo4j mongodb
```

### 4. Datenbanken initialisieren

```bash
# Ersteinrichtung mit Beispieldaten
python scripts/app_init.py --seed

# Alles zurücksetzen (löscht alle Daten)
python scripts/app_init.py --force-reset --seed
```

### 5. Agents lokal ausführen

```bash
# Dokument verarbeiten
python agent.py --config creator --document-path ./data/doc.pdf --prompt "Extract requirements"

# Creator als API Server starten
python agent.py --config creator --port 8001

# Validator als API Server starten
python agent.py --config validator --port 8002

# Mit Streaming Output ausführen
python agent.py --config creator --document-path ./data/doc.pdf --prompt "Extract requirements" --stream --verbose

# Vollständigen Test durchführen
python agent.py --config creator --document-dir ./data/example_data/ --prompt "Identify possible requirements for a medium sized car rental company based on the provided GoBD document." --stream --verbose
```

### 6. Crash Recovery & Checkpointing

Der Agent erstellt während der Ausführung automatisch Checkpoints, die ein Fortsetzen nach Abstürzen ermöglichen:

```bash
# Job mit expliziter Job ID starten (für späteres Fortsetzen)
python agent.py --config creator --job-id my-job-123 --document-path ./data/doc.pdf --prompt "Extract requirements"

# Nach einem Absturz vom letzten Checkpoint fortsetzen
python agent.py --config creator --job-id my-job-123 --resume

# Mit Streaming Output fortsetzen
python agent.py --config creator --job-id my-job-123 --resume --stream --verbose
```

**Funktionsweise:**
- Checkpoints: `workspace/checkpoints/job_<id>.db` (SQLite)
- Logs: `workspace/logs/job_<id>.log`
- LangGraph speichert den State nach jeder Graph Node Ausführung
- Fortsetzen mit derselben `--job-id` setzt am letzten Checkpoint fort
- Checkpoints und Logs werden nach Abschluss zum Debugging aufbewahrt

**Bereinigung:**
```bash
# Alle Checkpoint-Dateien entfernen
rm workspace/checkpoints/job_*.db

# Alle Log-Dateien entfernen
rm workspace/logs/job_*.log

# Dateien für einen bestimmten Job entfernen
rm workspace/checkpoints/job_my-job-123.db
rm workspace/logs/job_my-job-123.log
```

### 7. Datenbankverwaltung

```bash
# Nur PostgreSQL zurücksetzen
python scripts/app_init.py --only-postgres --force-reset

# Neo4j mit Seed-Daten zurücksetzen
python scripts/app_init.py --only-neo4j --force-reset --seed

# Optionen des Init-Scripts anzeigen
python scripts/app_init.py --help

# Backup erstellen (wird im backups/ Verzeichnis gespeichert)
python scripts/app_init.py --create-backup                  # Automatisch benannt: backups/YYYYMMDD_NNN/
python scripts/app_init.py --create-backup my_backup        # Benannt: backups/YYYYMMDD_NNN_my_backup/

# Aus Backup wiederherstellen
python scripts/app_init.py --restore-backup backups/20260117_001_my_backup

# Neo4j Graph gegen FINIUS Metamodell validieren
python validate_metamodel.py --check all --json

# Letzte Option: Alle Docker Volumes entfernen und neu initialisieren
podman-compose -f docker-compose.dev.yaml down -v
podman-compose -f docker-compose.dev.yaml up -d
python scripts/app_init.py --seed
```

### 8. Datenbanken stoppen

```bash
podman-compose -f docker-compose.dev.yaml down      # Daten behalten
podman-compose -f docker-compose.dev.yaml down -v   # Daten entfernen
```

## Architektur

Das System verwendet ein **Universal Agent** Pattern - ein einzelner konfigurationsgesteuerter Agent, der durch Änderung seiner Konfiguration entweder als Creator oder Validator deployed werden kann:

```
┌─────────────────────────────────────────────────────────────────────┐
│                           DASHBOARD                                 │
│                  (Streamlit UI - Job-Verwaltung)                    │
└───────────────────────────────────┬─────────────────────────────────┘
                                    │
         ┌──────────────────────────┴──────────────────────────┐
         │                                                     │
         ▼                                                     ▼
┌───────────────────────────┐                    ┌───────────────────────────┐
│     UNIVERSAL AGENT       │                    │     UNIVERSAL AGENT       │
│   (config: creator)       │                    │   (config: validator)     │
│                           │                    │                           │
│ - Dokumentenverarbeitung  │  PostgreSQL Cache  │ - Graph Exploration       │
│ - Anforderungsextraktion  │ ◄────────────────► │ - Relevanzprüfung        │
│ - Recherche & Citations   │  (Anforderungen)   │ - Erfüllungsvalidierung  │
│                           │                    │ - Neo4j Integration       │
└───────────────────────────┘                    └───────────────────────────┘
                                                              │
                                                              ▼
                                                  ┌─────────────────────┐
                                                  │       Neo4j         │
                                                  │  (Knowledge Graph)  │
                                                  └─────────────────────┘
```

**Universal Agent:**

Dieselbe Codebase (`src/`) bedient beide Rollen. Das Verhalten wird durch Konfiguration bestimmt:

| Config | Zweck | Tools | Polling |
|--------|-------|-------|---------|
| `creator` | Anforderungen aus Dokumenten extrahieren | document, search, citation, cache | `jobs` Tabelle |
| `validator` | Validierung und Integration in den Graph | graph, cypher, validation | `requirements` Tabelle |

Configs befinden sich in `configs/{name}/` und erweitern Framework-Defaults über `$extends`.

**Datenfluss:**
1. Creator pollt die `jobs` Tabelle → verarbeitet Dokument → schreibt in die `requirements` Tabelle
2. Validator fragt ausstehende Anforderungen ab → validiert → integriert in Neo4j

## Cockpit (Debug Dashboard)

Angular Debug Dashboard zur Visualisierung der Agent-Ausführung in Echtzeit. Nützlich bei Entwicklung und Debugging.

### Zugriff

| Modus | URL |
|-------|-----|
| Docker (Dev Compose) | http://localhost:4000 |
| Lokale Entwicklung (`npm start`) | http://localhost:4200 |

> **Hinweis:** Die meisten Cockpit Panels (Audit Trail, Chat History, Request Viewer, Graph Timeline) lesen aus MongoDB. Das Cockpit startet auch ohne MongoDB, aber diese Panels bleiben dann leer. Sicherstellen, dass `MONGODB_URL` in `.env` gesetzt und MongoDB gestartet ist.

### Hauptkomponenten

- **Agent Activity** — Audit Trail der Agent-Ausführungsschritte
- **Graph Timeline** — Neo4j Graph-Visualisierung mit Timeline Playback
- **DB Table** — PostgreSQL Tabellen-Browser
- **Request Viewer** — LLM Request/Response Inspector
- **Todo List** — Agent Task Tracking
- **Chat History** — Agent Conversation Viewer

### Lokal ausführen

```bash
# Terminal 1: API
cd cockpit/api && pip install -r requirements.txt
uvicorn main:app --reload --port 8085

# Terminal 2: Frontend
cd cockpit && npm install && npm start   # http://localhost:4200
```

### MCP Integration

Das Cockpit enthält einen MCP Server, der Cockpit Metriken für Claude Code bereitstellt. Siehe `cockpit/mcp/` für die Konfiguration.

## Debugging

### Workspace-Dateien und Logs

Pro-Job-Dateien werden im Workspace-Verzeichnis gespeichert:
- **Workspace-Dateien**: `workspace/job_<uuid>/` - Enthält `workspace.md`, `todos.yaml`, `plan.md` und Unterverzeichnisse
- **Checkpoints**: `workspace/checkpoints/job_<id>.db` - SQLite Checkpoint für Resume-Funktionalität
- **Logs**: `workspace/logs/job_<id>.log` - Agent Execution Logs

```bash
# Checkpoint-/Log-Dateien für einen bestimmten Job bereinigen
rm workspace/checkpoints/job_<id>.db workspace/logs/job_<id>.log

# Alle Checkpoints und Logs bereinigen
rm workspace/checkpoints/job_*.db workspace/logs/job_*.log
```

### MongoDB Conversation Viewer

MongoDB speichert LLM Request/Response-Verläufe und Agent Audit Trails. Wenn `MONGODB_URL` konfiguriert ist (siehe [Umgebungsvariablen](#2-umgebungsvariablen-bearbeiten)), kann `scripts/view_llm_conversation.py` zur Untersuchung des Agent-Verhaltens verwendet werden:

```bash
# Alle Jobs mit Einträgen auflisten
python scripts/view_llm_conversation.py --list

# LLM Conversation für einen Job anzeigen
python scripts/view_llm_conversation.py --job-id <uuid>

# Job-Statistiken anzeigen (Token Usage, Latenz, Dauer)
python scripts/view_llm_conversation.py --job-id <uuid> --stats

# Vollständigen Agent Audit Trail anzeigen (alle Schritte)
python scripts/view_llm_conversation.py --job-id <uuid> --audit

# Nur Tool Calls im Audit Trail anzeigen
python scripts/view_llm_conversation.py --job-id <uuid> --audit --step-type tool_call

# Audit als Timeline-Visualisierung anzeigen
python scripts/view_llm_conversation.py --job-id <uuid> --audit --timeline

# Conversation als JSON exportieren
python scripts/view_llm_conversation.py --job-id <uuid> --export conversation.json

# Einzelnen LLM Request als HTML anzeigen (öffnet im Browser)
python scripts/view_llm_conversation.py --doc-id <mongodb_objectid> --temp

# Letzte Requests über alle Jobs anzeigen
python scripts/view_llm_conversation.py --recent 20
```

MongoDB Collections:
- `llm_requests` - Vollständiger LLM Request/Response mit Messages, Model, Latenz, Token Usage
- `agent_audit` - Schrittweiser Execution Trace (Tool Calls, Phase Transitions, Routing Decisions)

## Lizenz

Creative Commons Attribution 4.0 International License (CC BY 4.0). Siehe [LICENSE.txt](LICENSE.txt).
