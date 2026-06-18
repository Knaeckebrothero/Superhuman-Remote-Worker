# citation_engine tests

Test suite for the vendored `citation_engine` package (`../../citation_engine/`),
folded in from its former standalone repo. This mirrors the package's original suite.

## Layout

| File | Needs | Runs in CI? |
|------|-------|-------------|
| `test_engine.py` | SQLite (in-process) | ✅ yes |
| `test_search.py` | SQLite + pure functions | ✅ yes |
| `test_embeddings.py` | mocked `httpx` | ✅ yes |
| `test_integration_postgres.py` | live Postgres (`RUN_POSTGRES_TESTS=true`) | ⏭ skipped |
| `test_integration_web.py` | outbound network (`RUN_WEB_TESTS=true`) | ⏭ skipped |
| `test_integration_llm.py` | LLM endpoint (`RUN_LLM_TESTS=true`) | ⏭ skipped |

The unit tests run as part of CI's `pytest tests/ -x -q`. The integration tests are
skip-guarded behind `RUN_*_TESTS` env flags and never run unless explicitly enabled,
so they can't break CI.

## Run the unit tests

```bash
pip install -r requirements.txt          # provides pymupdf, httpx, psycopg2-binary, etc.
pytest tests/citation_engine/ -q
```

## Run the Postgres round-trip against k3d

> **Why k3d and not a throwaway DB:** in multi-agent (Postgres) mode the engine no
> longer creates its own schema — SRW's `orchestrator/database/migrations/vector/`
> owns it (see `CitationEngine._initialize_schema`). So the round-trip must run
> against a database where those migrations have already applied the schema. On k3d
> that is the pgvector instance's `citation_engine` database.

1. Pull the citation DB credentials from the running orchestrator (resolves whatever
   the Secret holds — no secret-name guessing):

   ```bash
   CTX=k3d-srw; NS=srw
   CIT_USER=$(kubectl --context=$CTX -n $NS exec deploy/srw-orchestrator -c orchestrator -- printenv CITATION_POSTGRES_USER)
   CIT_PW=$(kubectl --context=$CTX -n $NS exec deploy/srw-orchestrator -c orchestrator -- printenv CITATION_POSTGRES_PASSWORD)
   ```

2. Port-forward the pgvector service (local 5433 → service 5432):

   ```bash
   kubectl --context=$CTX -n $NS port-forward svc/srw-pgvector 5433:5432 &
   ```

3. Point the suite at it and run:

   ```bash
   export RUN_POSTGRES_TESTS=true
   export CITATION_DB_URL="postgresql://$CIT_USER:$CIT_PW@localhost:5433/citation_engine"
   pytest tests/citation_engine/test_integration_postgres.py -v
   ```

A green run confirms the fold-in end-to-end: the vendored engine connects to the
host-migrated schema, registers sources, writes and reads back citations, and the
Step-3 "host owns the schema" change behaves correctly against the real dev schema.

## Web / LLM integration

Same pattern with `RUN_WEB_TESTS=true` (needs outbound network) or `RUN_LLM_TESTS=true`
(needs `CITATION_LLM_URL` / `OPENAI_API_KEY`). See each file's module docstring.
