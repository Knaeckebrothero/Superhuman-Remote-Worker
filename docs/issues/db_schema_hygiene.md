# DB schema hygiene — dead code and snapshot drift

**Status:** open — first slice landed on `develop` (`f4160780`, 2026-06-11);
items 1–4 below remain.
**Origin:** 2026-06-11 database-architecture review (`docs/features/database_architecture.md`),
while deciding whether the audit-store refactor should bundle schema cleanup.
Verdict there: targeted hygiene only — the audit migration stays byte-parity,
and schema restructuring happens only when a feature or measured problem
forces it. This doc tracks the targeted part.

## Done (on `develop` — `f4160780`, 2026-06-11)

- **Pre-BFF session block deleted** — `orchestrator/database/postgres.py`
  `SESSION OPERATIONS` section (6 methods, 91 lines: `create_session`,
  `get_session`, `update_session_activity`, `delete_session`,
  `delete_expired_sessions`, `delete_sessions_by_user`). They targeted the
  `sessions` table that migration `0009_srw_sessions.sql` dropped; zero
  callers anywhere — any call would raise `UndefinedTableError`. The live
  path is the cookie BFF over `srw_sessions` (`auth/bff.py`,
  `security/auth.py`).
- **Permanently-skipped relic tests deleted** — six
  `@_skip_not_implemented` classes in `tests/test_user_management.py`
  (`TestPasswordModule`, `TestGetCurrentUser`, `TestValidateSession`,
  `TestSeedDefaultUsers`, `TestMcpTokenEndpoints`,
  `TestAdminBootstrapFlow`) plus the `_make_session` helper and the
  `security.password` import gate. That module will never exist — the
  password/cookie-session design lost to Keycloak + BFF — so the gate
  could never open. Before/after: 50 passed in both runs; skips 29 → 2
  (both remaining are unrelated "HomeLab deployment files not present").

## Open items

1. **Generated current-schema artifact** (highest value, ~half day).
   `schema.sql` / `vector_schema.sql` are frozen at cutover and now 25 / 5
   migrations stale — the live schema exists nowhere readable. The frozen
   file still advertises the dropped `sessions` table; that is exactly how
   the dead methods above survived a year. Proposal: a CI step (or make
   target) that runs migrate-from-zero against a scratch Postgres and
   commits `pg_dump --schema-only` output as generated, clearly-marked
   read-only `schema_current.sql` / `vector_schema_current.sql`. The audit
   family (`migrations/audit/`) joins automatically once it exists.

2. **"User Auth Fields" relic family** — the section now directly after the
   deleted block in `postgres.py` (`get_user_by_email_with_auth`,
   `set_user_password`, `set_email_verified`, `create_user_with_password`,
   `migrate_existing_users_verified`). Same pre-Keycloak password design;
   the only caller of any of them is their own unit test in
   `TestPostgresDBUserOps` (tested dead code), and `init.py` no longer
   references seeding/password paths at all. Unlike the session block the
   `users` table exists, so before deleting: confirm nothing reads the
   `password_hash` / `email_verified` columns elsewhere, then remove the
   methods + their tests (and consider a migration dropping the columns).

3. **Messaging-trio verdict** — `message_log`, `external_contacts`,
   `notification_queue` are written only via the IMAP-poller path
   (`orchestrator/services/imap_poller.py`). Decide whether the
   email/messaging subsystem has a future; either it gets an owner or the
   tables + code get dropped. Needs a product call, not just a grep.

4. **Naming residue** (cosmetic, do opportunistically) —
   `list_mcp_tokens` / `cleanup_expired_mcp_tokens` and the `/api` token
   endpoints still speak "mcp_tokens" over the consolidated `auth_tokens`
   table (migration 0010 renamed it).

## Non-goals — owned elsewhere

- Audit-store schema → `postgres_audit_store_implementation.md`
  (byte-parity locked; do not bundle cleanup into that train).
- Vector schema → memory-overhaul track (actively operating on it).
- LLM key/override sprawl (`user_api_keys`, `project_api_keys`,
  `system_api_keys`, `llm_endpoints`, `models`, `config_overrides`) →
  config-matrix branch (migrations 0021/0022 are already generalizing it).
- `chat_history` → `thread_messages` convergence → product decision,
  deliberately decoupled from the storage migration.
- `jobs.context` / `config_override` JSONB blobs → deliberate pattern with
  atomic merge helpers, not debt.
