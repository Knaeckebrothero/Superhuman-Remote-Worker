#!/usr/bin/env bash
# =============================================================================
# Generate the canonical current-schema artifacts (HF-1 / Phase 2 of the DB
# roadmap). This is the root-cause fix for the schema-drift bug class: instead
# of a hand-maintained frozen snapshot (schema.sql / vector_schema.sql) that
# silently rots as migrations land, we replay every migration from zero into a
# throwaway container and dump the result. The dump IS the schema of record;
# CI fails if a migration lands without regenerating it (see
# .github/workflows/db-migrations.yml, artifact job).
#
# One script owns the whole lifecycle per DB family so that local == CI by
# construction:
#   start scratch container -> pg_isready -> migrate-from-zero (real apply,
#   so .notx.sql CONCURRENTLY files run too) -> pg_dump INSIDE the container
#   (client == server, no runner-side pg_dump drift) -> normalize -> write.
#
# Images are pinned to the PROD majors (helm/values.yaml): app + vector are
# pgvector/pgvector:pg15, audit is postgres:16. pg_dump output is
# major-version-sensitive, so the artifact must be dumped on the same major it
# runs on in prod — the existing CI dry-run uses pg16 for app+vector, which is
# wrong for artifact purposes.
#
# Determinism traps handled:
#   * The `\restrict <token>` / `\unrestrict <token>` lines pg_dump emits since
#     the Aug-2025 security release (CVE-2025-8714) carry a RANDOM token per
#     dump. We pin it with --restrict-key when the pg_dump build supports it
#     AND strip the lines entirely in normalization (belt and suspenders).
#   * The `-- Dumped from database version` / `-- Dumped by pg_dump version`
#     header lines are stripped so a Postgres MINOR bump (pg15.4 -> pg15.5)
#     doesn't churn the artifact for no schema reason.
#   * The audit migrations seed their monthly partitions RELATIVE TO now()
#     (`date_trunc('month', now()) + i months`), so replaying the identical
#     migration chain in July yields _p2026_07..09 and in August _p2026_08..10.
#     That is drift in the dump but not in the schema, and it made the CI
#     freshness gate fail for everyone on the 1st of every month. The rolling
#     months are canonicalized to a fixed synthetic epoch (see
#     canonicalize_rolling_partitions).
# Everything else pg_dump emits is deterministic for a fixed input + image, so
# the artifact is byte-stable and reviewable as a plain diff.
#
# Usage:
#   scripts/schema-snapshot.sh                 # regenerate all three artifacts
#   scripts/schema-snapshot.sh app             # one family (app|vector|audit)
#   scripts/schema-snapshot.sh --check         # regenerate to temp + diff vs
#                                              #   committed; exit 1 on drift
#   CONTAINER_ENGINE=podman scripts/schema-snapshot.sh   # force engine
#   PYTHON=./venv/bin/python scripts/schema-snapshot.sh  # runner interpreter
#
# Requires: docker or podman, and a Python with asyncpg + cryptography (the
# migration runner's deps) reachable as $PYTHON.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OUT_DIR="orchestrator/database"

# --- interpreter for the migration runner ----------------------------------
# The runner imports `security.crypto` / `utils.db_url` assuming orchestrator/
# is sys.path[0], so we always invoke it as `-m database.migrate` from inside
# orchestrator/ (mirrors the CI dry-run job's working-directory). Database
# modules also import shared packages from the repository-root `src/` tree, so
# the migration invocation explicitly carries REPO_ROOT in PYTHONPATH.
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$REPO_ROOT/venv/bin/python" ]]; then
    PYTHON="$REPO_ROOT/venv/bin/python"
  else
    PYTHON="python3"
  fi
fi

# --- container engine (docker preferred for CI parity, podman fallback) -----
ENGINE="${CONTAINER_ENGINE:-}"
if [[ -z "$ENGINE" ]]; then
  if command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  else
    echo "ERROR: neither docker nor podman found on PATH." >&2
    exit 1
  fi
fi

# --- family table: name | image | db | migrations-subdir | out | host-port --
# Fixed host ports (uncommon range) so the host-side runner can reach each
# scratch container on localhost; families run sequentially and are torn down
# between runs.
FAMILIES=(
  "app    pgvector/pgvector:pg15 app    app    schema_current.sql        55432"
  "vector pgvector/pgvector:pg15 vector vector vector_schema_current.sql 55433"
  "audit  postgres:16            audit  audit  audit_schema_current.sql  55434"
)

PGPASS="srwsnapshot"
STARTED_CONTAINERS=()

cleanup() {
  for cid in "${STARTED_CONTAINERS[@]:-}"; do
    [[ -n "$cid" ]] && "$ENGINE" rm -f "$cid" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

# Wait until the REAL Postgres server accepts a TCP connection on the published
# host port. Uses a host-side asyncpg probe (asyncpg is the runner's own dep, so
# it is always present) over the SAME path the migrate step will use. This
# deliberately avoids an in-container `pg_isready`: the official image runs a
# temporary UNIX-socket-only server during initdb, so an exec'd pg_isready can
# pass before the real TCP listener exists, and the subsequent restart resets
# the migration connection mid-run. $1 = host port, $2 = db.
wait_ready() {
  local port="$1" db="$2" i
  for i in $(seq 1 60); do
    if PGPROBE_DSN="postgresql://postgres:$PGPASS@localhost:$port/$db" \
       "$PYTHON" - <<'PY' 2>/dev/null
import asyncio, os, asyncpg
async def main():
    c = await asyncpg.connect(os.environ["PGPROBE_DSN"], timeout=3)
    await c.execute("SELECT 1")
    await c.close()
asyncio.run(main())
PY
    then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

# Emit the GENERATED banner for a family to stdout. $1 = family name.
emit_header() {
  local fam="$1"
  cat <<EOF
-- =============================================================================
-- GENERATED FILE — DO NOT EDIT BY HAND.
--
-- Canonical current schema for the '$fam' database, produced by replaying every
-- migration under orchestrator/database/migrations/$fam/ from zero into a
-- throwaway container and dumping the result.
--
-- Source of truth : orchestrator/database/migrations/$fam/*.sql
-- Regenerate      : scripts/schema-snapshot.sh $fam
-- CI enforces that this file matches a fresh regeneration (db-migrations.yml).
-- The frozen orchestrator/database/{schema,vector_schema}.sql snapshots are a
-- separate, historical concern; THIS file tracks the live migration chain.
--
EOF
  case "$fam" in
    app)
      cat <<'EOF'
-- Runtime-only objects NOT present here (created outside the migration runner):
--   * LangGraph checkpointer tables (checkpoints, checkpoint_blobs,
--     checkpoint_writes, checkpoint_migrations) — created by
--     AsyncPostgresSaver.setup() (src/agent.py) only under
--     CHECKPOINTER_BACKEND=postgres, in the control-plane DB by default.
EOF
      ;;
    audit)
      cat <<'EOF'
-- Runtime-only objects NOT present here (created outside the migration runner):
--   * Monthly audit partition children beyond the migration-seeded ones —
--     created at runtime by services/audit_partitions.py
--     (CREATE TABLE ... (LIKE parent)) as time advances.
--
-- The monthly partitions below are named _p1970_01, _p1970_02, ... and bounded
-- on 1970 dates. Those months are SYNTHETIC. The migrations seed them relative to
-- now() (current month + 2 lookahead), so a literal dump would rename every
-- leaf on the 1st of each month and report schema drift where none exists; the
-- snapshot rewrites the rolling window onto a fixed epoch instead. Real
-- databases carry the actual months. Only the count, bounds width, storage
-- params, indexes and constraints of these leaves are meaningful here.
EOF
      ;;
    *)
      echo "-- (No runtime-only objects for this family.)"
      ;;
  esac
  echo "-- =============================================================================="
  echo ""
}

# Synthetic year the rolling monthly partitions are rewritten onto. Any year is
# fine as long as it is fixed and obviously not a real data month; 1970 reads as
# "epoch placeholder" and keeps the artifact valid, applyable SQL.
EPOCH_YEAR=1970

# Canonicalize rolling monthly partitions (`<parent>_pYYYY_MM`) onto $EPOCH_YEAR
# so the artifact depends on the migration chain alone, never on the wall clock
# of whoever ran the dump. Reads a dump body on stdin, writes the rewritten body
# on stdout.
#
# The i-th chronologically-distinct month found becomes $EPOCH_YEAR-i, and the
# `FOR VALUES FROM/TO` literals move with it (the last partition's exclusive
# upper bound is the month after the last seeded one, and appears only as a
# literal, never as a name). Correctness rests on two properties:
#   * YYYY_MM sorts lexicographically == chronologically, so the mapping is
#     stable across a year boundary (2026_11, 2026_12, 2027_01 -> _01, _02, _03).
#   * The rewrite is textual and order-preserving, and every leaf of a parent
#     shares the `<parent>_p` prefix, so the leaves stay grouped between the same
#     neighbours pg_dump put them between — the substitution can never reorder
#     the file.
# A no-op for families with no such partitions (currently app and vector).
canonicalize_rolling_partitions() {
  local text months m y mm i=0 sed_args=() last=""
  text="$(cat)"

  months="$(grep -oE '_p[0-9]{4}_[0-9]{2}' <<<"$text" | sed 's/^_p//' | sort -u)"
  if [[ -z "$months" ]]; then
    printf '%s\n' "$text"
    return 0
  fi

  while read -r m; do
    [[ -z "$m" ]] && continue
    i=$((i + 1))
    y="${m%_*}"
    mm="${m#*_}"
    sed_args+=(-e "s/_p${y}_${mm}/_p${EPOCH_YEAR}_$(printf '%02d' "$i")/g")
    sed_args+=(-e "s/'${y}-${mm}-01 00:00:00+00'/'${EPOCH_YEAR}-$(printf '%02d' "$i")-01 00:00:00+00'/g")
    last="$m"
  done <<<"$months"

  # Exclusive upper bound of the final partition = the month after $last.
  y="${last%_*}"
  mm=$((10#${last#*_} + 1))
  if ((mm > 12)); then
    mm=1
    y=$((y + 1))
  fi
  sed_args+=(-e "s/'${y}-$(printf '%02d' "$mm")-01 00:00:00+00'/'${EPOCH_YEAR}-$(printf '%02d' $((i + 1)))-01 00:00:00+00'/g")

  printf '%s\n' "$text" | sed "${sed_args[@]}"
}

# Generate one family's artifact into $OUT_DIR (or a caller-supplied dir via
# $2). $1 = family spec line.
generate_one() {
  read -r fam image db subdir outfile port <<<"$1"
  local dest_dir="${2:-$OUT_DIR}"
  local dest="$dest_dir/$outfile"

  echo ">> [$fam] scratch $image on :$port -> $dest" >&2

  # Deterministic name + pre-remove so a leftover from a crashed run can never
  # hold the port or shadow this container.
  local name="srw-snapshot-$fam"
  "$ENGINE" rm -f "$name" >/dev/null 2>&1 || true
  local cid
  cid="$("$ENGINE" run -d --name "$name" \
    -e "POSTGRES_USER=postgres" \
    -e "POSTGRES_PASSWORD=$PGPASS" \
    -e "POSTGRES_DB=$db" \
    -p "$port:5432" \
    "$image")"
  STARTED_CONTAINERS+=("$name")

  if ! wait_ready "$port" "$db"; then
    echo "ERROR: [$fam] container never accepted a TCP connection." >&2
    "$ENGINE" logs "$name" >&2 || true
    return 1
  fi

  # Migrate from zero (real apply — runs .notx.sql CONCURRENTLY files too).
  ( cd orchestrator && \
    PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    DATABASE_URL="postgresql://postgres:$PGPASS@localhost:$port/$db" \
    "$PYTHON" -m database.migrate --dir "database/migrations/$subdir" ) >&2

  # Dump INSIDE the container (client == server by construction). Pin the
  # random \restrict token (CVE-2025-8714) with --restrict-key when the build
  # supports it; the key must be ALPHANUMERIC — underscores are rejected with
  # "invalid restrict key". Fall back to a plain dump on any rejection (older
  # builds lack the flag): normalization strips the \restrict lines regardless,
  # so the artifact is deterministic either way.
  local raw
  if ! raw="$("$ENGINE" exec "$cid" pg_dump -U postgres -d "$db" \
      --schema-only --no-owner --no-privileges \
      --no-security-labels --no-tablespaces \
      --restrict-key=srwsnapshot 2>/dev/null)"; then
    raw="$("$ENGINE" exec "$cid" pg_dump -U postgres -d "$db" \
      --schema-only --no-owner --no-privileges \
      --no-security-labels --no-tablespaces)"
  fi

  # Normalize: strip the random \restrict/\unrestrict lines and the version
  # header lines, then pin the now()-relative monthly partitions to a fixed
  # epoch; keep everything else (SETs, -- Name: banners, DDL).
  local body
  body="$(printf '%s\n' "$raw" \
    | grep -vE '^\\(un)?restrict ' \
    | grep -vE '^-- Dumped (from database|by pg_dump) version ' \
    | canonicalize_rolling_partitions)"

  mkdir -p "$dest_dir"
  { emit_header "$fam"; printf '%s\n' "$body"; } > "$dest"

  "$ENGINE" rm -f "$name" >/dev/null 2>&1 || true
  # Drop it from the cleanup list (already gone).
  local n=()
  local c
  for c in "${STARTED_CONTAINERS[@]}"; do [[ "$c" != "$name" ]] && n+=("$c"); done
  STARTED_CONTAINERS=("${n[@]:-}")

  echo ">> [$fam] wrote $(wc -l <"$dest") lines" >&2
}

# --- arg parsing ------------------------------------------------------------
CHECK=0
ONLY=""
for a in "$@"; do
  case "$a" in
    --check) CHECK=1 ;;
    app|vector|audit) ONLY="$a" ;;
    *) echo "ERROR: unknown arg '$a' (expected: app|vector|audit|--check)" >&2; exit 2 ;;
  esac
done

select_families() {
  local line
  for line in "${FAMILIES[@]}"; do
    read -r fam _rest <<<"$line"
    if [[ -z "$ONLY" || "$ONLY" == "$fam" ]]; then
      echo "$line"
    fi
  done
}

if [[ "$CHECK" -eq 1 ]]; then
  # Regenerate into a temp dir and diff against the committed artifacts.
  TMP="$(mktemp -d)"
  trap 'cleanup; rm -rf "$TMP"' EXIT
  drift=0
  while IFS= read -r line; do
    read -r fam _i _d _s outfile _p <<<"$line"
    generate_one "$line" "$TMP"
    if ! diff -u "$OUT_DIR/$outfile" "$TMP/$outfile" >/dev/null 2>&1; then
      echo "DRIFT: $OUT_DIR/$outfile is stale." >&2
      diff -u "$OUT_DIR/$outfile" "$TMP/$outfile" >&2 || true
      drift=1
    fi
  done < <(select_families)
  if [[ "$drift" -eq 1 ]]; then
    echo "" >&2
    echo "Schema artifacts are out of date. Run: scripts/schema-snapshot.sh && git add -A orchestrator/database/*_current.sql" >&2
    exit 1
  fi
  echo "OK: schema artifacts are up to date." >&2
else
  while IFS= read -r line; do
    generate_one "$line"
  done < <(select_families)
fi
