#!/usr/bin/env python3
"""One-shot importer: MongoDB `srw_logs` backup -> Postgres `srw_audit`.

Backfills the pre-cutover MongoDB audit collections (agent_audit, llm_requests,
chat_history) into the new partitioned Postgres audit store so historical jobs
render again in the cockpit Audit/Chat/LLM panes.

Why this is a transform, not a `mongorestore`:
  * Mongo `_id` (ObjectId)            -> Postgres `id` (BIGINT, assigned here)
  * Mongo `job_id` (str)             -> Postgres `job_id` (UUID)
  * agent_audit: each Mongo doc      -> ONE append-only `event_phase='pre'` row;
    the step-specific top-level fields (tool/llm/error/state/workspace/...) are
    packed into the `payload` JSONB. Mongo already merged results inline, so the
    reader's JOIN-merge reproduces the original wire shape (read-parity).
  * chat_history.request_id (ObjectId str) -> the BIGINT id assigned to the
    referenced llm_requests row.
  * nested datetimes -> ISO-8601 'Z' strings inside JSONB (D22).
  * cleartext secrets in metadata.config_override / git URLs are REDACTED.

Safety:
  * Imported ids live in a high, non-overlapping band (default 1e9/2e9/3e9) far
    above the live BIGSERIAL sequences, so they never collide; the sequences are
    left untouched.
  * Idempotent: the import band is DELETEd before each load, so re-runs are clean.
  * The 2026_05 (and any missing in-range) monthly partitions are created with
    the same reloptions as migrations/audit/0001_initial.sql before loading.

Connection comes from env: PG_HOST PG_PORT PG_USER PG_PASSWORD PG_DB
                            MONGO_URI MONGO_DB
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import uuid

import asyncpg
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

# --- id bands (collision-proof: live sequences are in the hundreds) -----------
BAND = {"llm_requests": 1_000_000_000, "agent_audit": 2_000_000_000, "chat_history": 3_000_000_000}
BAND_WIDTH = 1_000_000_000

# --- redaction ----------------------------------------------------------------
SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|password|passwd|secret|access[_-]?key|private[_-]?key|client[_-]?secret|bearer|authorization)",
    re.I,
)
URL_CRED_RE = re.compile(r"://([^/\s:@]+):([^/\s@]+)@")
# bare API-key tokens (e.g. sk-…-internal) that get echoed into prompts / tool
# output / chat content, where key-name redaction can't see them.
SK_KEY_RE = re.compile(r"sk-[A-Za-z0-9._-]{12,}")
REDACTED = "[REDACTED]"


def _scrub_str(s: str) -> str:
    s = URL_CRED_RE.sub(r"://\1:" + REDACTED + "@", s)
    s = SK_KEY_RE.sub(REDACTED, s)
    return s


def _scrub_content(obj):
    """URL-credential + bare-key-token stripping, for free-form content blobs."""
    if isinstance(obj, dict):
        return {k: _scrub_content(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_content(v) for v in obj]
    if isinstance(obj, str):
        return _scrub_str(obj)
    return obj


def _redact_full(obj):
    """Key-name redaction + content scrub (for metadata)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and SECRET_KEY_RE.search(k):
                out[k] = REDACTED
            else:
                out[k] = _redact_full(v)
        return out
    if isinstance(obj, list):
        return [_redact_full(v) for v in obj]
    if isinstance(obj, str):
        return _scrub_str(obj)
    return obj


# --- JSONB encoding -----------------------------------------------------------
def _json_default(o):
    if isinstance(o, dt.datetime):
        s = o.astimezone(dt.timezone.utc).isoformat()
        return s.replace("+00:00", "Z")
    if isinstance(o, (dt.date, dt.time)):
        return o.isoformat()
    return str(o)  # ObjectId, Decimal, bytes-ish — safe fallback


def _jsonb(v):
    return json.dumps(v, default=_json_default)


def _uuid(v):
    return uuid.UUID(str(v))


# --- column orders (must match the COPY `columns=`) ---------------------------
LLM_COLS = ["id", "job_id", "agent_type", "call_type", "model", "iteration", "timestamp",
            "latency_ms", "request", "response", "metadata", "auxiliary_metadata", "metrics"]
AUDIT_COLS = ["id", "job_id", "agent_type", "iteration", "step_type", "node_name", "phase",
              "phase_number", "timestamp", "latency_ms", "event_phase", "pre_id", "request_id",
              "payload", "metadata"]
CHAT_COLS = ["id", "job_id", "agent_type", "iteration", "model", "timestamp", "latency_ms",
             "phase", "phase_number", "request_id", "inputs", "response", "reasoning"]

AUDIT_PROMOTED = {"_id", "job_id", "agent_type", "iteration", "step_type", "node_name",
                  "phase", "phase_number", "timestamp", "latency_ms", "metadata", "step_number"}


def row_llm(doc, rid):
    return (
        rid, _uuid(doc["job_id"]), doc.get("agent_type"), doc.get("call_type") or "main",
        doc.get("model") or "unknown", doc.get("iteration"), doc["timestamp"], doc.get("latency_ms"),
        _scrub_content(doc.get("request") or {}), _scrub_content(doc.get("response") or {}),
        _redact_full(doc.get("metadata")), _scrub_content(doc.get("auxiliary_metadata")),
        _scrub_content(doc.get("metrics") or {}),
    )


def row_audit(doc, rid, lr_map):
    phase = doc.get("phase")
    phase_col = phase if isinstance(phase, str) else None
    payload = {k: v for k, v in doc.items() if k not in AUDIT_PROMOTED}
    if not isinstance(phase, str) and phase is not None:
        payload["phase"] = phase  # the phase_complete OBJECT quirk lives in payload
    if "id" in payload:  # memory_* steps carry a flat 'id' (memory uuid) that would clobber the row id on splat
        payload["memory_id"] = payload.pop("id")
    # Remap the nested llm.request_id (original Mongo ObjectId) -> the BIGINT id of
    # the imported llm_requests row, and set the canonical request_id column. Without
    # this, audit LLM steps link to dead ObjectIds (get_llm_request / drill-down fails).
    req_col = None
    llm = payload.get("llm")
    if isinstance(llm, dict) and llm.get("request_id") is not None:
        req_col = lr_map.get(str(llm["request_id"]))
        llm["request_id"] = req_col  # BIGINT, or None if unresolvable (better than a dead ObjectId)
    return (
        rid, _uuid(doc["job_id"]), doc.get("agent_type"), doc.get("iteration"),
        doc.get("step_type") or "unknown", doc.get("node_name"), phase_col, doc.get("phase_number"),
        doc["timestamp"], doc.get("latency_ms"), "pre", None, req_col,
        _scrub_content(payload), _redact_full(doc.get("metadata")),
    )


def row_chat(doc, rid, lr_map):
    req = doc.get("request_id")
    return (
        rid, _uuid(doc["job_id"]), doc.get("agent_type"), doc.get("iteration"), doc.get("model"),
        doc["timestamp"], doc.get("latency_ms"), doc.get("phase"), doc.get("phase_number"),
        lr_map.get(str(req)) if req is not None else None,
        _scrub_content(doc.get("inputs") or []), _scrub_content(doc.get("response") or {}),
        _scrub_content(doc.get("reasoning")),
    )


def _months_in_range(lo: dt.datetime, hi: dt.datetime):
    y, m = lo.year, lo.month
    out = []
    while (y, m) <= (hi.year, hi.month):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


async def ensure_partitions(conn, months):
    await conn.execute("SET TIME ZONE 'UTC'")
    for parent in ("llm_requests", "agent_audit", "chat_history"):
        for (y, m) in months:
            start = dt.date(y, m, 1)
            end = dt.date(y + (m == 12), (m % 12) + 1, 1)
            part = f"{parent}_p{y}_{m:02d}"
            await conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{part}" PARTITION OF "{parent}" '
                f"FOR VALUES FROM ('{start}') TO ('{end}')"
            )
            await conn.execute(
                f'ALTER TABLE "{part}" SET (fillfactor=100, '
                "autovacuum_vacuum_insert_scale_factor=0.05, autovacuum_vacuum_insert_threshold=10000, "
                "autovacuum_analyze_scale_factor=0.02, autovacuum_freeze_min_age=0)"
            )
            print(f"  partition ready: {part} [{start} .. {end})")


async def load(conn, table, coll, cols, transform, limit, id_map=None, batch=2000):
    """Keyset-paginated load. Each chunk is a fresh short-lived find() (no long-
    lived cursor to stall on a hung getMore); MongoClient timeouts fail loud.
    id_map (when given) maps Mongo _id->BIGINT so ids are stable across re-runs."""
    base = BAND[table]
    await conn.execute(f"DELETE FROM {table} WHERE id >= $1 AND id < $2", base, base + BAND_WIDTH)
    n, idx, last_id = 0, 0, None
    while True:
        q = {} if last_id is None else {"_id": {"$gt": last_id}}
        chunk = None
        for attempt in range(4):
            try:
                chunk = list(coll.find(q).sort("_id", ASCENDING).limit(batch))
                break
            except PyMongoError as e:
                if attempt == 3:
                    raise
                print(f"\n  [retry {attempt + 1}/3] {table} read after {last_id}: {type(e).__name__}: {e}")
        if not chunk:
            break
        last_id = chunk[-1]["_id"]
        recs = []
        for doc in chunk:
            if limit and idx >= limit:
                break
            rid = id_map[str(doc["_id"])] if id_map is not None else base + idx
            recs.append(transform(doc, rid))
            idx += 1
        if recs:
            await conn.copy_records_to_table(table, records=recs, columns=cols)
            n += len(recs)
            print(f"    {table}: {n} rows…", end="\r", flush=True)
        if limit and idx >= limit:
            break
    print(f"  loaded {table}: {n} rows" + " " * 20)
    return n


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="rows per collection (0 = all; use small for a smoke test)")
    ap.add_argument("--tables", default="llm_requests,chat_history,agent_audit",
                    help="comma list; llm_requests must precede chat_history (request_id remap)")
    args = ap.parse_args()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]

    mc = MongoClient(
        os.environ.get("MONGO_URI", "mongodb://127.0.0.1:47017"),
        tz_aware=True, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000, socketTimeoutMS=120000,
    )
    mdb = mc[os.environ.get("MONGO_DB", "srw_logs")]

    # partition span from actual data
    lo = hi = None
    for coll in ("agent_audit", "llm_requests", "chat_history"):
        a = next(iter(mdb[coll].find().sort("timestamp", ASCENDING).limit(1)), None)
        b = next(iter(mdb[coll].find().sort("timestamp", -1).limit(1)), None)
        if a:
            lo = a["timestamp"] if lo is None else min(lo, a["timestamp"])
        if b:
            hi = b["timestamp"] if hi is None else max(hi, b["timestamp"])
    months = _months_in_range(lo, hi)
    print(f"data span: {lo.date()} .. {hi.date()}  -> partitions {months}")

    conn = await asyncpg.connect(
        host=os.environ["PG_HOST"], port=int(os.environ.get("PG_PORT", 5432)),
        user=os.environ["PG_USER"], password=os.environ["PG_PASSWORD"],
        database=os.environ.get("PG_DB", "srw_audit"),
    )
    # binary codec: copy_records_to_table streams in BINARY format. jsonb binary
    # wire format = 1-byte version header (0x01) + UTF-8 JSON text.
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: b"\x01" + _jsonb(v).encode("utf-8"),
        decoder=lambda data: json.loads(bytes(data)[1:].decode("utf-8")),
        schema="pg_catalog",
        format="binary",
    )
    try:
        print("ensuring partitions…")
        await ensure_partitions(conn, months)

        # Precompute llm_requests ObjectId -> BIGINT map (cheap _id-only scan) so
        # chat/agent_audit cross-refs resolve regardless of which tables we load
        # (and re-runs of a single table stay id-stable).
        lr_map = {}
        lbase = BAND["llm_requests"]
        for i, d in enumerate(mdb.llm_requests.find({}, {"_id": 1}).sort("_id", ASCENDING)):
            lr_map[str(d["_id"])] = lbase + i
        print(f"lr_map: {len(lr_map)} llm_requests ids")

        if "llm_requests" in tables:
            await load(conn, "llm_requests", mdb.llm_requests, LLM_COLS, row_llm,
                       args.limit, id_map=lr_map, batch=1000)
        if "chat_history" in tables:
            await load(conn, "chat_history", mdb.chat_history, CHAT_COLS,
                       lambda d, r: row_chat(d, r, lr_map), args.limit, batch=1000)
        if "agent_audit" in tables:
            await load(conn, "agent_audit", mdb.agent_audit, AUDIT_COLS,
                       lambda d, r: row_audit(d, r, lr_map), args.limit, batch=2000)

        print("\nverification (imported band per table):")
        for t in ("llm_requests", "chat_history", "agent_audit"):
            base = BAND[t]
            row = await conn.fetchrow(
                f"SELECT count(*) n, count(distinct job_id) jobs, min(timestamp) lo, max(timestamp) hi "
                f"FROM {t} WHERE id >= $1 AND id < $2", base, base + BAND_WIDTH)
            print(f"  {t}: {row['n']} rows, {row['jobs']} jobs, {row['lo']} .. {row['hi']}")
        # cross-ref integrity: chat rows whose request_id resolves to an imported llm row
        if "chat_history" in tables and "llm_requests" in tables:
            res = await conn.fetchrow(
                "SELECT count(*) tot, count(*) FILTER (WHERE EXISTS "
                "(SELECT 1 FROM llm_requests l WHERE l.id = c.request_id)) ok "
                "FROM chat_history c WHERE c.id >= $1 AND c.id < $2",
                BAND["chat_history"], BAND["chat_history"] + BAND_WIDTH)
            print(f"  chat.request_id -> llm_requests resolves: {res['ok']}/{res['tot']}")
    finally:
        await conn.close()
        mc.close()


if __name__ == "__main__":
    asyncio.run(main())
