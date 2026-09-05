"""E6 — live smoke of the prefilter + purge lanes, run INSIDE the orchestrator
pod (python3 - < this file) with the pod's own DB facades and Gitea client.

The local cluster has no embedding profile, so the index rows are seeded by
hand with upsert_kb_note (the metadata half the reindexer would write) after
the files are committed through the real materialize path. Then:
  prefilter tick  -> orphan nursery notes archived (file + row), linked one kept
  purge tick(0s)  -> archived notes removed (file + row), linked one kept
Prints one line per step and a JSON summary.
"""

import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

PROJECT = os.environ["PROJECT"]
TS = int(time.time())


def blob_sha(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def note(slug, ntype, links=(), created_days_ago=30):
    created = (
        datetime.now(timezone.utc) - timedelta(days=created_days_ago)
    ).isoformat()
    body = f"# {slug}\n\nE6 lane smoke ({ntype}).\n"
    if links:
        body += (
            "\n## Relationships\n**REFERENCES:** "
            + ", ".join(f"[{l}]({l}.md)" for l in links)
            + "\n"
        )
    return (
        f'---\nid: {slug}\ntype: {ntype}\ndescription: "E6 lane smoke"\ntags: [e6]\n'
        f"status: active\ncreated: {created}\nmodified: {created}\n---\n{body}"
    )


async def main():
    from orchestrator.database.postgres import PostgresDB
    from orchestrator.services.gitea import GiteaClient
    from orchestrator.services.kb_materialize import materialize_knowledge_note
    from orchestrator.services.kb_prefilter import prefilter_kb_tick
    from orchestrator.services.kb_purge import purge_kb_tick
    from orchestrator.services.kb_reindex import resolve_kb_repo
    from shared.runtime.services.knowledge_store import KnowledgeStore
    from shared.db_url import build_postgres_url

    postgres_db = PostgresDB()
    await postgres_db.connect()
    vector_db = PostgresDB(
        connection_string=build_postgres_url(
            "VECTOR_POSTGRES", fallback_env="VECTOR_DB_URL"
        ),
        env_prefix="VECTOR_POSTGRES",
        default_min_connections=1,
        default_max_connections=2,
    )
    await vector_db.connect()
    gitea = GiteaClient()
    for name in ("initialize", "ensure_initialized", "init", "setup", "connect"):
        fn = getattr(gitea, name, None)
        if callable(fn):
            r = fn()
            if asyncio.iscoroutine(r):
                await r
            break
    print("S0a gitea initialized:", getattr(gitea, "_initialized", None))
    store = KnowledgeStore(db=vector_db, embedding_service=None)
    kb = uuid.UUID(PROJECT)
    resolved = await resolve_kb_repo(postgres_db, PROJECT)
    print("S0 repo:", resolved.repo if resolved else None)

    slugs = {
        "root": f"e6-decision-{TS}",  # active durable root
        "linked": f"e6-learning-linked-{TS}",  # nursery, linked from root -> must survive
        "orphan1": f"e6-learning-orphan-{TS}",
        "orphan2": f"e6-state-orphan-{TS}",
        "young": f"e6-learning-young-{TS}",  # orphan but 1 day old -> must survive (min age)
    }
    contents = {
        "root": note(slugs["root"], "decision", links=[slugs["linked"]]),
        "linked": note(slugs["linked"], "learning"),
        "orphan1": note(slugs["orphan1"], "learning"),
        "orphan2": note(slugs["orphan2"], "state"),
        "young": note(slugs["young"], "learning", created_days_ago=1),
    }
    types = {
        "root": "decision",
        "linked": "learning",
        "orphan1": "learning",
        "orphan2": "state",
        "young": "learning",
    }
    rows = {}
    for key, slug in slugs.items():
        res = await materialize_knowledge_note(
            postgres_db=postgres_db,
            gitea_client=gitea,
            project_id=PROJECT,
            slug=slug,
            content=contents[key],
        )
        assert res["status"] in ("committed", "skipped"), res
        created = datetime.now(timezone.utc) - timedelta(
            days=1 if key == "young" else 30
        )
        row_id = await store.upsert_kb_note(
            kb_id=kb,
            note_id=slug,
            path=f"knowledge/{slug}.md",
            title=slug,
            note_type=types[key],
            content=contents[key],
            blob_sha=blob_sha(contents[key]),
            embedding_version=None,
            status="active",
            tags=["e6"],
            created_at=created,
            modified_at=created,
        )
        rows[key] = row_id
    await store.replace_note_links(rows["root"], kb, slugs["root"], [slugs["linked"]])
    print("S1 seeded 5 files + rows; root -> linked link recorded")

    async def statuses():
        out = {}
        for key, slug in slugs.items():
            r = await vector_db.fetchrow(
                "SELECT status, invalidated_at FROM knowledge_index WHERE kb_id=$1 AND note_id=$2",
                kb,
                slug,
            )
            out[key] = (r["status"] if r else None, bool(r and r["invalidated_at"]))
        return out

    before = await statuses()
    print("S2 before:", before)

    pre = await prefilter_kb_tick(
        postgres_db=postgres_db,
        store=store,
        gitea_client=gitea,
        kb_id=kb,
        min_age=timedelta(days=7),
        limit=25,
    )
    after_pre = await statuses()
    print("S3 prefilter:", pre, "->", after_pre)

    # the file must say archived too (git is the truth)
    tree = await gitea.list_tree(resolved.repo, resolved.branch or "main")
    path_sha = {e["path"]: e["sha"] for e in tree if e.get("type") == "blob"}
    file_txt = await gitea.get_file_content(
        resolved.repo, f"knowledge/{slugs['orphan1']}.md"
    )
    print(
        "S4 orphan1 file status line:",
        [l for l in file_txt.splitlines() if l.startswith("status:")],
    )

    # Simulate what the reindex sweep does after any file rewrite: restamp the
    # row's blob_sha from the tree. Without it the purge lane's CAS refuses
    # (run 2: candidates=4 refused=4) — the safe direction, but not the flow.
    restamped = 0
    for key, slug in slugs.items():
        sha = path_sha.get(f"knowledge/{slug}.md")
        if sha:
            await vector_db.execute(
                "UPDATE knowledge_index SET blob_sha=$3 WHERE kb_id=$1 AND note_id=$2",
                kb,
                slug,
                sha,
            )
            restamped += 1
    print("S4b restamped blob_sha from tree for", restamped, "rows (sweep simulation)")
    purge = await purge_kb_tick(
        postgres_db=postgres_db,
        store=store,
        gitea_client=gitea,
        kb_id=kb,
        grace=timedelta(seconds=0),
        limit=25,
    )
    after_purge = await statuses()
    tree = await gitea.list_tree(resolved.repo, resolved.branch or "main")
    paths = {e["path"] for e in tree if e.get("type") == "blob"}
    present = {k: (f"knowledge/{s}.md" in paths) for k, s in slugs.items()}
    print("S5 purge:", purge, "->", after_purge, "files present:", present)

    # cleanup: remove the survivors' files + rows
    from orchestrator.services.kb_materialize import materialize_knowledge_note_delete

    for key in ("root", "linked", "young"):
        await materialize_knowledge_note_delete(
            postgres_db=postgres_db,
            gitea_client=gitea,
            project_id=PROJECT,
            slug=slugs[key],
            reason="e6 cleanup",
            store=store,
        )
    print(
        "SUMMARY",
        json.dumps(
            {
                "prefilter": pre,
                "after_prefilter": after_pre,
                "purge": purge,
                "after_purge": after_purge,
                "files_present_after_purge": present,
                "expect": "orphan1/orphan2 archived then purged; root/linked/young untouched",
            }
        ),
    )
    await vector_db.close()
    await postgres_db.close()


asyncio.run(main())
