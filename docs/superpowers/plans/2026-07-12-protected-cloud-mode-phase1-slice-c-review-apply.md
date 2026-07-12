# Protected Cloud Mode — Phase 1 Slice C: staging → review → apply + toggle — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A user creates a protected session from Cockpit, watches a "Cloud changes (N)" badge as the agent works, reviews the staged diff in a Monaco side-by-side panel, and Applies the whole diff to the real Nextcloud folder (or Rejects it) — including after the workspace pod is gone — while the agent honestly describes its cloud writes as staged.

**Architecture:** Three subsystems. (1) **Orchestrator staging + review/apply** (`orchestrator/services/cloud_staging/` new package + `diff_source.py`): at each turn end the agent pings an internal endpoint; the orchestrator SSH-streams the upperdir tar from the workspace pod to S3 (`cloud-staging/<thread_id>/upper.tar` + `manifest.json`), derives the diff manifest orchestrator-side from the tar + the mount's persisted etag baseline, and serves review/apply/reject through a `DiffSource` seam shared with Mode A. (2) **Agent-side** (`src/`): the turn-end trigger, an overlay-reset endpoint (post-apply upperdir clear + fresh workdir), an ENOTCONN monitor loop, and guard alignment. (3) **Cockpit**: the create-session checkbox (gated by a new `features` block on the capabilities payload), the status-bar badge, and the review panel reusing `job-diff-review`.

**Tech Stack:** FastAPI + asyncpg + boto3-in-thread (orchestrator), Python `tarfile` for manifest derivation, paramiko-backed `WorkspaceBackend` scripts (agent), Angular 19 signals + Monaco (Cockpit), pytest + vitest.

**Spec:** `docs/superpowers/specs/2026-07-12-protected-cloud-slice-c-design.md` (owner-approved 2026-07-12). Master design: `docs/design/cloud_access_unification.md`.

## Global Constraints

- **CI is the gate** (Python 3.12, no `/dev/fuse`, no live cloud, no live S3): every test is mocked — httpx `MockTransport`/`unittest.mock` fakes, `FakeMainCloudBackend` (`tests/cloud/fake.py`), `FakeRemoteBackend` script-text assertions, MagicMock'd boto3. Run `ruff check` on touched files before each commit.
- **Fail-closed:** a protected thread must NEVER reach a live cloud write path. Apply writes happen orchestrator-side, only on explicit user action. Engage failure ⇒ no cloud dir at all.
- **No S3 credentials on workspace or agent pods.** Only the orchestrator holds `SNAPSHOT_S3_ACCESS_KEY_ID`/`SNAPSHOT_S3_SECRET_ACCESS_KEY`; staging S3 keys are `cloud-staging/<thread_id>/upper.tar` and `cloud-staging/<thread_id>/manifest.json` in `S3_BUCKET` (default `srw-snapshots`).
- **Whole-diff apply, epoch-pinned:** Accept applies ALL staged changes, Reject discards ALL. Apply/reject carry the reviewed `staged_epoch`; mismatch ⇒ 409 `{"code": "epoch_stale"}`. Conflict gate mirrors Mode A's accept contract exactly: hard 409 `{"code": "external_modifications_detected", "diverged": [...]}`, NO force flag; partial write failure ⇒ 502 `{"code": "partial_write_failure"}` with staging retained.
- **Epoch semantics:** `staged_epoch` is monotonic; bumped on every successful stage push, apply, and reject. `staged_summary IS NULL` ⇔ nothing staged.
- **`schema_current.sql` is generated, never hand-edited:** after any migration run `scripts/schema-snapshot.sh app` and commit the regenerated file.
- **asyncpg JSONB returns raw JSON strings** — every JSONB read must `json.loads` when the value is a `str` (established codebase rule; no global codec).
- **Deletes before creates** in apply (whiteout-before-create ordering, spec §7).
- **Feature flag:** everything user-visible stays behind `PROTECTED_CLOUD_MODE_ENABLED` (helm `agent.protectedCloudModeEnabled`, dev ON / prod OFF), read via `main._is_protected_cloud_mode_enabled()` (`orchestrator/main.py:1226`).
- **Honesty copy:** the agent must say "staged for your review", never "saved to your cloud" (exact block text in Task 15).
- **Cockpit styles:** do NOT add styles to `persistent-chat.component.scss` (~60 KB source vs 48 kB compiled error budget). New UI uses `<app-badge>` and inline `styles: []` like `citations-panel`.
- **Angular tests are TestBed-free vitest specs over exported pure helpers** (pattern: `job-diff-review.component.spec.ts`, `persistent-chat.component.spec.ts`); rendering is covered by the cluster Playwright smoke, not unit tests.
- **Commit per task, path-scoped `git add` (never pathless — the working tree carries the owner's concurrent work), work directly on `develop`, NEVER push.**

## File map (who owns what)

| Path | Role |
|---|---|
| `orchestrator/database/migrations/app/0057_cloud_ro_mounts_staging.sql` | NEW — 4 staging columns on `cloud_ro_mounts` |
| `orchestrator/database/postgres.py` | +`update_ro_mount_baseline`, `update_ro_mount_staging`; `_ro_mount_row` JSONB loads |
| `orchestrator/services/cloud_staging/__init__.py` | NEW — `select_protected_mount` shared helper |
| `orchestrator/services/cloud_staging/manifest.py` | NEW — tar → manifest derivation (pure) |
| `orchestrator/services/cloud_staging/stage.py` | NEW — SSH tar stream → S3 push, debounce, signature skip |
| `orchestrator/services/cloud_staging/apply.py` | NEW — apply/reject engine |
| `orchestrator/services/diff_source.py` | NEW — `DiffSummary`/`DiffFileContent` dataclasses + `GiteaDiffSource` + `UpperdirDiffSource` |
| `orchestrator/services/cloud/ro_engage.py` | baseline capture at engage; factory gets `reader_id` |
| `orchestrator/services/snapshot_service.py` | +`upload_blob_file`, `delete_blob` |
| `orchestrator/services/workspace_suspension.py` | teardown stage hook |
| `orchestrator/services/job_cloud_baseline.py` | `detect_external_mods` core extracted (`detect_external_mods_against_baseline`) |
| `orchestrator/main.py` | internal cloud-stage endpoint; thread cloud-diff endpoints; job diff endpoints refactored onto `GiteaDiffSource`; capabilities `features` block; `_reader_client` reader_id fix |
| `src/api/persistent_app.py` | turn-end stage ping; `POST /cloud-overlay/reset` |
| `src/api/persistent_session.py` | overlay monitor loop; `reset_cloud_overlay()` |
| `src/services/cloud_overlay/overlay_mount.py` | +`reset_upper`; heal gets fresh workdir |
| `src/services/cloud_mount/__init__.py` | +`restart_mount` |
| `src/services/cloud_mount/guardrails.py` + `src/tools/shell/shell_tools.py` | upperdir guard write-only alignment |
| `config/prompts/systemprompt_interactive*.txt` (+ `orchestrator/config/prompts/` mirror) | honesty block |
| `src/core/loader.py` | `protected_cloud` in Jinja context |
| `cockpit/src/app/core/services/api.service.ts`, `capabilities.service.ts`, `core/models/api.model.ts` | thread cloud-diff API + types + features gate |
| `cockpit/src/app/views/session-create/session-create.component.ts` | protected checkbox |
| `cockpit/src/app/views/job-diff-review/job-diff-review.component.ts` | `threadId` generalization + binary render |
| `cockpit/src/app/views/persistent-chat/` | badge + panel host |

**Task order:** 1→2 (DB+engage foundations), 3→4→5 (staging pipeline), 6→7→8 (DiffSource + read endpoints), 9→10 (reset + apply), 11→12 (hardening), 13→14 (Cockpit), 15 (honesty), 16 (docs/close-out). Tasks 11, 12, 15 have no dependency on 6–10 and can be reordered if convenient, but the numbered order is the default.

---

### Task 1: migration 0057 + PostgresDB staging accessors

**Files:**
- Create: `orchestrator/database/migrations/app/0057_cloud_ro_mounts_staging.sql`
- Modify: `orchestrator/database/postgres.py` (methods live near `create_ro_mount` at :1259; `_ro_mount_row` at :1329)
- Modify (generated): `orchestrator/database/schema_current.sql`
- Test: `tests/cloud/test_ro_mount_staging_db.py` (new)

**Interfaces:**
- Consumes: existing `cloud_ro_mounts` CRUD (`create_ro_mount` :1259, `get_ro_mount_by_thread` :1299, `mark_ro_mount_revoked` :1320, `_ro_mount_row` :1329).
- Produces (later tasks rely on these exact signatures):
  - `async def update_ro_mount_baseline(self, row_id: str, baseline: dict[str, str]) -> bool`
  - `async def update_ro_mount_staging(self, row_id: str, *, staged_epoch: int, staged_summary: dict | None) -> bool` — sets `staged_at=now()` when summary is not None, else `staged_at=NULL`.
  - `_ro_mount_row` returns `etag_baseline` and `staged_summary` as **parsed dicts (or None)**, never raw JSON strings.

- [ ] **Step 1: Find the existing CRUD test pattern.** Run `grep -rn "create_ro_mount\|_ro_mount_row" tests/ | head` and open the file it names (Slice A added CRUD coverage). Copy its fixture style (it fakes the asyncpg pool/conn — reuse the same fake classes/import).

- [ ] **Step 2: Write the failing tests** in `tests/cloud/test_ro_mount_staging_db.py`. Adapt the fixture import to what Step 1 found; the assertions below are the requirements:

```python
"""cloud_ro_mounts staging columns (Slice C migration 0057) — DB accessors.

_ro_mount_row must json.loads JSONB payloads (asyncpg returns raw JSON
strings); update_ro_mount_staging must NULL staged_at when clearing.
"""

import json

from orchestrator.database.postgres import PostgresDB


def test_ro_mount_row_parses_jsonb_strings():
    row = {
        "id": "x", "credentials": None,
        "etag_baseline": json.dumps({"a.txt": "et1"}),
        "staged_summary": json.dumps({"counts": {"added": 1, "modified": 0, "deleted": 0}}),
    }
    d = PostgresDB._ro_mount_row(row)
    assert d["etag_baseline"] == {"a.txt": "et1"}
    assert d["staged_summary"]["counts"]["added"] == 1


def test_ro_mount_row_leaves_none_jsonb_as_none():
    d = PostgresDB._ro_mount_row({"id": "x", "credentials": None,
                                  "etag_baseline": None, "staged_summary": None})
    assert d["etag_baseline"] is None
    assert d["staged_summary"] is None
```

Plus two async accessor tests using Step 1's fake-conn pattern: `update_ro_mount_baseline` executes an UPDATE containing `etag_baseline` and passes `json.dumps(baseline)`; `update_ro_mount_staging(..., staged_summary=None)` executes SQL whose text sets `staged_at = NULL`.

- [ ] **Step 3: Run to verify failure.** `pytest tests/cloud/test_ro_mount_staging_db.py -v` → FAIL (KeyError / AttributeError: no such methods; `_ro_mount_row` returns the raw string).

- [ ] **Step 4: Write the migration** — `orchestrator/database/migrations/app/0057_cloud_ro_mounts_staging.sql` (header convention matches 0052–0055):

```sql
-- migration:     0057_cloud_ro_mounts_staging.sql
-- description:   Protected cloud Slice C staging state on cloud_ro_mounts:
--                persisted etag baseline (conflict gate + manifest
--                classification) and staged-epoch bookkeeping for the
--                S3 staging pipeline (design spec 2026-07-12 §5).
-- depends-on:    0055_datasources_read_only.sql
-- expected:      < 1s
-- locks:         Brief ACCESS EXCLUSIVE on cloud_ro_mounts (ADD COLUMN, no rewrite)
-- transactional: yes
-- ============================================================================

ALTER TABLE cloud_ro_mounts ADD COLUMN IF NOT EXISTS etag_baseline JSONB;
ALTER TABLE cloud_ro_mounts ADD COLUMN IF NOT EXISTS staged_epoch  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cloud_ro_mounts ADD COLUMN IF NOT EXISTS staged_at     TIMESTAMPTZ;
ALTER TABLE cloud_ro_mounts ADD COLUMN IF NOT EXISTS staged_summary JSONB;

COMMENT ON COLUMN cloud_ro_mounts.etag_baseline  IS 'path->etag map (files only) captured at engage, re-captured after each apply';
COMMENT ON COLUMN cloud_ro_mounts.staged_epoch   IS 'monotonic staging epoch: bumped on every successful stage push, apply, and reject';
COMMENT ON COLUMN cloud_ro_mounts.staged_at      IS 'when the current epoch was pushed; NULL when nothing staged';
COMMENT ON COLUMN cloud_ro_mounts.staged_summary IS 'manifest counts + content signature for the current epoch (entry lists live in S3); NULL when nothing staged';
```

- [ ] **Step 5: Implement the accessors** in `orchestrator/database/postgres.py`, directly after `mark_ro_mount_revoked` (:1320):

```python
    async def update_ro_mount_baseline(self, row_id: str, baseline: dict[str, str]) -> bool:
        """Persist the etag baseline (engage-time capture / post-apply re-capture)."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE cloud_ro_mounts SET etag_baseline = $2::jsonb "
                "WHERE id = $1 AND status = 'active'",
                row_id, json.dumps(baseline),
            )
        return result == "UPDATE 1"

    async def update_ro_mount_staging(
        self, row_id: str, *, staged_epoch: int, staged_summary: dict | None
    ) -> bool:
        """Advance the staging epoch. staged_summary=None clears the staged state."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE cloud_ro_mounts SET staged_epoch = $2, "
                "staged_summary = $3::jsonb, "
                "staged_at = CASE WHEN $3::text IS NULL THEN NULL ELSE now() END "
                "WHERE id = $1 AND status = 'active'",
                row_id, staged_epoch,
                json.dumps(staged_summary) if staged_summary is not None else None,
            )
        return result == "UPDATE 1"
```

(Match the surrounding pool-acquire idiom in the file — if sibling methods use a different helper for `execute`, copy it.) Extend `_ro_mount_row` (:1329) after the credentials decrypt line:

```python
        for _jf in ("etag_baseline", "staged_summary"):
            if isinstance(d.get(_jf), str):
                d[_jf] = json.loads(d[_jf])
```

- [ ] **Step 6: Run tests.** `pytest tests/cloud/test_ro_mount_staging_db.py -v` → PASS.

- [ ] **Step 7: Regenerate the schema snapshot.** Run `scripts/schema-snapshot.sh app` then `git diff --stat orchestrator/database/schema_current.sql` (expect the 4 columns + comments).

- [ ] **Step 8: Commit** (path-scoped):

```bash
git add orchestrator/database/migrations/app/0057_cloud_ro_mounts_staging.sql \
        orchestrator/database/postgres.py orchestrator/database/schema_current.sql \
        tests/cloud/test_ro_mount_staging_db.py
git commit -m "feat(cloud): migration 0057 — staging columns + accessors on cloud_ro_mounts"
```

---

### Task 2: Etag-baseline capture at engage + `reader_id` into the client factory

**Files:**
- Modify: `orchestrator/services/cloud/ro_engage.py` (engage_ro_mount at :61; persist block ~:120-130)
- Modify: `orchestrator/main.py` (`_reader_client` inside `_engage_protected_cloud_for_thread` at :19424; factory passed at :19439)
- Test: extend the existing engage tests (find via `grep -rln "engage_ro_mount" tests/` — Slice A added persist-on-ok/refuse coverage; add cases there)

**Interfaces:**
- Consumes: Task 1's `update_ro_mount_baseline`; backend `capture_etag_baseline(handle) -> dict[str, str]` (path→etag, files only — `services/cloud/base.py:229`, NC impl `nextcloud.py:760`).
- Produces: `http_client_factory` contract changes to `factory(credentials: str | None, reader_id: str)`; after a successful engage the `cloud_ro_mounts` row has a non-null `etag_baseline`.

**Fail-closed decision (spec §5/§7):** baseline capture failure is an engage **refusal** — without a baseline, neither manifest classification nor the conflict gate can run, so protected mode must not engage.

- [ ] **Step 1: Write the failing tests** in the file Step 0's grep found. Three additions, following that file's existing fake style (fake backend + fake postgres recorder):

```python
async def test_engage_captures_and_persists_etag_baseline(...):
    # fake backend.capture_etag_baseline returns {"a.txt": "e1"}
    # assert fake_db.update_ro_mount_baseline called with the created row id and that map

async def test_engage_refuses_when_baseline_capture_fails(...):
    # backend.capture_etag_baseline raises CloudBackendError
    # assert RoEngageRefused raised AND revoke_ro_grant called (no dangling grant)

async def test_engage_passes_reader_id_to_client_factory(...):
    # factory = lambda credentials, reader_id: recorder(...)
    # assert recorder saw grant.reader_id (e.g. "srw-reader-u1"), not a re-derived name
```

- [ ] **Step 2: Run to verify failure.** `pytest <that file> -v` → FAIL (factory called with 1 arg; no baseline persist call).

- [ ] **Step 3: Implement in `ro_engage.py`.** (a) Change the factory call at :85 to `client = http_client_factory(grant.credentials, grant.reader_id)` and update the param docstring. (b) After the `create_ro_mount` persist succeeds, capture + persist the baseline, refusing (with grant revoke, matching the existing refusal-cleanup path in this function) on failure:

```python
    try:
        baseline = await backend.capture_etag_baseline(handle)
    except Exception as e:
        await _revoke_quietly(backend, grant, user_key)  # reuse this fn's existing cleanup helper/inline pattern
        raise RoEngageRefused(f"etag baseline capture failed: {e}") from e
    await postgres_db.update_ro_mount_baseline(row_id, baseline)
```

(Adopt whatever revoke-and-refuse idiom the function already uses for probe failure — do not invent a second cleanup path.)

- [ ] **Step 4: Fix the factory in `main.py:19424`** (this is Slice B deferral #4 — the reader name is currently re-derived as `f"srw-reader-{user_id}"`):

```python
    def _reader_client(credentials: str | None, reader_id: str):
        # httpx client authenticated AS THE READER (basic auth), for the probe.
        return httpx.AsyncClient(
            base_url=backend._base_url,
            auth=(reader_id, credentials or ""),
            timeout=30.0,
        )
```

- [ ] **Step 5: Run the engage tests + the cloud suite.** `pytest <engage test file> tests/cloud/ -q` → PASS.

- [ ] **Step 6: Commit:**

```bash
git add orchestrator/services/cloud/ro_engage.py orchestrator/main.py <engage test file>
git commit -m "feat(cloud): persist etag baseline at engage (fail-closed) + pass grant.reader_id into the probe client factory"
```

### Task 3: Manifest derivation (`cloud_staging/manifest.py`)

**Files:**
- Create: `orchestrator/services/cloud_staging/__init__.py`, `orchestrator/services/cloud_staging/manifest.py`
- Test: `tests/cloud_staging/__init__.py` (empty), `tests/cloud_staging/test_manifest.py`

**Interfaces:**
- Consumes: nothing (pure; stdlib `tarfile` only). Classification rules mirror `src/services/cloud_overlay/whiteout.py` (char(0,0) whiteouts, `.wh.` name markers, 3 opaque xattrs, `.wh..wh..opq` sentinel) — the orchestrator image does NOT ship `src/`, so the constants are **re-declared here with a cross-reference comment**, plus the tar/pax spellings.
- Produces:
  - `derive_manifest(tar_path: str, *, baseline: dict[str, str], epoch: int, staged_at: str) -> dict` returning `{"epoch", "staged_at", "counts": {"added","modified","deleted"}, "entries": [{"path","status","size","binary"}, ...], "skipped": [{"path","kind"}, ...]}` (skipped = non-regular members — symlinks etc. — which WebDAV cannot represent; surfaced, never silent) with entries sorted by path, `status ∈ added|modified|deleted`.
  - `select_protected_mount(mount_rows: list[dict]) -> dict | None` in `cloud_staging/__init__.py` — the single definition of "which mount is protected": first row with `backend_id == "nextcloud"` and a truthy `cloud_handle` (extracted from `main.py:19409-19416`; Task 10 and the engage path both use it).

**Classification rules (spec §5):**
1. Only members under `upper/` count (`tar -C /home/agent-host/.overlay upper` produces `upper/...` names; tolerate a `./` prefix). `rel` = path inside the mount.
2. Char device member with devmajor=0, devminor=0 → whiteout of `rel` — unless its basename starts with `.wh.` (engine bookkeeping, skip).
3. Regular-file member whose basename starts with `.wh.` → xattr-format whiteout of `dirname(rel)/basename[4:]`; the exact name `.wh..wh..opq` instead marks `dirname(rel)` opaque. A bare `.wh.` basename raises `ValueError` (mirror whiteout.py).
4. Directory member whose pax headers carry any of `SCHILY.xattr.trusted.overlay.opaque` / `SCHILY.xattr.user.overlay.opaque` / `SCHILY.xattr.user.fuseoverlayfs.opaque` equal to `"y"` → opaque dir. (GNU tar stores xattrs as `SCHILY.xattr.*` pax records; values may arrive `str` or `bytes` — accept both.)
5. Every whiteout target and opaque dir is **expanded against the baseline** to per-file deletes: `deleted` gets baseline paths equal to the target or under `target + "/"` that are NOT shadowed by a staged regular file. A whiteout/opaque with no baseline matches contributes nothing (amendment #2: never-in-lower ⇒ no-op).
6. Regular file member → `modified` if `rel` in baseline else `added`; `binary` = `b"\0" in first 8 KiB`; `size` = member size. A path both staged and whiteout-marked (replace) is present: it appears as added/modified only.

- [ ] **Step 1: Write the failing tests.** Synthetic tars need no root: `tarfile.TarInfo(type=tarfile.CHRTYPE)` writes char devices, `pax_headers` writes xattrs. Test helper + cases:

```python
"""derive_manifest — synthetic-tar cases for every classification rule (spec §5)."""

import io
import tarfile

import pytest

from orchestrator.services.cloud_staging.manifest import derive_manifest


def _build_tar(tmp_path, members):
    """members: list of (name, kind, data, pax) tuples; kind in file|chr|dir."""
    p = tmp_path / "upper.tar"
    with tarfile.open(p, "w", format=tarfile.PAX_FORMAT) as tf:
        for name, kind, data, pax in members:
            ti = tarfile.TarInfo(name=name)
            if pax:
                ti.pax_headers = pax
            if kind == "chr":
                ti.type = tarfile.CHRTYPE
                ti.devmajor = 0
                ti.devminor = 0
                tf.addfile(ti)
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return str(p)


def test_added_vs_modified_by_baseline_membership(tmp_path):
    tar = _build_tar(tmp_path, [
        ("upper/new.txt", "file", b"hello", None),
        ("upper/old.txt", "file", b"world", None),
    ])
    m = derive_manifest(tar, baseline={"old.txt": "e1"}, epoch=3, staged_at="t")
    st = {e["path"]: e["status"] for e in m["entries"]}
    assert st == {"new.txt": "added", "old.txt": "modified"}
    assert m["counts"] == {"added": 1, "modified": 1, "deleted": 0}
    assert m["epoch"] == 3


def test_char_whiteout_expands_to_baseline_files(tmp_path):
    # whiteout of a DIRECTORY deletes every baseline file under it
    tar = _build_tar(tmp_path, [("upper/docs", "chr", b"", None)])
    m = derive_manifest(tar, baseline={"docs/a.txt": "e", "docs/b/c.txt": "e", "keep.txt": "e"},
                        epoch=1, staged_at="t")
    assert {e["path"] for e in m["entries"]} == {"docs/a.txt", "docs/b/c.txt"}
    assert all(e["status"] == "deleted" for e in m["entries"])


def test_whiteout_of_never_in_lower_path_is_noop(tmp_path):
    tar = _build_tar(tmp_path, [("upper/ghost.txt", "chr", b"", None)])
    m = derive_manifest(tar, baseline={"real.txt": "e"}, epoch=1, staged_at="t")
    assert m["entries"] == []


def test_opaque_dir_deletes_unshadowed_baseline_files_only(tmp_path):
    tar = _build_tar(tmp_path, [
        ("upper/proj", "dir", b"", {"SCHILY.xattr.user.fuseoverlayfs.opaque": "y"}),
        ("upper/proj/kept.txt", "file", b"v2", None),
    ])
    m = derive_manifest(tar, baseline={"proj/kept.txt": "e", "proj/gone.txt": "e"},
                        epoch=1, staged_at="t")
    st = {e["path"]: e["status"] for e in m["entries"]}
    assert st == {"proj/kept.txt": "modified", "proj/gone.txt": "deleted"}


def test_opaque_dir_never_in_lower_is_pure_add(tmp_path):
    # fuse-overlayfs marks every merged-created dir opaque (whiteout.py phase-0 note)
    tar = _build_tar(tmp_path, [
        ("upper/newdir", "dir", b"", {"SCHILY.xattr.user.fuseoverlayfs.opaque": "y"}),
        ("upper/newdir/f.txt", "file", b"x", None),
    ])
    m = derive_manifest(tar, baseline={}, epoch=1, staged_at="t")
    assert [(e["path"], e["status"]) for e in m["entries"]] == [("newdir/f.txt", "added")]


def test_wh_name_marker_and_sentinel_and_bookkeeping(tmp_path):
    tar = _build_tar(tmp_path, [
        ("upper/a/.wh.dead.txt", "file", b"", None),        # xattr-format whiteout
        ("upper/b/.wh..wh..opq", "file", b"", None),         # opaque sentinel for b/
        ("upper/.wh..opq", "chr", b"", None),                # engine bookkeeping char dev -> skip
    ])
    m = derive_manifest(tar, baseline={"a/dead.txt": "e", "b/old.txt": "e"}, epoch=1, staged_at="t")
    st = {e["path"]: e["status"] for e in m["entries"]}
    assert st == {"a/dead.txt": "deleted", "b/old.txt": "deleted"}


def test_bare_wh_prefix_raises(tmp_path):
    tar = _build_tar(tmp_path, [("upper/x/.wh.", "file", b"", None)])
    with pytest.raises(ValueError):
        derive_manifest(tar, baseline={}, epoch=1, staged_at="t")


def test_binary_sniff_and_size(tmp_path):
    tar = _build_tar(tmp_path, [
        ("upper/img.png", "file", b"\x89PNG\x00\x1a", None),
        ("upper/note.md", "file", b"plain text", None),
    ])
    m = derive_manifest(tar, baseline={}, epoch=1, staged_at="t")
    by = {e["path"]: e for e in m["entries"]}
    assert by["img.png"]["binary"] is True and by["img.png"]["size"] == 6
    assert by["note.md"]["binary"] is False


def test_select_protected_mount_picks_first_nextcloud_with_handle():
    from orchestrator.services.cloud_staging import select_protected_mount
    rows = [
        {"backend_id": "opencloud", "cloud_handle": "h0"},
        {"backend_id": "nextcloud", "cloud_handle": None},
        {"backend_id": "nextcloud", "cloud_handle": "h1", "mountpoint": "Proj"},
        {"backend_id": "nextcloud", "cloud_handle": "h2"},
    ]
    assert select_protected_mount(rows)["cloud_handle"] == "h1"
    assert select_protected_mount([]) is None
```

- [ ] **Step 2: Run to verify failure.** `pytest tests/cloud_staging/test_manifest.py -v` → FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement.** `orchestrator/services/cloud_staging/__init__.py`:

```python
"""Protected cloud Slice C — staging pipeline (stage → S3 → review → apply)."""

from __future__ import annotations


def select_protected_mount(mount_rows: list[dict]) -> dict | None:
    """The one definition of which of a thread's mounts is the protected one:
    the first Nextcloud mount that has a cloud handle. Mirrors (and replaces)
    the inline pick in main._engage_protected_cloud_for_thread."""
    for row in mount_rows or []:
        if row.get("backend_id") == "nextcloud" and row.get("cloud_handle"):
            return row
    return None
```

`orchestrator/services/cloud_staging/manifest.py` — full implementation:

```python
"""Derive a staged-diff manifest from an upperdir tar (protected cloud Slice C).

The tar is `tar --xattrs -C /home/agent-host/.overlay upper` streamed from the
workspace pod. Classification mirrors src/services/cloud_overlay/whiteout.py
(char(0,0) whiteouts, `.wh.` name markers, opaque dirs via three xattrs or the
`.wh..wh..opq` sentinel) — constants re-declared because the orchestrator image
does not ship src/. Whiteouts/opaque dirs are expanded against the etag
baseline to per-file deletes; targets never in the baseline are no-ops
(design §11.6 amendment #2).
"""

from __future__ import annotations

import posixpath
import tarfile

_UPPER_PREFIX = "upper/"
_WH_PREFIX = ".wh."
_OPAQUE_SENTINEL = ".wh..wh..opq"
_OPAQUE_XATTR_KEYS = (
    "SCHILY.xattr.trusted.overlay.opaque",
    "SCHILY.xattr.user.overlay.opaque",
    "SCHILY.xattr.user.fuseoverlayfs.opaque",
)
_BINARY_SNIFF_BYTES = 8192


def _rel(name: str) -> str | None:
    name = name.lstrip("./")
    if not name.startswith(_UPPER_PREFIX):
        return None
    return name[len(_UPPER_PREFIX):].rstrip("/") or None


def _is_opaque(member: tarfile.TarInfo) -> bool:
    for key in _OPAQUE_XATTR_KEYS:
        val = (member.pax_headers or {}).get(key)
        if val in ("y", b"y"):
            return True
    return False


def derive_manifest(
    tar_path: str, *, baseline: dict[str, str], epoch: int, staged_at: str
) -> dict:
    staged: dict[str, dict] = {}          # rel -> {size, binary}
    whiteout_targets: set[str] = set()    # file-or-dir paths whited out
    opaque_dirs: set[str] = set()

    with tarfile.open(tar_path, "r") as tf:
        for member in tf:
            rel = _rel(member.name)
            if rel is None:
                continue
            base = posixpath.basename(rel)
            if member.ischr():
                if member.devmajor == 0 and member.devminor == 0:
                    if base.startswith(_WH_PREFIX):
                        continue  # engine bookkeeping (e.g. `.wh..opq` node)
                    whiteout_targets.add(rel)
                continue
            if member.isdir():
                if _is_opaque(member):
                    opaque_dirs.add(rel)
                continue
            if not member.isreg():
                continue
            if base == _OPAQUE_SENTINEL:
                parent = posixpath.dirname(rel)
                if parent:
                    opaque_dirs.add(parent)
                continue
            if base.startswith(_WH_PREFIX):
                remainder = base[len(_WH_PREFIX):]
                if not remainder:
                    raise ValueError(f"bare whiteout prefix in upperdir tar: {rel!r}")
                parent = posixpath.dirname(rel)
                whiteout_targets.add(posixpath.join(parent, remainder) if parent else remainder)
                continue
            f = tf.extractfile(member)
            head = f.read(_BINARY_SNIFF_BYTES) if f else b""
            staged[rel] = {"size": member.size, "binary": b"\0" in head}

    deleted: set[str] = set()
    for target in whiteout_targets | opaque_dirs:
        if target in baseline and target not in staged:
            deleted.add(target)
        prefix = target + "/"
        for path in baseline:
            if path.startswith(prefix) and path not in staged:
                deleted.add(path)

    entries = [
        {"path": p, "status": "modified" if p in baseline else "added",
         "size": meta["size"], "binary": meta["binary"]}
        for p, meta in staged.items()
    ] + [{"path": p, "status": "deleted", "size": 0, "binary": False} for p in deleted]
    entries.sort(key=lambda e: e["path"])
    counts = {"added": 0, "modified": 0, "deleted": 0}
    for e in entries:
        counts[e["status"]] += 1
    return {"epoch": epoch, "staged_at": staged_at, "counts": counts, "entries": entries}
```

- [ ] **Step 4: Run tests.** `pytest tests/cloud_staging/test_manifest.py -v` → PASS (all 10).

- [ ] **Step 5: Commit:**

```bash
git add orchestrator/services/cloud_staging/ tests/cloud_staging/
git commit -m "feat(cloud): staged-diff manifest derivation from upperdir tar + select_protected_mount"
```

---

### Task 4: Stage service (`cloud_staging/stage.py`) + SnapshotService blob helpers

**Files:**
- Create: `orchestrator/services/cloud_staging/stage.py`
- Modify: `orchestrator/services/snapshot_service.py` (add two methods near `save_blob`/`get_blob` :255-295)
- Test: `tests/cloud_staging/test_stage.py`

**Interfaces:**
- Consumes: Task 1 accessors; Task 3 `derive_manifest`; `services/ssh_helpers.py::build_agent_ssh_cmd(ssh_host, ssh_port, remote_cmd, *, key_path=None, ...)` (:77); `snapshot_service.save_blob`/`get_blob` (existing, :255/:287).
- Produces (Tasks 5/8/10 rely on):
  - `async def stage_thread_cloud_diff(*, thread_id: str, postgres_db, snapshot_service) -> dict | None` — returns `{"epoch": int, "counts": {...}}` on push, `{"skipped": "<reason>"}` on no-op (`not_protected` / `no_active_mount` / `no_workspace` / `in_flight` / `unchanged` / `empty`), `None` on hard failure (logged, never raises).
  - Key builders `staging_tar_key(thread_id) -> f"cloud-staging/{thread_id}/upper.tar"`, `staging_manifest_key(thread_id) -> f"cloud-staging/{thread_id}/manifest.json"`.
  - Pure command builders (unit-tested as strings): `stage_signature_cmd() -> str`, `stage_tar_cmd() -> str`.
  - `SnapshotService.upload_blob_file(self, key: str, local_path: str) -> bool` and `SnapshotService.delete_blob(self, key: str) -> bool` (boto3 `upload_file` / `delete_object` via `asyncio.to_thread`, guarded by `self._available` like `save_blob`).

**Behavior (spec §5):**
1. Load thread; skip unless `metadata.protected_cloud`. Load mount row via `get_ro_mount_by_thread`; skip unless `status == "active"`.
2. Resolve workspace SSH from thread metadata exactly like `workspace_suspension.py:471-472` does (`ws_ctx = metadata.get("workspace_container") or {}`; host = `ws_ctx.get("pod_ip") or ws_ctx.get("host")`; port via the same fallback chain — **read `workspace_suspension._resolve_ssh_port` and replicate its logic in a local `_resolve_workspace_ssh(metadata) -> tuple[str, int] | None`**, with a comment naming the source). No host → `{"skipped": "no_workspace"}`.
3. Debounce: module-level `_inflight: set[str]`; if thread_id present → `{"skipped": "in_flight"}`; wrap the body in try/finally add/discard.
4. Signature: run `stage_signature_cmd()` over SSH (`asyncio.create_subprocess_exec` on `build_agent_ssh_cmd(...)`, capture stdout, 30s timeout):
   `find /home/agent-host/.overlay/upper -mindepth 1 -printf '%P|%y|%s|%T@\n' 2>/dev/null | sort | sha256sum | cut -d' ' -f1`
   - Empty upperdir hashes a constant: detect emptiness with the same output — a sha256 of empty input (`e3b0c44...`) means empty → delete both blobs, `update_ro_mount_staging(row_id, staged_epoch=row["staged_epoch"] + 1, staged_summary=None)`, return `{"skipped": "empty"}` (only if something was staged before; if `staged_summary` already None, skip the writes).
   - If signature equals `row["staged_summary"]["signature"]` → `{"skipped": "unchanged"}`.
5. Tar: `stage_tar_cmd()` = `tar --xattrs --xattrs-include='*' --acls -C /home/agent-host/.overlay -cf - upper` (PLAIN tar — no zstd; the manifest deriver and diff source read it with `tarfile`). Stream stdout in 1 MiB chunks to a `tempfile.NamedTemporaryFile(suffix=".tar")` (copy the loop shape from `snapshot_service.py:424-454`), hard cap `_STAGE_MAX_BYTES = 9 * 1024**3` (above the 8 GiB upperdir quota) → kill + `None`.
6. `manifest = derive_manifest(tmp.name, baseline=row["etag_baseline"] or {}, epoch=row["staged_epoch"] + 1, staged_at=<UTC ISO now>)`; `manifest["signature"] = signature`.
7. Upload: `upload_blob_file(staging_tar_key(...), tmp.name)` then `save_blob(staging_manifest_key(...), json.dumps(manifest).encode())` — check `save_blob`'s exact signature/param order before calling (it exists at snapshot_service.py:255; adapt if it takes content-type).
8. Persist: `update_ro_mount_staging(row["id"], staged_epoch=manifest["epoch"], staged_summary={"counts": manifest["counts"], "signature": signature})`. Return `{"epoch": ..., "counts": ...}`. Delete the temp file in `finally`.

- [ ] **Step 1: Write the failing tests.** Structure `stage.py` so logic is testable without SSH: put the subprocess calls behind module-level `async def _run_ssh_capture(cmd: list[str], *, timeout: float) -> bytes | None` and `async def _stream_tar_to_file(cmd: list[str], dest_path: str) -> bool`, and monkeypatch those in tests. Cases:

```python
# tests/cloud_staging/test_stage.py — key cases (fake db = MagicMock with AsyncMock
# methods returning the dict rows; fake snapshot_service = MagicMock with AsyncMock
# save_blob/upload_blob_file/delete_blob; monkeypatch _run_ssh_capture/_stream_tar_to_file)

test_command_strings_pinned            # stage_signature_cmd()/stage_tar_cmd() exact text incl. --xattrs and -C .overlay
test_skips_when_not_protected          # thread metadata lacks protected_cloud -> {"skipped": "not_protected"}, no SSH call
test_skips_when_no_active_mount
test_skips_when_no_workspace_host
test_unchanged_signature_skips_upload  # row.staged_summary.signature == fake signature -> no upload, no epoch bump
test_empty_upperdir_clears_staging     # sha of empty input -> delete_blob called for BOTH keys, staging cleared with bumped epoch
test_empty_upperdir_when_nothing_staged_is_pure_noop   # staged_summary already None -> no delete/update calls
test_push_derives_manifest_uploads_and_bumps_epoch
    # build a real tiny tar via Task 3's _build_tar helper pattern; _stream_tar_to_file writes it to dest
    # assert upload_blob_file called with cloud-staging/<tid>/upper.tar; save_blob with manifest json;
    # update_ro_mount_staging(staged_epoch=old+1, staged_summary has counts+signature, NOT entries)
test_inflight_debounce                 # second concurrent call returns {"skipped": "in_flight"}
```

- [ ] **Step 2: Run to verify failure.** `pytest tests/cloud_staging/test_stage.py -v` → FAIL.

- [ ] **Step 3: Implement `stage.py`** per the behavior spec above, and add to `snapshot_service.py`:

```python
    async def upload_blob_file(self, key: str, local_path: str) -> bool:
        """Upload a local file to an arbitrary bucket key (staging tars)."""
        if not self._available:
            return False
        try:
            await asyncio.to_thread(self._s3.upload_file, local_path, self._bucket, key)
            return True
        except Exception as e:
            logger.error(f"S3 upload_blob_file failed for {key}: {e}")
            return False

    async def delete_blob(self, key: str) -> bool:
        if not self._available:
            return False
        try:
            await asyncio.to_thread(self._s3.delete_object, Bucket=self._bucket, Key=key)
            return True
        except Exception as e:
            logger.error(f"S3 delete_blob failed for {key}: {e}")
            return False
```

(Match the file's actual logger/availability-flag names — read the neighbors `save_blob`/`get_blob` first.)

- [ ] **Step 4: Run tests + snapshot-service suite.** `pytest tests/cloud_staging/test_stage.py tests/test_citation_snapshot_blob.py -v` → PASS.

- [ ] **Step 5: Commit:**

```bash
git add orchestrator/services/cloud_staging/stage.py orchestrator/services/snapshot_service.py \
        tests/cloud_staging/test_stage.py
git commit -m "feat(cloud): turn-end stage service — upperdir tar stream to S3 with manifest + signature skip"
```

---

### Task 5: Stage triggers — internal endpoint, agent turn-end ping, teardown hook

**Files:**
- Modify: `orchestrator/main.py` (new endpoint beside the internal agent-thread endpoints ~:17290)
- Modify: `src/api/persistent_app.py` (`_loop_on_turn_complete` at :3431, after the `workspace_sync` block :3470-3475)
- Modify: `orchestrator/services/workspace_suspension.py` (`suspend_thread_workspace`, insert before the `capture_vm_snapshot` call at :502)
- Test: `tests/cloud_staging/test_stage_triggers.py` (endpoint + teardown hook); agent-side test in the module that already covers `_loop_on_turn_complete` (find via `grep -rln "_loop_on_turn_complete" tests/`; if none exists, add `tests/test_persistent_turn_complete_stage_ping.py`)

**Interfaces:**
- Consumes: Task 4 `stage_thread_cloud_diff`; `require_internal` (`orchestrator/security/access.py:1120`); agent-side pattern from `src/tools/communication/messaging.py:197-226` (`ORCHESTRATOR_URL` default `http://localhost:8085`, header `X-Internal-Key` from env `MCP_INTERNAL_KEY`).
- Produces: `POST /api/agents/threads/{thread_id}/cloud-stage` (internal-key only) → `{"scheduled": true}` | `{"skipped": "flag_off"}`; agent helper `_notify_cloud_stage() -> None` (fire-and-forget, never raises).

- [ ] **Step 1: Write the failing endpoint tests** (ExitStack-patch pattern from `tests/test_export_to_cloud_endpoint.py:_patch_endpoint`):

```python
test_cloud_stage_requires_internal_key      # no header -> 401
test_cloud_stage_schedules_task             # patch main.stage_thread_cloud_diff; assert a task
                                            # is created (patch asyncio.create_task or await the
                                            # registry) and response {"scheduled": True}
test_cloud_stage_flag_off_skips             # patch _is_protected_cloud_mode_enabled -> False
test_teardown_hook_stages_before_snapshot   # call suspend_thread_workspace with a protected
                                            # thread dict; assert stage_thread_cloud_diff awaited
                                            # BEFORE capture_vm_snapshot (record call order)
test_teardown_hook_swallows_stage_errors    # stage raises -> snapshot still runs
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Orchestrator endpoint** (place near the other internal `/api/agents/threads/...` endpoints; keep a module-level task registry mirroring `_protected_engage_tasks`):

```python
_cloud_stage_tasks: dict[str, asyncio.Task] = {}


@app.post("/api/agents/threads/{thread_id}/cloud-stage")
async def agent_trigger_cloud_stage(request: Request, thread_id: str) -> dict[str, Any]:
    """Internal — agent turn-end ping. Fire-and-forget staging of the thread's
    protected-cloud upperdir to S3 (Slice C spec §5)."""
    await require_internal(request)
    if not _is_protected_cloud_mode_enabled():
        return {"skipped": "flag_off"}
    from services.cloud_staging.stage import stage_thread_cloud_diff

    async def _run() -> None:
        try:
            await stage_thread_cloud_diff(
                thread_id=thread_id, postgres_db=postgres_db,
                snapshot_service=snapshot_service,
            )
        finally:
            _cloud_stage_tasks.pop(thread_id, None)

    if thread_id not in _cloud_stage_tasks:
        _cloud_stage_tasks[thread_id] = asyncio.create_task(_run())
    return {"scheduled": True}
```

(Verify `snapshot_service` is the module-level singleton name in main.py — grep `from services.snapshot_service import`.)

- [ ] **Step 4: Agent-side ping** in `persistent_app.py` — add the helper near `_loop_on_turn_complete` and call it at the END of that function, after the `workspace_sync` block:

```python
async def _notify_cloud_stage() -> None:
    """Fire-and-forget turn-end staging ping (protected cloud, Slice C).
    Never raises — staging failure must not touch the turn."""
    orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8085")
    headers: dict[str, str] = {}
    internal_key = os.getenv("MCP_INTERNAL_KEY", "")
    if internal_key:
        headers["X-Internal-Key"] = internal_key
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            await client.post(
                f"{orchestrator_url}/api/agents/threads/{_thread_id}/cloud-stage"
            )
    except Exception as e:
        logger.debug(f"cloud-stage ping failed (non-fatal): {e}")
```

Call site (inside `_loop_on_turn_complete`, last lines):

```python
    overlay = getattr(_session, "overlay_mount_manager", None)
    if overlay is not None and overlay.active:
        asyncio.create_task(_notify_cloud_stage())
```

(Verify the session attribute name: `grep -n "overlay_mount_manager" src/api/persistent_session.py` — Slice B created it around :511/:728-731. If the manager lives under a different attr on the session object `persistent_app._session` holds, adapt.)

- [ ] **Step 5: Teardown hook** in `workspace_suspension.py::suspend_thread_workspace`, immediately before the `capture_vm_snapshot` call (:502), using the thread row the function already loaded:

```python
        if (thread.get("metadata") or {}).get("protected_cloud"):
            try:
                from services.cloud_staging.stage import stage_thread_cloud_diff
                await stage_thread_cloud_diff(
                    thread_id=thread_id, postgres_db=self._db,
                    snapshot_service=self._snapshot_service,
                )
            except Exception as e:
                logger.warning(f"teardown cloud-stage failed (non-fatal) for {thread_id}: {e}")
```

(Verify the attribute names for db/snapshot service on `WorkspaceSuspensionService` — read the class `__init__`; also verify the local variable holding the thread dict — it may be named differently.)

- [ ] **Step 6: Run tests.** `pytest tests/cloud_staging/ -v` plus the agent-side test file → PASS.

- [ ] **Step 7: Commit:**

```bash
git add orchestrator/main.py src/api/persistent_app.py orchestrator/services/workspace_suspension.py \
        tests/cloud_staging/test_stage_triggers.py <agent-side test file>
git commit -m "feat(cloud): stage triggers — internal endpoint + agent turn-end ping + teardown hook"
```

### Task 6: `DiffSource` protocol + `GiteaDiffSource` (characterize first, then refactor)

**Files:**
- Create: `orchestrator/services/diff_source.py`
- Modify: `orchestrator/main.py` (`get_job_diff` :14048-14083, `get_job_diff_file` :14086-14151)
- Test: `tests/test_job_diff_endpoints.py` (NEW — characterization; there is **no existing coverage** of these endpoints, confirmed by exploration 2026-07-12)

**Interfaces:**
- Consumes: `services/job_cloud_baseline.py::get_diff_summary(job=, gitea_client=)` (:444, returns `{"baseline_commit", "head_commit", "files": [{"path","status"}]}` or None); `gitea_client.get_file_content(repo_name, path, ref=...) -> str | None` (gitea.py:690, utf-8 only — Mode A is text-only by design).
- Produces (Tasks 7/8/10 rely on these EXACT shapes):

```python
@dataclass(frozen=True)
class DiffEntrySummary:
    path: str
    status: str            # "added" | "modified" | "deleted"
    binary: bool = False

@dataclass(frozen=True)
class DiffSummary:
    files: list[DiffEntrySummary]
    meta: dict[str, Any]   # source-specific: gitea -> baseline_commit/head_commit;
                           # upperdir -> epoch/staged_at/counts

@dataclass(frozen=True)
class DiffFileContent:
    path: str
    status: str
    old_content: str | None
    new_content: str | None
    old_binary: bool = False
    new_binary: bool = False

class GiteaDiffSource:
    def __init__(self, *, job: dict, gitea_client: Any): ...
    async def summary(self) -> DiffSummary | None      # None == no diff available (404)
    async def file(self, path: str) -> DiffFileContent | None
```

**Behavior-preservation contract:** the two job endpoints' response JSON must be byte-identical to today (keys `job_id`, `diff_status`, `baseline_commit`, `head_commit`, `files[{path,status}]`; per-file `job_id`, `path`, `status`, `old_content`, `new_content`) — including the 404/400/503 gate order. `GiteaDiffSource.file` moves the endpoint's inline logic (old from `ref=baseline` for modified/deleted, new from `ref=branch_name or "main"` for modified/added) into the class; binary flags stay False (text-only source).

- [ ] **Step 1: Write CHARACTERIZATION tests against the CURRENT endpoints** (before touching anything). Use the ExitStack pattern from `tests/test_export_to_cloud_endpoint.py` (patch `main.require_job_access` → `(user, job)`, `main.gitea_client` MagicMock with `is_initialized=True`, `AsyncMock` `get_file_content`/`list_tree`/`get_branch_head_sha`) and `httpx.AsyncClient(transport=ASGITransport(app=main.app))` or the file's existing client fixture. Pin:

```python
test_diff_summary_shape          # files list, baseline_commit/head_commit keys, diff_status passthrough
test_diff_summary_404_without_baseline_commit
test_diff_file_modified_reads_old_at_baseline_new_at_branch   # assert get_file_content call args (ref=)
test_diff_file_added_has_none_old
test_diff_file_deleted_has_none_new
test_diff_file_400_outside_projects_prefix
```

- [ ] **Step 2: Run — these must PASS against the unmodified endpoints.** `pytest tests/test_job_diff_endpoints.py -v` → PASS. Commit the characterization net on its own:

```bash
git add tests/test_job_diff_endpoints.py
git commit -m "test(jobs): characterization net for job diff endpoints (pre-DiffSource refactor)"
```

- [ ] **Step 3: Implement `orchestrator/services/diff_source.py`** — the dataclasses above plus:

```python
class GiteaDiffSource:
    """Mode A diff source: Gitea trees at baseline..branch (text-only)."""

    def __init__(self, *, job: dict, gitea_client: Any):
        self._job = job
        self._gitea = gitea_client

    async def summary(self) -> DiffSummary | None:
        from services.job_cloud_baseline import get_diff_summary
        s = await get_diff_summary(job=self._job, gitea_client=self._gitea)
        if s is None:
            return None
        return DiffSummary(
            files=[DiffEntrySummary(path=f["path"], status=f["status"]) for f in s["files"]],
            meta={"baseline_commit": s["baseline_commit"], "head_commit": s["head_commit"]},
        )

    async def file(self, path: str) -> DiffFileContent | None:
        s = await self.summary()
        if s is None:
            return None
        entry = next((f for f in s.files if f.path == path), None)
        if entry is None:
            return None
        repo = self._job.get("repo_name")
        baseline = self._job.get("cloud_diff_baseline_commit")
        branch = self._job.get("branch_name") or "main"
        old = new = None
        if entry.status in ("modified", "deleted"):
            old = await self._gitea.get_file_content(repo, path, ref=baseline)
        if entry.status in ("modified", "added"):
            new = await self._gitea.get_file_content(repo, path, ref=branch)
        return DiffFileContent(path=path, status=entry.status,
                               old_content=old, new_content=new)
```

- [ ] **Step 4: Refactor the two endpoints** to build a `GiteaDiffSource` and serialize from it, preserving every gate (404 no baseline / 503 gitea / 400 prefix / 404 no repo_name) and every response key exactly. The endpoints keep their own HTTPException logic; only the data fetch moves.

- [ ] **Step 5: Run the characterization tests — still green, unchanged.** `pytest tests/test_job_diff_endpoints.py -v` → PASS.

- [ ] **Step 6: Commit:**

```bash
git add orchestrator/services/diff_source.py orchestrator/main.py
git commit -m "refactor(jobs): extract DiffSource seam — GiteaDiffSource behind unchanged job diff endpoints"
```

---

### Task 7: `UpperdirDiffSource`

**Files:**
- Modify: `orchestrator/services/diff_source.py`
- Test: `tests/cloud_staging/test_upperdir_diff_source.py`

**Interfaces:**
- Consumes: Task 4 key builders + `snapshot_service.get_blob(key) -> bytes | None`; the manifest shape from Task 3; backend byte reads — use the SAME method `export_job_to_shared_folder` uses for reading project-folder file bytes (`grep -n "get_project_folder_file_bytes" orchestrator/services/*.py tests/cloud/fake.py` — `FakeMainCloudBackend` implements it at fake.py:~334, signature `get_project_folder_file_bytes(handle, path=...) -> bytes`).
- Produces:

```python
class UpperdirDiffSource:
    def __init__(self, *, thread_id: str, mount_row: dict, backend: Any,
                 handle: Any, snapshot_service: Any): ...
    async def summary(self) -> DiffSummary | None       # from manifest.json; None if blob missing
    async def file(self, path: str) -> DiffFileContent | None
    async def raw_new_bytes(self, path: str) -> bytes | None   # Task 10's apply engine uses this
```

**Behavior:**
- `summary()`: `get_blob(staging_manifest_key(thread_id))` → parse → `DiffSummary(files=[DiffEntrySummary(path, status, binary)], meta={"epoch", "staged_at", "counts"})`. Missing blob or `mount_row["staged_summary"] is None` → None.
- Tar access: download `upper.tar` bytes once per instance (`get_blob`), hold `tarfile.open(fileobj=io.BytesIO(...))` lazily; member name for `path` is `upper/{path}`.
- `file(path)`: status from the manifest. New side (added/modified): tar member bytes; utf-8 decode → `new_content`; `UnicodeDecodeError` or manifest `binary` flag → `new_content=None, new_binary=True`. Old side (modified/deleted): `backend.get_project_folder_file_bytes(handle, path=path)`; decode the same way into `old_content`/`old_binary`; backend errors → `old_content=None` (the review UI shows "unavailable"; apply doesn't use old bytes). Added → old None; deleted → new None.
- `raw_new_bytes(path)`: undecoded tar member bytes (apply is byte-true).

- [ ] **Step 1: Write the failing tests** — build a real tar (Task 3 helper pattern) + manifest dict, fake `snapshot_service` (MagicMock, `get_blob` AsyncMock returning tar/manifest bytes by key), `FakeMainCloudBackend` seeded via `seed_project_file` for old-side reads:

```python
test_summary_maps_manifest_entries_and_meta
test_summary_none_when_no_manifest_blob
test_file_modified_old_from_cloud_new_from_tar
test_file_added_old_is_none
test_file_deleted_new_is_none_old_from_cloud
test_file_binary_member_sets_flag_and_none_content     # tar member with b"\0"
test_file_old_side_binary_cloud_bytes_flagged
test_raw_new_bytes_returns_exact_member_bytes
```

- [ ] **Step 2: Run to verify failure.** → FAIL (no class).

- [ ] **Step 3: Implement** in `diff_source.py` per the behavior block (blob reads and `tarfile` parsing wrapped in `asyncio.to_thread` where they touch bytes >1 MiB is unnecessary at these sizes — keep it simple and synchronous inside the async methods, matching `get_blob`'s own to_thread usage).

- [ ] **Step 4: Run tests.** → PASS.

- [ ] **Step 5: Commit:**

```bash
git add orchestrator/services/diff_source.py tests/cloud_staging/test_upperdir_diff_source.py
git commit -m "feat(cloud): UpperdirDiffSource — staged-diff review reads from S3 epoch + live cloud old-side"
```

---

### Task 8: Thread cloud-diff read endpoints (summary / per-file / restage)

**Files:**
- Modify: `orchestrator/main.py` (new endpoints beside the owner-facing thread endpoints, e.g. after :19653's neighborhood)
- Test: `tests/cloud_staging/test_thread_cloud_diff_endpoints.py`

**Interfaces:**
- Consumes: `require_thread_owner(request, postgres_db, thread_id) -> (user, thread)` (`security/access.py:528` — admin bypass + `user_id` equality); Task 7 `UpperdirDiffSource`; Task 3 `select_protected_mount`; `postgres_db.list_thread_mounts(thread_id)` (used at main.py:18714); `main_cloud_router.for_backend("nextcloud")`; `ProjectFolderHandle.from_db(...)` (pattern at `job_cloud_baseline.py`'s `detect_external_mods`).
- Produces (Cockpit Task 14 consumes these EXACT shapes):

```
GET  /api/agents/threads/{thread_id}/cloud-diff
  200 -> {"thread_id", "epoch": int, "staged_at": str|null,
          "counts": {"added","modified","deleted"},
          "protected_mount": str|null,          # mountpoint/display name from the mount row
          "files": [{"path","status","binary"}]}
  200 with epoch=0, files=[], counts all 0 when nothing staged
  404 when the thread is not protected (metadata.protected_cloud falsy) or flag off
GET  /api/agents/threads/{thread_id}/cloud-diff/{file_path:path}
  200 -> {"thread_id","path","status","old_content","new_content","old_binary","new_binary"}
  404 when path not in the staged diff / nothing staged
POST /api/agents/threads/{thread_id}/cloud-diff/restage
  -> schedules stage_thread_cloud_diff like Task 5's internal endpoint (owner-triggered
     refresh); 409 {"code":"no_workspace"} when the thread has no workspace host
```

**Shared resolver** — one private helper both GETs use:

```python
async def _thread_cloud_diff_source(thread_id: str, thread: dict):
    """(mount_row, UpperdirDiffSource|None, protected_mount_name|None) for a protected thread.

    Deliberately does NOT require row.status == "active": revoked-but-staged
    rows (ended threads, grant already reconciled away) stay reviewable —
    spec §11. Only restage/apply-side workspace steps need a live pod.
    """
    row = await postgres_db.get_ro_mount_by_thread(thread_id)
    if not row:
        return None, None, None
    mount_rows = await postgres_db.list_thread_mounts(thread_id)
    sel = select_protected_mount(mount_rows)
    backend = main_cloud_router.for_backend(row["backend"])
    handle = ProjectFolderHandle.from_db(str(sel["cloud_handle"]), backend=row["backend"]) if sel else None
    src = UpperdirDiffSource(thread_id=thread_id, mount_row=row, backend=backend,
                             handle=handle, snapshot_service=snapshot_service)
    name = (sel or {}).get("mountpoint") or (sel or {}).get("workspace_name")
    return row, src, name
```

(Verify the mount-row column names `backend_id`/`cloud_handle`/`mountpoint` against `list_thread_mounts`' SELECT before coding — `grep -n "list_thread_mounts" orchestrator/database/postgres.py` and read the row shape; adjust `select_protected_mount`'s keys in Task 3 if they differ — that helper + its test are the single place to fix.)

**Note on ended threads (spec §11):** review works after pod death AND after the reconciler revokes the grant — the resolver above must not require `status == "active"` for reads; only `restage` needs a live workspace.

- [ ] **Step 1: Write the failing endpoint tests** (ExitStack pattern; patch `main.require_thread_owner` to return an owner + a thread whose `metadata={"protected_cloud": True}`):

```python
test_summary_returns_counts_epoch_and_files          # fake S3 manifest via patched snapshot_service
test_summary_empty_when_nothing_staged               # staged_summary None -> epoch 0, files []
test_summary_404_when_thread_not_protected
test_summary_works_on_revoked_mount_row              # ended thread: status='revoked', staged_summary set
test_file_returns_old_new_content
test_file_404_for_unknown_path
test_restage_schedules_stage
test_restage_409_without_workspace
test_owner_auth_denied_for_other_user                # require_thread_owner raising 403 propagates
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** the three endpoints + the resolver in `main.py`. Every endpoint starts `user, thread = await require_thread_owner(request, postgres_db, thread_id)` then gates `if not (thread.get("metadata") or {}).get("protected_cloud") or not _is_protected_cloud_mode_enabled(): raise HTTPException(404, ...)`.

- [ ] **Step 4: Run tests.** → PASS. Also re-run `pytest tests/cloud_staging/ -q`.

- [ ] **Step 5: Commit:**

```bash
git add orchestrator/main.py tests/cloud_staging/test_thread_cloud_diff_endpoints.py
git commit -m "feat(cloud): thread cloud-diff endpoints — summary, per-file, restage (owner-gated)"
```

---

### Task 9: Agent overlay-reset endpoint + `OverlayMountManager.reset_upper`

**Files:**
- Modify: `src/services/cloud_overlay/overlay_mount.py` (new method + scripts, near `heal` :152)
- Modify: `src/api/persistent_session.py` (session method `reset_cloud_overlay()`), `src/api/persistent_app.py` (route)
- Test: `tests/cloud_overlay/test_overlay_reset.py` (FakeRemoteBackend from `tests/cloud_overlay/test_overlay_mount.py:14-35` — copy the class, module-private)

**Interfaces:**
- Consumes: `OverlayMountManager._run(script_name, script_text, timeout=, require_ok=)` (:293-304), `_mount_body_only_script()` (exists — used by `heal` :159), `RcloneMountManager.refresh_vfs(mount_id=None, *, recursive=True)` (`cloud_mount/__init__.py:222`).
- Produces:
  - `OverlayMountManager.reset_upper(self, refresh_lower: Callable[[], None]) -> None` — post-apply/reject: unmount overlay (plain `-u`, fall back to `-uz`), **wipe `upper` AND `work`, recreate both fresh** (fresh-workdir-per-epoch — Slice B deferral #2 lands here), `refresh_lower()`, remount.
  - Agent route `POST /cloud-overlay/reset` → `{"ok": true}` | 404 when no active overlay — Task 10's orchestrator apply calls this.
  - `PersistentSession.reset_cloud_overlay(self) -> None` — wraps `overlay.reset_upper(refresh_lower=lambda: self.<rclone manager attr>.refresh_vfs())` (verify the session's rclone-manager attribute name next to `overlay_mount_manager` in persistent_session.py:~511/728).

- [ ] **Step 1: Write the failing tests** (script-text assertions, the established pattern):

```python
test_reset_upper_script_order_and_content
    # calls: overlay_reset_unmount.sh then overlay_wipe_upper.sh then (refresh_lower fires)
    # then overlay_remount.sh; wipe script contains: rm -rf of BOTH upper and work contents,
    # mkdir -p of both, and NO rm of the merged/lower paths
test_reset_upper_unmount_plain_u_with_uz_fallback     # script text: fusermount3 -u ... || fusermount3 -uz
test_reset_upper_refresh_lower_called_between_wipe_and_remount   # record ordering via a list
test_reset_upper_raises_on_remount_failure            # FakeRemoteBackend returns __SRW_OVERLAY_FAILED__
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** in `overlay_mount.py`:

```python
    def reset_upper(self, refresh_lower: Callable[[], None]) -> None:
        """Discard the staged upperdir after an apply/reject and remount with a
        FRESH workdir (a workdir must never be reused across overlay instances
        — design §11.2). Plain unmount first; lazy as fallback."""
        self._run("overlay_reset_unmount.sh", self._reset_unmount_script(),
                  timeout=60, require_ok=False)
        self._run("overlay_wipe_upper.sh", self._wipe_upper_script(), timeout=60)
        refresh_lower()
        self._run("overlay_remount.sh", self._mount_body_only_script(), timeout=120)

    def _reset_unmount_script(self) -> str:
        return (
            "#!/bin/bash\n"
            f"fusermount3 -u {self.merged} 2>/dev/null || fusermount3 -uz {self.merged} 2>/dev/null || true\n"
            f"echo {_OVERLAY_OK}\n"
        )

    def _wipe_upper_script(self) -> str:
        return (
            "#!/bin/bash\nset -e\n"
            f"rm -rf {self.upper} {self.work}\n"
            f"mkdir -p {self.upper} {self.work}\n"
            f"echo {_OVERLAY_OK}\n"
        )
```

(Match the real quoting/echo style of the neighboring script builders — read `_heal_unmount_script`/`_probe_script` first and mirror them, including any `shlex.quote` use.)

- [ ] **Step 4: Session method + route.** `persistent_session.py`:

```python
    def reset_cloud_overlay(self) -> None:
        """Post-apply/reject reset, called by the orchestrator via /cloud-overlay/reset."""
        overlay = self.overlay_mount_manager
        if overlay is None or not overlay.active:
            raise RuntimeError("no active cloud overlay")
        overlay.reset_upper(refresh_lower=lambda: self.<rclone_attr>.refresh_vfs())
```

`persistent_app.py` (near the other session-control routes; same no-auth in-cluster posture as its neighbors — verify by reading two adjacent routes):

```python
@app.post("/cloud-overlay/reset")
async def cloud_overlay_reset() -> dict:
    if _session is None or getattr(_session, "overlay_mount_manager", None) is None:
        raise HTTPException(status_code=404, detail="no cloud overlay")
    await asyncio.to_thread(_session.reset_cloud_overlay)
    return {"ok": True}
```

- [ ] **Step 5: Run tests** + the existing overlay suite: `pytest tests/cloud_overlay/ -v` → PASS.

- [ ] **Step 6: Commit:**

```bash
git add src/services/cloud_overlay/overlay_mount.py src/api/persistent_session.py \
        src/api/persistent_app.py tests/cloud_overlay/test_overlay_reset.py
git commit -m "feat(agent): overlay reset endpoint — wipe upper + fresh workdir per epoch, lower refresh, remount"
```

### Task 10: Apply / reject engine + endpoints

**Files:**
- Create: `orchestrator/services/cloud_staging/apply.py`
- Modify: `orchestrator/services/job_cloud_baseline.py` (extract `detect_external_mods` core, :495-556)
- Modify: `orchestrator/main.py` (two endpoints; agent-app URL resolution)
- Test: `tests/cloud_staging/test_apply.py`, `tests/cloud_staging/test_apply_endpoints.py`; existing `detect_external_mods` behavior covered by Task 6's characterization net stays green

**Interfaces:**
- Consumes: Task 7 `UpperdirDiffSource.raw_new_bytes` + manifest summary; Task 8's `_thread_cloud_diff_source` resolver; Task 9's agent route; Task 1 `update_ro_mount_baseline`/`update_ro_mount_staging`; Task 4 key builders + `snapshot_service.delete_blob`; backend `put_project_folder_file_bytes(handle, path=, data=)` / `delete_project_folder_file(handle, path=, if_exists=True)` / `capture_etag_baseline(handle)` (all on `FakeMainCloudBackend` too).
- Produces:

```python
# job_cloud_baseline.py — extracted core (job wrapper keeps its exact signature + behavior)
async def detect_external_mods_against_baseline(
    *, baseline_entries: dict[str, str], backend: Any, handle: Any,
    scope_paths: set[str] | None = None,
) -> list[dict[str, str]]:
    """Same divergence kinds as today (etag_mismatch / missing_at_cloud /
    unexpected_at_cloud). scope_paths, when given, restricts BOTH directions
    to paths in the set (thread applies check only touched paths — spec §7;
    the job wrapper passes None to preserve its whole-tree behavior)."""

# cloud_staging/apply.py
async def apply_staged_diff(*, thread_id: str, epoch: int, postgres_db, main_cloud_router,
                            snapshot_service, reset_agent_overlay) -> dict
async def reject_staged_diff(*, thread_id: str, epoch: int, postgres_db,
                             snapshot_service, reset_agent_overlay) -> dict
class StagedApplyError(Exception):      # .code + .detail dict -> endpoint maps to HTTP
    def __init__(self, status_code: int, detail: dict): ...
```

`reset_agent_overlay` is `Callable[[], Awaitable[bool]]` — the orchestrator-side closure that POSTs the agent's `/cloud-overlay/reset` (below); returns False on any failure (dead pod is NORMAL, never fatal).

**Apply flow (spec §7 — implement in this order):**
1. Load mount row (`get_ro_mount_by_thread`); `staged_summary is None` → `StagedApplyError(409, {"code": "nothing_staged"})`; `epoch != row["staged_epoch"]` → `StagedApplyError(409, {"code": "epoch_stale", "staged_epoch": row["staged_epoch"]})`.
2. Resolve backend/handle via `select_protected_mount(list_thread_mounts(...))`; missing → `StagedApplyError(409, {"code": "no_protected_mount"})`.
3. Build `UpperdirDiffSource`; `summary()` None → `StagedApplyError(410, {"code": "staging_missing"})` (DB says staged but S3 blobs gone).
4. Conflict gate: `detect_external_mods_against_baseline(baseline_entries=row["etag_baseline"] or {}, backend=..., handle=..., scope_paths={f.path for f in summary.files})`; non-empty → `StagedApplyError(409, {"code": "external_modifications_detected", "message": "Cloud folder was modified externally since staging. Resolve manually before applying.", "diverged": diverged})` — mirrors the job accept contract (main.py:14237-14248), no force flag.
5. **Deletes first** (whiteout-before-create): for entries `status == "deleted"` sorted by path DESCENDING (children before parents — harmless for files, correct if a future backend deletes dirs): `backend.delete_project_folder_file(handle, path=..., if_exists=True)`. Then adds/mods: `raw_new_bytes(path)` → `backend.put_project_folder_file_bytes(handle, path=..., data=...)`. Sequential, fail-soft: collect `errors: list[str]`, count `applied`/`deleted` (same result shape as `apply_diff_to_cloud`, job_cloud_baseline.py:557).
6. `errors` non-empty → return `{"applied", "deleted", "errors"}` WITHOUT touching staging state (endpoint maps to 502 `partial_write_failure`; retry is safe — PUT overwrite / DELETE if_exists are idempotent).
7. Full success: (a) `overlay_reset = await reset_agent_overlay()` (False on dead pod — proceed); (b) `new_baseline = await backend.capture_etag_baseline(handle)` → `update_ro_mount_baseline(row["id"], new_baseline)`; (c) `delete_blob` both keys; (d) `update_ro_mount_staging(row["id"], staged_epoch=row["staged_epoch"] + 1, staged_summary=None)`; (e) return `{"applied", "deleted", "errors": [], "epoch": row["staged_epoch"] + 1, "overlay_reset": overlay_reset}`.

**Reject flow:** steps 1 (same pins) → `reset_agent_overlay()` best-effort → delete blobs → clear staging (epoch+1, summary None) → `{"rejected": True, "epoch": ..., "overlay_reset": ...}`. No baseline re-capture (cloud untouched).

**Known v1 limitation (record in the module docstring):** apply/reject on a dead pod cannot clear the upperdir inside the last workspace snapshot; a later resume restores it and the next stage push re-stages already-applied (or rejected) content against the fresh baseline — modified-with-identical-content entries; the user re-rejects. Accepted for v1 (spec §7).

- [ ] **Step 1: Write the failing engine tests** (`FakeMainCloudBackend` + fake snapshot_service + MagicMock db; a real tiny tar for the source):

```python
test_apply_epoch_stale_409
test_apply_nothing_staged_409
test_apply_staging_missing_410                     # summary None
test_apply_conflict_blocks_hard                    # diverged -> code external_modifications_detected, no writes happened
test_apply_scope_paths_limits_conflict_check       # external change on an UNTOUCHED path does NOT block
test_apply_deletes_before_creates                  # FakeMainCloudBackend op log order
test_apply_partial_failure_keeps_staging           # backend put raises for one path (fake fault injection);
                                                   # errors returned; update_ro_mount_staging NOT called; blobs NOT deleted
test_apply_success_full_sequence                   # order: reset -> capture_etag_baseline -> update baseline
                                                   # -> delete both blobs -> clear staging (epoch+1)
test_apply_success_with_dead_pod                   # reset_agent_overlay returns False -> still succeeds, overlay_reset False
test_reject_clears_without_writes                  # zero backend put/delete ops; blobs deleted; epoch+1
test_detect_against_baseline_scope_and_kinds       # direct unit: all 3 kinds + scoping both directions
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Extract the conflict-gate core.** In `job_cloud_baseline.py`, move the body of `detect_external_mods` (:516-556, from `live_entries = await backend.list_project_folder(handle)` through the diverged loops) into `detect_external_mods_against_baseline` with the signature above; add scoping:

```python
    live_map = {e.path: e.etag for e in live_entries if not e.is_dir}
    if scope_paths is not None:
        live_map = {p: t for p, t in live_map.items() if p in scope_paths}
        baseline_entries = {p: t for p, t in baseline_entries.items() if p in scope_paths}
```

The existing `detect_external_mods(*, job, project, main_cloud_router)` keeps its signature and its early-return guards, then delegates with `scope_paths=None`. (Task 6's characterization tests + this task's unit test are the net.)

- [ ] **Step 4: Implement `apply.py`** per the flow blocks. Then the endpoints in `main.py`:

```python
@app.post("/api/agents/threads/{thread_id}/cloud-diff/apply")
async def apply_thread_cloud_diff(request: Request, thread_id: str, body: dict = Body(...)):
    user, thread = await require_thread_owner(request, postgres_db, thread_id)
    _require_protected(thread)                       # shared 404 gate from Task 8
    epoch = int(body.get("epoch", -1))
    try:
        result = await apply_staged_diff(
            thread_id=thread_id, epoch=epoch, postgres_db=postgres_db,
            main_cloud_router=main_cloud_router, snapshot_service=snapshot_service,
            reset_agent_overlay=lambda: _reset_thread_overlay(thread_id, thread),
        )
    except StagedApplyError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    if result.get("errors"):
        raise HTTPException(status_code=502, detail={
            "code": "partial_write_failure", **result})
    return {"thread_id": thread_id, **result}
```

(reject endpoint mirrors it with `reject_staged_diff`.) `_reset_thread_overlay(thread_id, thread) -> bool` POSTs the agent app's `/cloud-overlay/reset`: resolve the agent base URL **the same way the existing persistent-thread proxy does** — find it with `grep -n "persistent/threads" orchestrator/main.py | grep -i "proxy\|stream"` and reuse its host/port resolution helper verbatim; 10s timeout; any exception → `logger.warning` + `False`.

- [ ] **Step 5: Endpoint tests** (`test_apply_endpoints.py`, ExitStack pattern): epoch body → engine call args; `StagedApplyError` → HTTP status/detail passthrough; partial → 502; owner gate.

- [ ] **Step 6: Run everything touched.** `pytest tests/cloud_staging/ tests/test_job_diff_endpoints.py -q` → PASS.

- [ ] **Step 7: Commit:**

```bash
git add orchestrator/services/cloud_staging/apply.py orchestrator/services/job_cloud_baseline.py \
        orchestrator/main.py tests/cloud_staging/test_apply.py tests/cloud_staging/test_apply_endpoints.py
git commit -m "feat(cloud): whole-diff epoch-pinned apply/reject — conflict gate, deletes-first, baseline re-capture"
```

---

### Task 11: ENOTCONN monitor loop + `RcloneMountManager.restart_mount` + heal fresh-workdir

**Files:**
- Modify: `src/services/cloud_mount/__init__.py` (new method; generators `_mount_script` :509, `_unmount_script` :734)
- Modify: `src/services/cloud_overlay/overlay_mount.py` (`_mount_body_only_script` — fresh workdir on heal/remount)
- Modify: `src/api/persistent_session.py` (monitor task startup/shutdown beside the overlay creation ~:511)
- Test: `tests/cloud_mount/test_restart_mount.py`, extend `tests/cloud_overlay/test_overlay_mount.py`, `tests/test_persistent_overlay_monitor.py`

**Interfaces:**
- Consumes: `OverlayMountManager.health_check() -> bool` (:102) and `heal(remount_lower)` (:152) — both exist, zero production callers today (Slice B deferral #1); `RcloneMountState` entries the manager already tracks (read `_start_all_sync` :431 to learn where the original mount dicts + states live — the manager must retain what `restart_mount` needs; if the mount dict isn't retained, store it on the state when first mounted).
- Produces:
  - `RcloneMountManager.restart_mount(self, mount_id: str) -> None` — runs the existing `_unmount_script(state)` then `_mount_script(mount, state)` for that one mount, re-using the retained mount dict; raises on remount failure.
  - `PersistentSession._cloud_overlay_monitor_loop()` — every 60s, when the overlay is active: `health_check()`; on False, log + `heal(remount_lower=lambda: rclone_mgr.restart_mount(<protected mount_id>))`. Started/cancelled with the session's other background tasks.
  - Fresh-workdir on heal: `_mount_body_only_script` prepends `rm -rf {work} && mkdir -p {work}` before the fuse-overlayfs mount line (NEVER touches `{upper}` — staged data survives heals).

- [ ] **Step 1: Failing tests.**

```python
# test_restart_mount.py (FakeRemoteBackend copy)
test_restart_mount_runs_unmount_then_mount_scripts_for_that_mount_only
test_restart_mount_unknown_mount_id_raises
# test_overlay_mount.py addition
test_mount_body_script_freshens_workdir_not_upper    # script contains rm -rf <work> + mkdir, and NO rm of <upper>
# test_persistent_overlay_monitor.py
test_monitor_heals_on_dead_probe        # health_check False once -> heal called with a callable that
                                        # invokes restart_mount(mount_id); use tiny sleep + task cancel
test_monitor_noop_when_healthy
test_monitor_survives_heal_exception    # heal raises -> loop continues (no crash)
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** the three pieces. Monitor loop skeleton for `persistent_session.py` (adopt the session's existing background-task creation/cancel idiom — find where Slice B started overlay-adjacent tasks or where `_token_refresh_loop` peers are cancelled):

```python
    async def _cloud_overlay_monitor_loop(self) -> None:
        """ENOTCONN watchdog for the protected overlay (design §11.6 #3)."""
        while True:
            await asyncio.sleep(60)
            overlay = self.overlay_mount_manager
            if overlay is None or not overlay.active:
                continue
            try:
                healthy = await asyncio.to_thread(overlay.health_check)
                if healthy:
                    continue
                logger.warning("cloud overlay unhealthy (ENOTCONN) — healing")
                await asyncio.to_thread(
                    overlay.heal,
                    lambda: self.<rclone_attr>.restart_mount(self._protected_mount_id),
                )
            except Exception as e:
                logger.error(f"overlay heal failed (will retry next probe): {e}")
```

`self._protected_mount_id` = the `mount_id` of the `protected_lower` mount (`f"protected-{thread_id}"` per `_build_protected_cloud_mount`, main.py:19473 — but read it from the mount payload the session received, do not re-derive the format).

- [ ] **Step 4: Run tests** + full agent-side suites: `pytest tests/cloud_mount/ tests/cloud_overlay/ -q` → PASS.

- [ ] **Step 5: Commit:**

```bash
git add src/services/cloud_mount/__init__.py src/services/cloud_overlay/overlay_mount.py \
        src/api/persistent_session.py tests/cloud_mount/test_restart_mount.py \
        tests/cloud_overlay/test_overlay_mount.py tests/test_persistent_overlay_monitor.py
git commit -m "feat(agent): ENOTCONN monitor wired to heal + RcloneMountManager.restart_mount + fresh workdir on heal"
```

---

### Task 12: Shell upperdir-guard write-only alignment

**Files:**
- Modify: `src/services/cloud_mount/guardrails.py`, `src/tools/shell/shell_tools.py` (`_cloud_upperdir_guard_decision` :142)
- Test: extend the existing guardrails/shell-guard test modules (find via `grep -rln "detect_cloud_delete_risk\|_cloud_upperdir_guard" tests/`)

**Interfaces:**
- Consumes: `_cloud_upperdir_guard_decision` (shell_tools.py:142 → `overlay.quota_guard_message()` :152). Note: `write_file`/`edit_file` (`src/tools/workspace/files.py:945/:1031`) are ALREADY write-only — this task touches only the shell guard. The rclone VFS **cache** guard on `read_file` (files.py:750-752) is a different guard with a different rationale (disk protection) — leave it alone.
- Produces: `guardrails.command_may_write_cloud(command: str) -> bool` — conservative write-indicator detection; the shell upperdir guard only fires when it returns True.

- [ ] **Step 1: Failing tests:**

```python
test_read_only_commands_pass_at_quota      # "cat workspace/cloud/a.txt", "ls -la workspace/cloud",
                                           # "grep -r foo workspace/cloud", "du -sh workspace/cloud"
                                           # -> guard returns None even when over quota
test_write_commands_blocked_at_quota       # "echo hi > workspace/cloud/x", "tee workspace/cloud/x",
                                           # "rm workspace/cloud/x", "cp a workspace/cloud/", "mv a workspace/cloud/",
                                           # "touch workspace/cloud/x", "mkdir workspace/cloud/d",
                                           # "sed -i s/a/b/ workspace/cloud/x", "rsync a workspace/cloud/",
                                           # "dd of=workspace/cloud/x", "truncate -s0 workspace/cloud/x"
test_pipeline_with_redirect_detected       # "sort a | uniq >> workspace/cloud/out"
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** in `guardrails.py`:

```python
_WRITE_INDICATORS = re.compile(
    r"(?:>>?|\btee\b|\brm\b|\bmv\b|\bcp\b|\btouch\b|\bmkdir\b|\brmdir\b"
    r"|\brsync\b|\bdd\b|\btruncate\b|\bln\b|\bunzip\b|\btar\b|\bsed\s+(?:-\S*\s+)*-i)"
)


def command_may_write_cloud(command: str) -> bool:
    """Conservative write-indicator check for the at-quota shell guard.

    Reads never copy-up into the overlay upperdir, so a full-quota upperdir
    must not block them (Slice B deferral: read/write asymmetry). False
    negatives are tolerable — a missed write fails at the FS layer and the
    guard catches the next command.
    """
    return bool(_WRITE_INDICATORS.search(command))
```

In `shell_tools.py::_cloud_upperdir_guard_decision`, before calling `overlay.quota_guard_message()`: `if not command_may_write_cloud(command): return None`.

- [ ] **Step 4: Run the guard test modules.** → PASS.

- [ ] **Step 5: Commit:**

```bash
git add src/services/cloud_mount/guardrails.py src/tools/shell/shell_tools.py <guard test files>
git commit -m "fix(agent): at-quota shell guard only blocks write-indicating commands (reads never copy-up)"
```

### Task 13: Feature gate + projects payload + session-create checkbox

**Files:**
- Modify: `orchestrator/main.py` (`GET /api/users/me/capabilities` :26687; projects list serializer — locate via `grep -n '"/api/projects"' orchestrator/main.py`)
- Modify: `cockpit/src/app/core/services/capabilities.service.ts`, `cockpit/src/app/core/models/api.model.ts` (Project :859-877), `cockpit/src/app/views/session-create/session-create.component.ts`
- Test: `tests/cloud_staging/test_capabilities_features.py` (orchestrator); `cockpit/src/app/views/session-create/session-create.component.spec.ts` (NEW — pure-helper vitest)

**Interfaces:**
- Consumes: `_is_protected_cloud_mode_enabled()` (main.py:1226); `CapabilitiesService` (fetches `getMyCapabilities()` once, exposes signals); session-create's local `Project` interface + untyped create body (`body['protected_cloud']`), which flows via `chat-page` → `PersistentChatService.createAndConnect` → `POST /persistent/threads` → `ThreadCreateRequest.protected_cloud` (:18332, already exists).
- Produces:
  - Capabilities payload gains `"features": {"protected_cloud": bool}` in BOTH branches (admin + non-admin) of the endpoint.
  - Projects list items gain `main_cloud_backend` (string|null) if not already serialized — check first; the DB column exists (`projects.main_cloud_backend`, read by `job_cloud_baseline`).
  - Cockpit: `CapabilitiesService.protectedCloudAvailable: Signal<boolean>`; `Project.main_cloud_backend?: string | null` on both the shared model and session-create's local interface.
  - Exported pure helper (the vitest target):

```typescript
export function protectedCloudToggleVisible(
  featureOn: boolean,
  selected: Array<{ is_default?: boolean; main_cloud_backend?: string | null }>,
): boolean {
  // Visible only when the deployment flag is on AND at least one selected
  // project is a non-default Nextcloud project (spec §2/§4: default projects
  // excluded in v1; Nextcloud-only per design §9.2).
  return featureOn && selected.some(
    (p) => !p.is_default && p.main_cloud_backend === 'nextcloud',
  );
}
```

- [ ] **Step 1: Orchestrator failing test:** capabilities response contains `features.protected_cloud` mirroring the flag (patch `_is_protected_cloud_mode_enabled` True/False; ExitStack pattern), for admin and non-admin users. Run → FAIL.

- [ ] **Step 2: Implement orchestrator side.** Add to both return branches of the capabilities endpoint: `"features": {"protected_cloud": _is_protected_cloud_mode_enabled()}`. Check the projects list payload: `grep -n "main_cloud_backend" orchestrator/main.py` — if the list/detail serializers don't include it, add `"main_cloud_backend": p.get("main_cloud_backend")` to the project dict builders (find them via the projects GET endpoints). Run test → PASS.

- [ ] **Step 3: Cockpit failing vitest** (`session-create.component.spec.ts`, TestBed-free like `job-diff-review.component.spec.ts`):

```typescript
import { describe, expect, it } from 'vitest';
import { protectedCloudToggleVisible } from './session-create.component';

describe('protectedCloudToggleVisible', () => {
  it('hidden when feature off', () => {
    expect(protectedCloudToggleVisible(false, [{ main_cloud_backend: 'nextcloud' }])).toBe(false);
  });
  it('hidden when only default projects selected', () => {
    expect(protectedCloudToggleVisible(true, [{ is_default: true, main_cloud_backend: 'nextcloud' }])).toBe(false);
  });
  it('hidden for non-nextcloud backends', () => {
    expect(protectedCloudToggleVisible(true, [{ main_cloud_backend: 'opencloud' }])).toBe(false);
  });
  it('visible for a selected non-default nextcloud project', () => {
    expect(protectedCloudToggleVisible(true, [
      { is_default: true, main_cloud_backend: 'nextcloud' },
      { is_default: false, main_cloud_backend: 'nextcloud' },
    ])).toBe(true);
  });
});
```

Run `cd cockpit && npx vitest run src/app/views/session-create` → FAIL.

- [ ] **Step 4: Implement Cockpit side.**
  - `capabilities.service.ts`: extend the stored payload type with `features?: { protected_cloud?: boolean }` and add `readonly protectedCloudAvailable = computed(() => !!this._payload()?.features?.protected_cloud);` (adapt to the service's actual signal names — read it first; it already stores `grants`/`catalog`).
  - `api.model.ts` `Project`: add `main_cloud_backend?: string | null;`.
  - `session-create.component.ts`: extend the local `Project` interface with `main_cloud_backend?: string | null`; export the pure helper; add a `protectedCloud = signal(false)` and a checkbox block in the template, rendered under the projects section:

```html
@if (protectedCloudVisible()) {
  <label class="protected-cloud-toggle">
    <input type="checkbox" [checked]="protectedCloud()"
           (change)="protectedCloud.set($any($event.target).checked)" />
    {{ 'sessionCreate.protectedCloud' | transloco }}
  </label>
  <p class="hint">{{ 'sessionCreate.protectedCloudHint' | transloco }}</p>
}
```

  with `protectedCloudVisible = computed(() => protectedCloudToggleVisible(this.capabilities.protectedCloudAvailable(), this.selectedProjects()))` (derive `selectedProjects()` from the existing `projects()`/`selectedProjectIds()` signals). Match the component's actual template idiom (it may use `*ngIf` and existing form-row classes — mirror the neighboring rows; inline styles only). In `createSession()`: `if (this.protectedCloud() && this.protectedCloudVisible()) body['protected_cloud'] = true;`. Add the two transloco keys to the i18n files the component already uses (`grep -rn "sessionCreate\." cockpit/src/assets/i18n/ | head` — add "Protected cloud — agent writes are staged for your review" and a one-line hint "The agent gets read-only cloud access; its changes wait for your approval in the review panel.").

- [ ] **Step 5: Run.** `npx vitest run` (whole cockpit suite) → PASS. Also `pytest tests/cloud_staging/test_capabilities_features.py -q` → PASS.

- [ ] **Step 6: Commit:**

```bash
git add orchestrator/main.py tests/cloud_staging/test_capabilities_features.py \
        cockpit/src/app/core/services/capabilities.service.ts cockpit/src/app/core/models/api.model.ts \
        cockpit/src/app/views/session-create/ cockpit/src/assets/i18n/
git commit -m "feat(cockpit): protected-cloud session toggle behind capabilities features gate"
```

---

### Task 14: Cockpit badge + review panel (job-diff-review generalization)

**Files:**
- Modify: `cockpit/src/app/core/services/api.service.ts` (Mode A section :2035-2110), `cockpit/src/app/core/models/api.model.ts` (:1413-1471 neighborhood)
- Modify: `cockpit/src/app/views/job-diff-review/job-diff-review.component.ts` (+ `.html`)
- Modify: `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts`, `cockpit/src/app/views/persistent-chat/persistent-chat.service.ts`
- Test: extend `job-diff-review.component.spec.ts`; extend `persistent-chat.component.spec.ts`

**Interfaces:**
- Consumes: Task 8/10 endpoints; existing component contract (`jobId = input.required<string>()`, `resolved = output<'accepted'|'rejected'>()`, `JobDiffSummary`/`JobDiffFile`/`JobAcceptOutcome` types :1413-1471); `Thread.metadata` (api.model.ts:1135-1153) carries `protected_cloud`; the SSE `turn.completed` case in `persistent-chat.service.ts::_handleEvent` (:2095, switch on `data.method`).
- Produces:

```typescript
// api.model.ts
export interface ThreadCloudDiffSummary {
  thread_id: string;
  epoch: number;
  staged_at: string | null;
  counts: { added: number; modified: number; deleted: number };
  protected_mount: string | null;
  files: Array<{ path: string; status: 'added' | 'modified' | 'deleted'; binary: boolean }>;
}
export interface ThreadCloudDiffFile {
  thread_id: string;
  path: string;
  status: 'added' | 'modified' | 'deleted';
  old_content: string | null;
  new_content: string | null;
  old_binary: boolean;
  new_binary: boolean;
}

// api.service.ts (mirror the job methods' style incl. error mapping)
getThreadCloudDiff(threadId: string): Observable<ThreadCloudDiffSummary | null>
getThreadCloudDiffFile(threadId: string, path: string): Observable<ThreadCloudDiffFile | null>
applyThreadCloudDiff(threadId: string, epoch: number): Observable<JobAcceptOutcome>   // reuse the tagged
   // outcome union: 409 external_modifications_detected -> 'conflict'; 502 partial_write_failure ->
   // 'partial'; NEW: 409 epoch_stale -> add an outcome variant {kind:'stale'; staged_epoch:number}
rejectThreadCloudDiff(threadId: string, epoch: number): Observable<{ rejected: boolean } | null>
restageThreadCloudDiff(threadId: string): Observable<unknown>
```

- Component: `jobId` becomes `input<string | null>(null)`, new `threadId = input<string | null>(null)` (exactly one must be set — assert in an effect); when `threadId` is set, load/apply/reject via the thread methods, hold the summary's `epoch` in a signal, pass it to apply/reject, and on a `stale` outcome reload the diff with a "diff changed — reloaded" notice. Binary entries (`entry.binary` or file `old_binary/new_binary`) render a "Binary file (size on apply) — no preview" placeholder instead of Monaco. Accept button label switches to "Apply to cloud" in thread mode (transloco key). Job mode behavior is UNCHANGED (job-review's usage compiles untouched).
- persistent-chat: service gains `protectedCloud = computed(...)` (from the loaded Thread's `metadata['protected_cloud']`), `cloudChangesCount = signal(0)`, `cloudDiffPanelOpen = signal(false)`, and `refreshCloudDiffCount()` (fetch summary → set count; called on thread load and DEBOUNCED ~2s after each `turn.completed` event when protected — staging runs at turn end, so this is the natural refresh edge). Status bar gets (copying the exact badge idiom at :580-592):

```html
@if (chat.protectedCloud() && chat.cloudChangesCount() > 0) {
  <app-badge tone="accent" size="sm" role="button"
             [title]="'chat.status.cloudChangesTooltip' | transloco:{ mount: chat.protectedMountName() }"
             (click)="chat.cloudDiffPanelOpen.set(true)">
    {{ 'chat.status.cloudChanges' | transloco:{ count: chat.cloudChangesCount() } }}
  </app-badge>
}
```

  and a panel host beside the citations panel (same structural pattern; inline styles only, NOT persistent-chat.scss): `<app-job-diff-review [threadId]="chat.threadId()" (resolved)="chat.onCloudDiffResolved($event)" />` inside a dismissible drawer. `onCloudDiffResolved` → `cloudChangesCount.set(0)`, close panel. The badge tooltip carries `protected_mount` (the Slice B deferral #5 multi-mount signal — which mount is protected) + `staged_at` staleness.

- [ ] **Step 1: Failing vitest additions** — pure helpers only (extract them so they're testable):

```typescript
// job-diff-review: export function diffApiFor(jobId: string|null, threadId: string|null): 'job'|'thread'  (throws when both/neither)
// job-diff-review: export function isBinaryEntry(sum: {binary?: boolean}, file: {old_binary?: boolean; new_binary?: boolean} | null): boolean
// persistent-chat: export function cloudBadgeVisible(protectedCloud: boolean, count: number): boolean
// persistent-chat.service: export function cloudCountFromSummary(s: ThreadCloudDiffSummary | null): number  (sum of counts)
```

Run → FAIL.

- [ ] **Step 2: Implement** the model types, service methods, component generalization, badge + drawer + refresh wiring, and the transloco keys (`chat.status.cloudChanges` = "Cloud changes ({{count}})", `chat.status.cloudChangesTooltip` = "Staged on {{mount}} — click to review", diff-review's thread-mode button "Apply to cloud", binary placeholder "Binary file — applied byte-for-byte, no preview").

- [ ] **Step 3: Run.** `cd cockpit && npx vitest run` → PASS (all suites, ~353+ tests). Also `npm i --no-save @monaco-editor/loader && npx ng build` if you touch anything Monaco-adjacent, to confirm budgets (the known build gotcha).

- [ ] **Step 4: Commit:**

```bash
git add cockpit/src/app/core/ cockpit/src/app/views/job-diff-review/ \
        cockpit/src/app/views/persistent-chat/ cockpit/src/assets/i18n/
git commit -m "feat(cockpit): cloud-changes badge + staged-diff review panel (job-diff-review thread mode)"
```

---

### Task 15: Agent honesty prompt block

**Files:**
- Modify: `config/prompts/systemprompt_interactive.txt` (+ `_deepseek`, `_glm`, `_gpt_5` variants) AND the `orchestrator/config/prompts/` mirror copies of the same four files (verify the mirror exists and diverges only intentionally: `diff config/prompts/systemprompt_interactive.txt orchestrator/config/prompts/systemprompt_interactive.txt`)
- Modify: `src/core/loader.py` (`render_instruction_content` :927-964, call site :3915; Jinja context :959-963)
- Modify: `src/api/persistent_app.py` (thread the flag into config — mirror how `_cli_datasources` reaches the loader via `config.extra`, loader.py:3913)
- Test: extend the loader test module covering `render_instruction_content` (find: `grep -rln "render_instruction_content" tests/`)

**Interfaces:**
- Consumes: the agent already knows the thread is protected — `persistent_app.py` reads `protected_cloud` from the workspace/attach payload at :1271/:1296-1297/:1730-1731; `render_instruction_content(template, tool_names, cli_datasources=...)` exposes Jinja vars `tools`, `has_tool`, `cli_datasources`, `has_cli_datasource`.
- Produces: `render_instruction_content(..., protected_cloud: bool = False)` adding `protected_cloud` to the Jinja context; the block below rendered ONLY when True.

**Exact block** — inserted in all four interactive variants directly AFTER the `Workspace:` block (base file lines 72-76; find the equivalent block in each variant — the wording around it differs per family, the inserted block is identical):

```
{% if protected_cloud %}
Protected cloud mode:
- The cloud folder (workspace/cloud) is in PROTECTED mode: everything you write there is STAGED for the user's review — nothing is saved to the cloud or visible to anyone else until the user applies it in the review panel.
- Never say a cloud file is "saved", "uploaded", or "shared". Say it is "staged for your review".
- When a piece of work is ready, tell the user so they can open the review panel ("Cloud changes" in the session header) and apply it.
{% endif %}
```

- [ ] **Step 1: Failing loader tests:**

```python
test_protected_block_rendered_when_flag_true      # rendered text contains "staged for your review"
test_protected_block_absent_when_flag_false       # and contains no "{%" residue
test_protected_block_absent_by_default            # omitted kwarg
```

- [ ] **Step 2: Run to verify failure** (TemplateError/undefined or block leaking as raw text).

- [ ] **Step 3: Implement.** (a) Add `protected_cloud: bool = False` to `render_instruction_content` and `"protected_cloud": protected_cloud` to its Jinja context dict. (b) At the call site (:3915), pass it from the same channel `_cli_datasources` uses (`config.extra` — read :3910-3915 and mirror). (c) In `persistent_app.py`, where the attach payload's `protected_cloud` is read (:1296-1297), stash it into that channel so the loader sees it. (d) Insert the block into all 4 files + the mirror copies.

- [ ] **Step 4: Run** the loader tests + a grep sanity: `grep -l "protected_cloud" config/prompts/ orchestrator/config/prompts/ -r` → 8 files.

- [ ] **Step 5: Commit:**

```bash
git add config/prompts/ orchestrator/config/prompts/ src/core/loader.py src/api/persistent_app.py <loader test file>
git commit -m "feat(agent): protected-cloud honesty block in interactive prompts (conditional Jinja)"
```

---

### Task 16: Engage-path dedup, docs sync + close-out

**Files:**
- Modify: `orchestrator/main.py` (`_engage_protected_cloud_for_thread` :19409-19416 → use `select_protected_mount`)
- Modify: `docs/design/cloud_access_unification.md` (§11 status), `docs/superpowers/specs/2026-07-12-protected-cloud-slice-c-design.md` (status line)
- Test: full-suite runs

- [ ] **Step 1: Dedup the mount pick.** Replace the inline first-NC-mount `next(...)` in `_engage_protected_cloud_for_thread` with `select_protected_mount(mount_rows)` (Task 3). Run the engage tests (Task 2's file) → PASS.

- [ ] **Step 2: Full regression sweep.**

```bash
pytest tests/cloud_staging/ tests/cloud/ tests/cloud_overlay/ tests/cloud_mount/ \
       tests/test_job_diff_endpoints.py -q          # expect all green
cd cockpit && npx vitest run                        # expect all green
ruff check orchestrator/services/cloud_staging orchestrator/services/diff_source.py \
       src/services/cloud_overlay src/services/cloud_mount
```

- [ ] **Step 3: Docs.** Append to the design doc's §11 status line: Slice C implemented (staging pipeline, DiffSource review/apply, toggle + badge, honesty copy, 5 Slice B deferrals closed) with the date. Update the spec's Status line to "implemented on develop". One-line entries, matching the existing style.

- [ ] **Step 4: Commit:**

```bash
git add orchestrator/main.py docs/design/cloud_access_unification.md \
        docs/superpowers/specs/2026-07-12-protected-cloud-slice-c-design.md
git commit -m "docs(design): Slice C status + engage uses select_protected_mount"
```

---

## Post-plan validation (manual, k3d — NOT part of task execution)

The live gate mirrors Slice B's: create a protected session from the Cockpit checkbox → agent writes → badge shows N → review panel renders the diff (incl. a binary file) → Apply → bytes verified in the NC groupfolder + upperdir cleared + fresh workdir + baseline re-captured → second run: Reject → upperdir cleared, cloud untouched → third check: end the session, review + apply from S3 with the pod gone. Run after all 16 tasks land; evidence goes into the design doc §11 and the spec status line.

## Verify-against-reality at implementation time (flagged inline above)

- The existing `cloud_ro_mounts` CRUD test file name + its fake-pool fixture (Task 1 Step 1).
- The engage test file name + its refusal-cleanup idiom (Task 2).
- `list_thread_mounts` row keys (`backend_id`/`cloud_handle`/`mountpoint`) — fix `select_protected_mount` + its test if they differ (Tasks 3/8).
- `save_blob`'s exact signature; snapshot-service availability-flag/logger names (Task 4).
- `WorkspaceSuspensionService` attribute names for db/snapshot service + the thread-dict local (Task 5).
- The session attribute holding `OverlayMountManager` and the rclone manager (Tasks 5/9/11); the mount payload field carrying the protected `mount_id` (Task 11).
- The persistent-thread proxy's agent-base-URL resolution helper (Task 10).
- Script-builder quoting style in `overlay_mount.py` (Task 9).
- Whether project serializers already include `main_cloud_backend`; capabilities service internal signal names; session-create template idiom (`@if` vs `*ngIf`) and i18n file layout (Tasks 13/14).

## Self-review notes (author)

- **Spec coverage:** §2 decisions → whole-diff+epoch (T10), no agent tool (honesty copy T15 carries the tell-the-user instruction), orchestrator-pull (T4/T5), latest-epoch-only (T4), default projects excluded (T13 helper); §4 toggle → T13; §5 staging → T1/T3/T4/T5; §6 review → T6/T7/T8 (+Cockpit T14); §7 apply/reject → T9/T10; §8 honesty → T15; §9 deferrals: #1 ENOTCONN → T11, #2 fresh-workdir → T9 (reset) + T11 (heal), #3 quota reads → T12, #4 reader_id → T2, #5 multi-mount signal → T8 (`protected_mount` field) + T14 (tooltip), #6 struck (spec corrected); §10 security → owner gates (T8/T10), internal-key (T5), no S3 creds off-orchestrator (T4/T5 design); §11 failure posture → stage never-blocks (T4/T5), partial-failure retention (T10), reconciler untouched (verified — it only revokes grants); §12 testing → per-task; §13 rollout → flag gates in T5/T8/T13.
- **Type consistency:** `DiffEntrySummary/DiffSummary/DiffFileContent` defined once (T6), consumed T7/T8/T10; `stage_thread_cloud_diff(*, thread_id, postgres_db, snapshot_service)` identical at T4 definition and T5/T8 call sites; `update_ro_mount_staging(row_id, *, staged_epoch, staged_summary)` identical T1/T4/T10; `reset_upper(refresh_lower)` T9 definition = T10's agent route behavior; epoch flow: stage=+1 w/ summary (T4), apply/reject=+1 w/ None (T10), pin=equality (T10), UI reload on `stale` (T14).
- **Known-limitation carried:** dead-pod apply + later resume can resurrect applied/rejected staging from the snapshot (T10 docstring + spec §7); S3 lifecycle for abandoned stagings out of scope (spec §11).




