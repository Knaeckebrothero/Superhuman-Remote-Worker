# Orchestrator M2-L4 (NATS Replica-Safety) — Verification Record

**Feature:** `docs/features/orchestrator_ha_scaling.md` — Milestone M2, Layer 4 (NATS handler replica-safety).
**Spec:** `docs/superpowers/specs/2026-06-28-orchestrator-m2-l4-nats-replica-safety-design.md`.
**Plan:** `docs/superpowers/plans/2026-06-28-orchestrator-m2-l4-nats-replica-safety.md`.

**Status (2026-06-28): Code complete, unit-verified, PUSHED to develop + deployed to dev (`sha-5355050`), and LIVE-VERIFIED on the dev two-replica cluster.** The `replicas: 2` NATS double-consume gap (both replicas ran every VM/sudo handler — no queue group, not leader-gated) is closed for the two harmful handlers using M1's primitives: `sudo.request` is claim-deduped on its unique NATS reply subject (migration 0040), and the daemon-`register` IDE seed is leader-gated. `session.events` and the four benign handlers are untouched. No queue groups.

## What changed (commits — match by subject; the push hook rewrites SHAs)

| # | Commit subject | What |
|---|---|---|
| 1 | `feat(orchestrator): claim sudo requests on the NATS reply subject …` | Migration `0040` (partial unique index `uq_sudo_request_reply_subject`) + `_insert_request` `ON CONFLICT … DO NOTHING`; now returns `None` on a lost claim and **raises** on a genuine DB error (no longer swallows). |
| 2 | `feat(orchestrator): on_sudo_request drops on lost claim, denies on DB error …` | Handler branch: lost claim → drop silently (the winner owns it); DB error → deny so the daemon doesn't hang; else proceed unchanged. |
| 3 | `feat(orchestrator): leader-gate the daemon-register IDE seed …` | `_on_daemon_register` spawns `_seed_vm_ide_config` only when `is_leader` (flattened import); context upsert + dispatch poke unchanged. |

## Verified via automated tests (2026-06-28) — 9 tests, all green

`.venv` Python 3.12.10 (matches CI). DB tests via `testcontainers.PostgresContainer("postgres:16")` over the podman socket.

| Test file | Tests | What it proves |
|---|---|---|
| `tests/test_sudo_request_claim.py` | 4 | Two concurrent `_insert_request` for one reply subject → exactly one inserts (the other returns `None`); distinct subjects each claim; `NULL` subject is unconstrained (vm_upgrade path); a genuine DB error **raises** (not swallowed to `None`). |
| `tests/test_on_sudo_request_claim.py` | 3 | Lost claim → no auto-eval / no SSE / no `_pending_msgs` / no reply (drop); DB error → `_nats_reply(..., approved=False)` (deny); winner with no auto-match → broadcasts + stores `_pending_msgs`. |
| `tests/test_nats_register_seed_gate.py` | 2 | `_seed_vm_ide_config` runs on the leader, is skipped on the follower. Uses the **flattened** `services.leader_election.is_leader` so the test toggles the same Event the handler reads. |

Run recipe:
```
DOCKER_HOST="unix:///run/user/$(id -u)/podman/podman.sock" TESTCONTAINERS_RYUK_DISABLED=true \
  .venv/bin/python -m pytest tests/test_sudo_request_claim.py \
    tests/test_on_sudo_request_claim.py tests/test_nats_register_seed_gate.py -v
```
`ruff check` clean across all touched files.

## Verified live on dev — two replicas (2026-06-28)

Deployed as `sha-5355050` (both replicas on node1+node3, **0 restarts**). `ORCHESTRATOR_ID=srw-dev`. Method: **targeted NATS injection** from an orchestrator pod (`nats-py`) — publishing onto the live fan-out subjects exercises the identical orchestrator code path (fan-out → `on_sudo_request` claim / `_on_daemon_register`) without staging a full VM-tier job (which would drag in the VM controller, headscale, and agent-sudo machinery unrelated to M2-L4).

| Check | Result |
|---|---|
| **Deploy clean** | **PASS** — both replicas Running on `sha-5355050`, **0 restarts**; one leader (`SRW_LEAD` on `m552t`). The new in-function `is_leader` import didn't break boot. |
| **Migration `0040` applied** | **PASS** — boot log `→ 0040_sudo_request_reply_subject_unique.sql` → `✓ … (23 ms)`; the `✓` proves the collapse-DELETE *and* the unique-index build both succeeded on the real schema. Index confirmed via `pg_indexes`: `… btree (nats_reply_subject) WHERE (nats_reply_subject IS NOT NULL)`. **0 duplicate** reply-subject groups in the 7 existing rows. |
| **Sudo dedup (the fix)** | **PASS** — published one synthetic `sudo.request.srw-dev.*` (real `job_id` for the FK) to the fan-out subject. **Both** replicas logged receiving it (`m552t` 15:36:53.110, `sjh2p` …106), yet **exactly one** `sudo_approval_requests` row was created. Pre-fix: two rows + two prompts. Test row deleted; back to baseline (7 rows, 0 test rows). |
| **Register import + handler (M1-class guard)** | **PASS** — published a synthetic `register`; **both** replicas logged `Daemon registered for job …` with **no** `Error handling daemon register` / `ModuleNotFound`, confirming the new flattened in-function `from services.leader_election import is_leader` resolves in the deployed image. |

The cross-replica approve reply (`_finalize_request` → `_nats_reply` on the persisted subject) is unchanged by M2-L4 and was confirmed structurally; the live SSE-prompt residual stays as documented above.

**M2-L4 is code-complete, unit-verified, and live-verified on dev.**

## Known residual (by design — out of scope, tracked)

After the fix, the *live* SSE sudo prompt fires only on the claim-winner replica; an operator on the other replica still **sees** the request via `list_sudo_requests` (main.py) on load/poll, just not as an instant push. Closing this fully (live push to operators on any replica) is **L3** (cross-replica SSE fan-out), a separate slice.
