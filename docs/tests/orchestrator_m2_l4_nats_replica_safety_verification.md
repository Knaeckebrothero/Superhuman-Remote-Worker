# Orchestrator M2-L4 (NATS Replica-Safety) — Verification Record

**Feature:** `docs/features/orchestrator_ha_scaling.md` — Milestone M2, Layer 4 (NATS handler replica-safety).
**Spec:** `docs/superpowers/specs/2026-06-28-orchestrator-m2-l4-nats-replica-safety-design.md`.
**Plan:** `docs/superpowers/plans/2026-06-28-orchestrator-m2-l4-nats-replica-safety.md`.

**Status (2026-06-28): Code complete + unit-verified on develop (UNPUSHED). Live (k3d + dev) two-replica checks still owed.** The `replicas: 2` NATS double-consume gap (both replicas ran every VM/sudo handler — no queue group, not leader-gated) is closed for the two harmful handlers using M1's primitives: `sudo.request` is claim-deduped on its unique NATS reply subject (migration 0040), and the daemon-`register` IDE seed is leader-gated. `session.events` and the four benign handlers are untouched. No queue groups.

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

## Still owed — live two-replica checks (operator-run, per the spec test plan)

- [ ] **k3d two-replica** (`replicas: 2`): force a daemon `register` → exactly **one** SSH IDE-seed in the logs (was two); issue a human-approval `sudo.request` → exactly **one** `sudo_approval_requests` row + **one** operator prompt (was two); approve on the **non-consuming** replica → the daemon receives the response (cross-replica reply via the persisted `nats_reply_subject`).
- [ ] **Migration `0040` applies** cleanly in the full chain on first deploy.
- [ ] **Live dev** (quiet window, under real traffic): repeat the k3d checks; confirm no duplicate `sudo_approval_requests` rows accrue.

## Known residual (by design — out of scope, tracked)

After the fix, the *live* SSE sudo prompt fires only on the claim-winner replica; an operator on the other replica still **sees** the request via `list_sudo_requests` (main.py) on load/poll, just not as an instant push. Closing this fully (live push to operators on any replica) is **L3** (cross-replica SSE fan-out), a separate slice.
