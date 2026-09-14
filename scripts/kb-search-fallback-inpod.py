"""Run the KB lexical-fallback gate inside a local k3d agent pod.

From the repository root, pass the two checkout SHA-256s so an old image cannot
silently pass. The script uses the pod's DB configuration, creates randomly
scoped fixtures in one transaction, and always rolls it back. It neither calls
an external embedding provider nor changes the runtime's embedding settings.

    kubectl --context k3d-srw -n srw exec -i deploy/srw-agent-stateless -c agent -- \
      env EXPECTED_STORE_SHA=<sha256> EXPECTED_TOOLS_SHA=<sha256> \
      python - < scripts/kb-search-fallback-inpod.py
"""

import asyncio
import hashlib
import json
import os
import socket
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from pgvector.asyncpg import register_vector

from agent.core.knowledge_injection import retrieve_bound_knowledge
from agent.services.knowledge.bindings import KnowledgeBinding
from agent.tools.context import ToolContext
from agent.tools.knowledge import knowledge_tools
from shared.db_url import build_postgres_url
from shared.runtime.services import knowledge_store
from shared.runtime.services.embedding_service import EmbeddingService


class TransactionDB:
    """Serialize concurrent injection reads onto the fixture transaction."""

    def __init__(self, connection):
        self.connection = connection
        self.lock = asyncio.Lock()
        self.queries = []

    async def fetch(self, sql, *args):
        async with self.lock:
            self.queries.append(sql)
            return await self.connection.fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        async with self.lock:
            return await self.connection.fetchrow(sql, *args)

    @asynccontextmanager
    async def acquire(self):
        async with self.lock:
            yield self.connection


async def seed(
    connection, kb, slug, content, *, chunks=False, tags=(), status="active"
):
    note_row = uuid.uuid4()
    await connection.execute(
        """
        INSERT INTO knowledge_index
            (id, project_id, kb_id, note_id, title, note_type, status, content, tags, path)
        VALUES ($1, $2, $2, $3::text, $3::text, 'learning', $4, $5, $6, $7)
        """,
        note_row,
        kb,
        slug,
        status,
        content,
        list(tags),
        f"knowledge/{slug}.md",
    )
    if chunks:
        await connection.execute(
            """
            INSERT INTO knowledge_chunks
                (id, note_row, kb_id, chunk_ix, content, search_doc, embedding_version)
            VALUES ($1, $2, $3, 0, $4::text, to_tsvector('english', $4::text), 'old-model')
            """,
            uuid.uuid4(),
            note_row,
            kb,
            content,
        )


async def exercise(store, db, bindings, label):
    kb = bindings[0].kb_id
    results = await store.search_chunks(
        [kb],
        "authenticating requests",
        embedding_version="missing-model",
    )
    by_slug = {note.note_id: note for note in results}
    assert set(by_slug) == {"sparse", "literal"}, by_slug.keys()
    assert "sparse" in by_slug["sparse"].matched_arms
    assert by_slug["literal"].matched_arms == ["exact"]
    assert all("dense" not in note.matched_arms for note in results)
    assert results.lexical_fallback

    combined = await store.search_chunks(
        [kb],
        "authenticating requests",
        exact=["config_key"],
        tags=["hot"],
        embedding_version="missing-model",
    )
    assert {note.note_id for note in combined} == {
        "sparse",
        "literal",
        "identifier",
        "tagged",
    }

    context = ToolContext(knowledge_store=store, knowledge_bindings=bindings)
    tools = {tool.name: tool for tool in knowledge_tools.create_kb_tools(context)}
    output = await asyncio.to_thread(
        tools["kb_search"].invoke,
        {"query": "authenticating requests", "kb": "project"},
    )
    assert "project:sparse" in output and "project:literal" in output
    assert "lexical fallback" in output and "⟨exact⟩" in output
    assert "dense" not in output and "restricted" not in output

    miss = await asyncio.to_thread(
        tools["kb_search"].invoke,
        {"query": "no lexical matches anywhere", "kb": "project"},
    )
    assert "No knowledge notes match" in miss
    assert "Embeddings unavailable" in miss and "Still indexing" in miss

    grep = await asyncio.to_thread(
        tools["kb_grep"].invoke,
        {"pattern": "config_key", "kb": "project"},
    )
    assert "project:identifier" in grep and "config_key" in grep

    selected = await retrieve_bound_knowledge(
        store,
        bindings,
        "authenticating requests",
        timeout=5,
    )
    assert {note.note_id for note in selected.notes} == {
        "sparse",
        "literal",
        "external",
    }
    block = knowledge_store.KnowledgeStore.assemble_knowledge_block(
        selected.notes,
        bindings=selected.bindings,
        external_watermarks=selected.external_watermarks,
    )
    assert "project:sparse" in block and "docs:external" in block and "⟨exact⟩" in block
    assert "restricted" not in block and "dense" not in block
    print(
        json.dumps(
            {
                "case": label,
                "status": "PASS",
                "notes": sorted(by_slug),
                "combined_notes": sorted(note.note_id for note in combined),
                "injected_notes": sorted(note.note_id for note in selected.notes),
                "zero_hit_notice": miss,
            }
        ),
        flush=True,
    )


async def main():
    for module, env in (
        (knowledge_store, "EXPECTED_STORE_SHA"),
        (knowledge_tools, "EXPECTED_TOOLS_SHA"),
    ):
        actual = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        assert actual == os.environ[env], (
            f"source mismatch: {module.__name__} ({actual})"
        )
        print(json.dumps({"module": module.__name__, "sha256": actual}), flush=True)

    connection = await asyncpg.connect(
        build_postgres_url("VECTOR_POSTGRES", fallback_env="VECTOR_DB_URL"),
        command_timeout=10,
    )
    kb, external, restricted = (uuid.uuid4() for _ in range(3))
    transaction = connection.transaction()
    try:
        await register_vector(connection)
        await transaction.start()
        try:
            await connection.execute("SET LOCAL statement_timeout = 10000")
            await seed(
                connection, kb, "sparse", "Authenticate requests safely", chunks=True
            )
            await seed(connection, kb, "literal", "authenticating requests matters")
            await seed(connection, kb, "recent", "unrelated", chunks=True)
            await seed(connection, kb, "identifier", "config_key is literal")
            await seed(connection, kb, "tagged", "tag selection", tags=["hot"])
            await seed(
                connection, kb, "archived", "authenticating requests", status="archived"
            )
            await seed(
                connection, external, "external", "authenticating requests externally"
            )
            await seed(
                connection,
                restricted,
                "restricted",
                "authenticating requests privately",
            )
            await connection.execute(
                "INSERT INTO kb_index_watermark (kb_id, status) VALUES ($1, 'partial')",
                kb,
            )
            bindings = [
                KnowledgeBinding(kb, "project", "Project", "native", True),
                KnowledgeBinding(external, "docs", "Docs", "datasource", False),
            ]
            db = TransactionDB(connection)
            await exercise(
                knowledge_store.KnowledgeStore(db, None),
                db,
                bindings,
                "no_embedding_service",
            )
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                svc = EmbeddingService(
                    provider="local",
                    model="unavailable-model",
                    api_key="test-only",
                    base_url=f"http://127.0.0.1:{reserved.getsockname()[1]}/v1",
                )
                svc._client.max_retries = 0
                svc._client.timeout = 1.0
                try:
                    await exercise(
                        knowledge_store.KnowledgeStore(db, svc),
                        db,
                        bindings,
                        "unreachable_embedding_endpoint",
                    )
                finally:
                    await svc._client.close()

            class HealthyEmbeddings:
                async def embed(self, text):
                    return [1.0] + [0.0] * 4095

            healthy = knowledge_store.KnowledgeStore(db, HealthyEmbeddings())
            db.queries.clear()
            results = await healthy.search_chunks(
                [kb],
                "authenticating requests",
                embedding_version="old-model",
            )
            assert (
                len(db.queries) == 1
                and "knowledge_chunk_hybrid_search(" in db.queries[0]
            )
            direct = await connection.fetch(
                "SELECT * FROM knowledge_chunk_hybrid_search($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                "authenticating requests",
                await healthy.embedding_service.embed("q"),
                [kb],
                "old-model",
                50,
                0.6,
                0.3,
                0.1,
                60,
            )
            assert [note.id for note in results] == [row["id"] for row in direct][:15]
            assert not getattr(results, "lexical_fallback", False)
            print(
                json.dumps({"case": "healthy_ranking_unchanged", "status": "PASS"}),
                flush=True,
            )
        finally:
            await transaction.rollback()

        for table in ("knowledge_index", "knowledge_chunks", "kb_index_watermark"):
            remaining = await connection.fetchval(
                f"SELECT count(*) FROM {table} WHERE kb_id = ANY($1)",
                [kb, external, restricted],
            )
            assert remaining == 0, (table, remaining)
        print(
            json.dumps(
                {"case": "fixture_rollback", "status": "PASS", "remaining_rows": 0}
            ),
            flush=True,
        )
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
