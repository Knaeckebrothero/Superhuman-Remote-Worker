# Configuration Guide

Complete reference for configuring the Citation Engine.

## Environment Variables

All configuration is done through environment variables. Create a `.env` file in your project root or set them in your shell.

### Database Configuration

#### SQLite (Basic Mode)

| Variable | Default | Description |
|----------|---------|-------------|
| `CITATION_DB_PATH` | `./citations.db` | Path to SQLite database file |

```bash
CITATION_DB_PATH=./data/citations.db
```

#### PostgreSQL (Multi-Agent Mode)

| Variable | Default | Description |
|----------|---------|-------------|
| `CITATION_DB_URL` | *none* | Full PostgreSQL connection URL |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_DB` | `citations` | Database name |
| `POSTGRES_USER` | `citation_user` | Database user |
| `POSTGRES_PASSWORD` | *none* | Database password |
| `POSTGRES_MIN_CONNECTIONS` | `1` | Connection pool minimum |
| `POSTGRES_MAX_CONNECTIONS` | `10` | Connection pool maximum |

```bash
# Option 1: Full URL (recommended)
CITATION_DB_URL=postgresql://user:password@localhost:5432/citations

# Option 2: Individual settings
POSTGRES_HOST=db.example.com
POSTGRES_PORT=5432
POSTGRES_DB=citations
POSTGRES_USER=citation_user
POSTGRES_PASSWORD=secure_password
```

**Note:** If `CITATION_DB_URL` is set, it takes precedence over individual `POSTGRES_*` variables.

---

### LLM Configuration

The Citation Engine uses an LLM to verify that quotes exist in sources and support claims.

| Variable | Default | Description |
|----------|---------|-------------|
| `CITATION_LLM_URL` | *none* (uses OpenAI) | Custom LLM endpoint URL |
| `CITATION_LLM_MODEL` | `gpt-4o-mini` | Model name/identifier |
| `OPENAI_API_KEY` | *none* | OpenAI API key |

#### Using OpenAI

```bash
OPENAI_API_KEY=sk-...
CITATION_LLM_MODEL=gpt-4o-mini  # or gpt-4o, gpt-3.5-turbo
```

#### Using Local LLM (llama.cpp)

```bash
CITATION_LLM_URL=http://localhost:8080/v1
CITATION_LLM_MODEL=mistral-7b
# OPENAI_API_KEY not needed for local models
```

#### Using Ollama

```bash
CITATION_LLM_URL=http://localhost:11434/v1
CITATION_LLM_MODEL=llama2
```

#### Using vLLM

```bash
CITATION_LLM_URL=http://localhost:8000/v1
CITATION_LLM_MODEL=meta-llama/Llama-2-7b-chat-hf
```

---

### Citation Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `CITATION_REASONING_REQUIRED` | `low` | When `relevance_reasoning` is required |

**`CITATION_REASONING_REQUIRED` values:**

| Value | Behavior |
|-------|----------|
| `none` | Never require reasoning |
| `low` | Require when confidence is "low" |
| `medium` | Require when confidence is "low" or "medium" |
| `high` | Always require reasoning |

```bash
# Require reasoning for all non-high-confidence citations
CITATION_REASONING_REQUIRED=medium
```

---

### Logging Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Console log level |
| `LOG_FILE_LEVEL` | `DEBUG` | File log level |
| `LOG_FORMAT` | `%(asctime)s - %(name)s - %(levelname)s - %(message)s` | Log format string |
| `LOG_DATE_FORMAT` | `%Y-%m-%d %H:%M:%S` | Date format |
| `LOG_DIRECTORY` | `.filesystem/logs` | Log file directory |
| `LOG_FILE` | `citation_engine.log` | Log file name |

```bash
LOG_LEVEL=DEBUG
LOG_FILE_LEVEL=DEBUG
LOG_FORMAT="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DIRECTORY=./logs
LOG_FILE=citations.log
```

---

### Test Configuration

These variables control which integration tests run.

| Variable | Default | Description |
|----------|---------|-------------|
| `RUN_POSTGRES_TESTS` | `false` | Run PostgreSQL integration tests |
| `RUN_LLM_TESTS` | `false` | Run LLM verification tests |
| `RUN_WEB_TESTS` | `false` | Run web source tests |

```bash
# Enable all integration tests
RUN_POSTGRES_TESTS=true
RUN_LLM_TESTS=true
RUN_WEB_TESTS=true
```

---

## Complete Example

### Development (.env)

```bash
# Database - SQLite for development
CITATION_DB_PATH=./dev_citations.db

# LLM - Local llama.cpp server
CITATION_LLM_URL=http://localhost:8080/v1
CITATION_LLM_MODEL=mistral-7b-instruct

# Behavior
CITATION_REASONING_REQUIRED=low

# Logging - verbose for development
LOG_LEVEL=DEBUG
LOG_FILE_LEVEL=DEBUG
LOG_DIRECTORY=./logs

# Tests
RUN_POSTGRES_TESTS=false
RUN_LLM_TESTS=true
RUN_WEB_TESTS=true
```

### Production (.env)

```bash
# Database - PostgreSQL for multi-agent
CITATION_DB_URL=postgresql://citation_user:${DB_PASSWORD}@db.prod.internal:5432/citations
POSTGRES_MIN_CONNECTIONS=5
POSTGRES_MAX_CONNECTIONS=20

# LLM - OpenAI for production
OPENAI_API_KEY=${OPENAI_API_KEY}
CITATION_LLM_MODEL=gpt-4o-mini

# Behavior - strict reasoning requirements
CITATION_REASONING_REQUIRED=medium

# Logging - production levels
LOG_LEVEL=WARNING
LOG_FILE_LEVEL=INFO
LOG_DIRECTORY=/var/log/citation-engine
```

---

## Programmatic Configuration

You can also configure the engine programmatically:

```python
from citation_engine import CitationEngine

# Override database path
engine = CitationEngine(mode="basic", db_path="/custom/path/citations.db")

# Override PostgreSQL URL
engine = CitationEngine(
    mode="multi-agent",
    db_url="postgresql://user:pass@host:5432/db"
)
```

**Note:** Programmatic configuration takes precedence over environment variables.

---

## Docker/Podman Configuration

When running in containers, pass environment variables:

```yaml
# docker-compose.yml / podman-compose.yml
services:
  app:
    image: your-app
    environment:
      - CITATION_DB_URL=postgresql://user:pass@postgres:5432/citations
      - CITATION_LLM_URL=http://llm-server:8080/v1
      - CITATION_LLM_MODEL=mistral-7b
    depends_on:
      - postgres
      - llm-server

  postgres:
    image: postgres:16-alpine
    environment:
      - POSTGRES_DB=citations
      - POSTGRES_USER=user
      - POSTGRES_PASSWORD=pass
```

---

## Troubleshooting

### LLM Connection Issues

```bash
# Test LLM endpoint
curl http://localhost:8080/v1/models

# Check environment
echo $CITATION_LLM_URL
echo $CITATION_LLM_MODEL
```

### Database Connection Issues

```bash
# Test PostgreSQL
psql $CITATION_DB_URL -c "SELECT 1"

# Check SQLite path exists
ls -la $(dirname $CITATION_DB_PATH)
```

### Missing Dependencies

```bash
# PDF support
pip install citation-engine[pdf]

# Web support
pip install citation-engine[web]

# PostgreSQL support
pip install citation-engine[postgresql]

# Everything
pip install citation-engine[full]
```
