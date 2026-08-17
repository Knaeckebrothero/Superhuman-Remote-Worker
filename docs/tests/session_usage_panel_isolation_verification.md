# Session Usage Panel Isolation — Verification Runbook

Verifies the fix for the composer usage panel rendering the **previous**
session's token counters in a brand-new session (`INPUT 154.6k · CTX 48%` beside
`Turn 0`).

- **Design / what & why:** `docs/done/session_usage_panel_leaks_previous_session_counters.md`
- **Prior art:** the S5 panel itself — `docs/features/context_summarization_rework.md` §4.6

**One-line root cause:** `usage` is per-thread state living on an app-lifetime
root singleton, written by the `usage.updated` handler and reset nowhere.

---

## 1. Invariants under test

| # | Invariant | Enforced by |
|---|---|---|
| **I1** | The panel shows the open thread's numbers, or nothing | `UsageState.threadId` + the `currentUsage` render gate; explicit resets on thread transitions |
| **I2** | A reload / thread switch restores the same numbers | `usage` on the durable `session.state` snapshot, aggregated from `thread_events` |
| **I3** | Restore + replay never double-count | `coveredBySnapshot` guard in the `usage.updated` handler, bounded by the same `hwm` the snapshot aggregates to |
| **I4** | An older peer that omits `usage` still gets a panel | the drop is gated on `snapshotSeededUsage` — nothing seeded means nothing to double-count, so covered frames still accumulate |

---

## 2. Unit gates

```bash
# Server: aggregation rule (accumulate across a turn's calls, sticky latest-wins,
# jsonb-as-text tolerance, null when the thread never reported usage)
.venv/bin/python -m pytest tests/test_session_state_snapshot.py -q      # 27 passed

# Client: isolation, snapshot seeding, no-double-count, legacy-peer tolerance
cd cockpit && npx vitest run src/app/core/services/persistent-chat.service.spec.ts
```

**Proving the gates bite.** Both guards were disabled in turn and the suite
re-run — a green suite against a reverted guard is worth nothing:

| Guard reverted | Result |
|---|---|
| `if (coveredBySnapshot) break;` | `does not double-count…` fails: `expected 1800 to be 900` |
| `currentUsage` thread binding + the three resets | 3 isolation specs fail — the original bug, reproduced |

---

## 3. Live k3d run (2026-08-17)

Two sessions on the running cluster. **Navigate in-app**, never with a fresh
page load: a full reload rebuilds the singleton and would mask the defect
entirely. The original report came from an SPA route change.

| Step | Expected | Observed |
|---|---|---|
| 1. Send a message in session A | Panel shows A's provider numbers | `INPUT 14.1k OUTPUT 98 REASONING ~85 CTX 13%`, Turn 1 |
| 2. Hard-reload A | Same numbers, output **not** doubled | identical — `98`, not `196` (**I2**, **I3**) |
| 3. `GET /state` for A | `usage` key populated | `input 14052 · output 98 · reasoning 85 (est) · ctx_limit 131072 · threshold 104857` |
| 4. Sessions → New Session → Create (all in-app) | **No panel** | `.usage-panel` absent; header `Untitled Session · Connected`, Turn 0 (**I1** — this is the reported bug) |
| 5. Send a message in B | B's own numbers, accumulated from zero | `INPUT 13.6k OUTPUT 42 REASONING ~33 CTX 13%` |
| 6. In-app switch back to A | A's numbers, not B's, not doubled | `INPUT 14.1k OUTPUT 98 REASONING ~85 CTX 13%` |

Step 2/3 is the real **I3** case, not a contrived one: A's only usage frame sits
at `seq 96`, between `replay_cursor 1` and `event_cursor 99`. It is therefore
both aggregated by the snapshot *and* re-delivered by replay. `OUTPUT 98`
proves the guard fired; without it the panel would have read `196`.

### Multi-frame aggregation against real Postgres

A tool-using turn is the case where a turn has more than one usage frame. The
live attempt at one hung on an unrelated stalled LLM call (agent silent after
`Persistent loop started with 50 tools`; the queue unit stayed `leased` past its
lease — nothing to do with this change, which touches only the `/state` read
path and the Cockpit). The query was instead exercised directly against the
cluster's Postgres inside a rolled-back transaction:

```sql
BEGIN;
INSERT INTO thread_events (thread_id, epoch, seq, kind, payload) VALUES
 (<thread>, 0, 49, 'usage.updated', '{"turn": 2, "input_tokens": 15000, "output_tokens": 120, "reasoning_tokens": 60}'),
 (<thread>, 0, 50, 'usage.updated', '{"turn": 2, "input_tokens": 16500, "output_tokens": 33}'),
 (<thread>, 0, 51, 'usage.updated', '{"turn": 3, "input_tokens": 99999, "output_tokens": 7}');
-- …the production CTE, with hwm = 50…
ROLLBACK;
```

Returned seq `43, 49, 50` — all three frames of turn 2, the turn-3 frame
excluded, and the frame missing `reasoning_tokens` tolerated. Post-rollback row
count confirmed unchanged. **Still owed:** the same path through a genuine
tool-using turn once the cluster's LLM path is healthy.

### Turn selection, live

Session A's stalled turn later landed, giving it two usage frames in different
turns — `seq 96 (turn 2, 14052/98/85)` and `seq 162 (turn 3, 13347/85/50)`. The
panel and `/state` then both read **turn 3 only** (`13.3k / 85 / ~50`), not the
sum of both. The aggregation is per-turn, not per-thread, as intended.

### Re-verified after the I4 gate

The `snapshotSeededUsage` gate was added after the run above; both sessions were
re-checked on the live cluster afterwards. `/state` and the rendered panel agree
exactly (`input 13347 · output 85 · reasoning 50 est`), output still not doubled.

---

## 4. Known limitations (by design, not defects)

- **The panel is one message behind at rest.** `INPUT` is the last *request's*
  prompt, so it excludes the reply that request produced. Self-heals on the next
  call. Making the CTX gauge exact needs a locally tokenized `ctx_used_tokens`,
  rejected in the design doc — it would mix an estimate into a panel that is
  otherwise entirely provider truth.
- **A `gone_beyond_horizon` re-anchor** replays nothing, but re-runs
  `_loadSessionState`, so the snapshot still restores the panel.
- **A stranded `turn.started` on an idle runtime** sets `replay_seq = hwm`; the
  snapshot is the only restore path there, and it covers it.
