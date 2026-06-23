# citation_engine tests

Test suite for the `citation_engine` package (`../../src/citation_engine/`).

After the **native SRW integration** (`docs/features/citation_engine_integration.md`),
the engine is async and Postgres-only — it runs against SRW's vector store
(`srw_vector`) through the agent's shared `PostgresDB` pool. SQLite /
`mode="basic"` and the engine's own embedding/LLM/DSN stacks were removed
(decision D3), and with them the fast in-process unit tests (`test_engine.py`,
`test_search.py`, `test_embeddings.py`) and the old-API web/LLM integration
tests. The single remaining test is the async Postgres round-trip below.

> Citation tool wiring is also covered by `tests/test_graph.py::TestEditCitationTool`
> (the `edit_citation` tool now routes through the engine on the vector store),
> which **does** run in CI.

## Layout

| File | Needs | Runs in CI? |
|------|-------|-------------|
| `test_integration_postgres.py` | live vector Postgres (`RUN_POSTGRES_TESTS=true`) | ⏭ skipped |

The round-trip is skip-guarded behind `RUN_POSTGRES_TESTS` and never runs in
CI's default `pytest tests/ -x -q` sweep, so it can't break CI.

## Run the Postgres round-trip against k3d

> **Why k3d and not a throwaway DB:** the engine never creates its own schema —
> SRW's `orchestrator/database/migrations/vector/` owns the `sources` /
> `citations` / `source_embeddings` / `job_sources` / `source_annotations` /
> `source_tags` tables. The round-trip must run against a database where those
> migrations have already applied the schema. On k3d that is the pgvector
> instance's **`srw_vector`** database (the same DB memory/KB use — there is no
> separate `citation_engine` database any more).

1. Pull the vector-DB credentials from the running orchestrator (resolves
   whatever the Secret holds — no secret-name guessing):

   ```bash
   CTX=k3d-srw; NS=srw
   V_USER=$(kubectl --context=$CTX -n $NS exec deploy/srw-orchestrator -c orchestrator -- printenv VECTOR_POSTGRES_USER)
   V_PW=$(kubectl --context=$CTX -n $NS exec deploy/srw-orchestrator -c orchestrator -- printenv VECTOR_POSTGRES_PASSWORD)
   V_DB=$(kubectl --context=$CTX -n $NS exec deploy/srw-orchestrator -c orchestrator -- printenv VECTOR_POSTGRES_DB)
   ```

2. Port-forward the pgvector service (local 5433 → service 5432):

   ```bash
   kubectl --context=$CTX -n $NS port-forward svc/srw-pgvector 5433:5432 &
   ```

3. Point the suite at it and run:

   ```bash
   export RUN_POSTGRES_TESTS=true
   export CITATION_DB_URL="postgresql://$V_USER:$V_PW@localhost:5433/${V_DB:-srw_vector}"
   pytest tests/citation_engine/test_integration_postgres.py -v
   ```

A green run confirms the integration end-to-end: the async engine connects to
the host-migrated schema on the shared vector pool, registers sources (with
`content_hash` dedup), writes and reads back citations, annotates/tags, runs FTS
keyword search, resets verification on edit, and enforces job-scoped isolation —
all on a real dev schema with asyncpg's strict typing (UUID `job_id`, JSONB,
enum casts, `vector(4096)`). The LLM verifier is skipped
(`CITATION_SKIP_VERIFICATION=true`), so no model endpoint is required.
