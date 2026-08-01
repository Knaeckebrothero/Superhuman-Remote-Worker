# Debug audit view refactor — from "download the whole job" to a windowed trace inspector

**Status:** **Implemented + k3d/browser-verified.** 2026-06-29; **P4 (chat lean + lazy hydration) added 2026-07-30 — see §0.1.** P0 committed (`7ea0d798`); P1 + the Agent-Activity refactor (P2) pushed + **deployed to dev**; **chat + graph migration + slider removal + dead-code sweep + a sort toggle done + verified, uncommitted on `develop`.** **The MCP bulk-tool migration + removal of the backend `/{audit,chat,graph}/bulk` endpoints + `get_*_bulk` store methods (incl. the retired `mongodb.py` copies) is now done too (2026-06-29) — the last OOM-prone read path is gone, and it's been verified end-to-end on the main cluster via the real MCP, including reading the original OOM job `19707fa1` at `limit=5000` without a crash.** Current as-built state is in **§0 below**; the original design + roadmap (§§1–10) are kept for context with deviations annotated. Slider + synchronized replay: **removed entirely** (owner: demo-only). The crash that motivated this is root-caused in the companion issue `docs/issues/audit_metadata_config_duplication_ooms_orchestrator.md` (the write-side OOM fix is **Phase 0**). The enabler: most server-side primitives already existed (`get_job_audit` paged+filtered, `get_request` lazy-detail, `iter_tool_calls` keyset) — so this was largely **frontend deletion + one lean projection**, not a rewrite.
**Component:** Cockpit debug dashboard (`cockpit/src/app/debug/**`, `cockpit/src/app/core/services/data.service.ts`, `indexed-db.service.ts`, `api.service.ts`) · audit read path (`orchestrator/database/audit_store.py`, `orchestrator/main.py` `/api/jobs/{id}/audit*`).
**Related:** `docs/issues/audit_metadata_config_duplication_ooms_orchestrator.md` (P0 root cause) · `docs/done/chat_history_tail_injections_swallow_the_conversation_delta.md` (**P4**, 2026-07-30 — chat gets the same lean+detail split, plus the write-side delta bug it exposed) · memory topics `project_self_improvement_loop`, `project_loop_repo_compounding` (loop jobs run the most steps → most exposed) · `project_cross_pod_checkpointer_d3` (separate checkpoint-blob bloat).

---

## 0. Implementation status (as built — 2026-06-29)

Functionally **complete and verified** (k3d build + 716 cockpit tests + live browser via Playwright). Two deliberate deviations from the original plan:

- **Infinite scroll, not CDK virtual scroll.** `@angular/cdk-experimental` (autosize, needed for variable-height *expandable* rows) isn't installed. Rather than add an experimental dep or split detail into a separate pane, each panel uses paged **infinite scroll** (fetch the next page near the bottom). It still kills the eager download and renders incrementally; strict viewport virtualization stays a future option if very large jobs strain the DOM.
- **Driven off `DataService.currentJobId`, not `jobContext.activeJobId`.** The debug dashboard's job dropdown sets `currentJobId` (via `setCurrentJob`) but never populates `activeJobId` — caught live via Playwright: the panel sat empty because the effect watched the wrong signal.

### Done + verified

| Area | What landed |
|---|---|
| **P0** (backend) | config blobs stripped from per-row audit metadata; committed `7ea0d798`, deployed to dev. *Backfill of existing rows run on **local k3d only**, NOT dev/prod (see Remaining).* |
| **P1** (backend) | `_STITCH_LEAN` + `lean=` flag on `get_job_audit`; `get_audit_step` + `GET /api/jobs/{id}/audit/step/{step_id}`. Stitch composed from shared parts so CORE/LEAN can't drift. MCP fat path unchanged. |
| **P2 API** | `ApiService.getAuditPage(…, order)` (lean, offset-paged) + `getAuditStep`. |
| **P2 frontend** | new **`AuditTraceService`** (paged infinite-scroll + lazy per-step detail + server-side filter + asc/desc order); **`agent-activity.component`** rewritten onto it. Pushed + deployed to dev. |
| **P3 chat** | new **`ChatTraceService`** (paged `/chat`); **`chat-history.component`** rewritten onto it (infinite scroll, decoupled from the slider). *Superseded by P4 (§0.1): pages are now lean + hydrated on demand.* |
| **P3 graph** | `GraphService` already loaded independently (`getGraphChanges`); removed its now-inert slider-sync effects. |
| **2c — kill the OOM** | `DataService.loadJob` gutted → **no `/*/bulk` eager download**; `DataService` slimmed to a job-selection holder. |
| **2c — slider** | removed from `timeline.component` (scrubber/play/time/loading/cache); timeline = job dropdown + refresh + AUTO. |
| **Sort toggle** (new, beyond plan) | asc⇄desc order toggle on Agent Activity (backend `order=` already existed) → newest-first lands you on the end of a job. |
| **Dead-code sweep** | removed `fetchAndCacheJob`/`loadWindow` + all slider/window signals from `DataService`; `getJobAuditBulk`/`getChatHistoryBulk`/`getGraphDeltasBulk` + `Bulk*Response` from `ApiService`; graph slider-sync. `data.service.spec` rewritten. **716/716 tests pass.** |
| **MCP migration + backend bulk removal** (2026-06-29) | MCP `get_audit_bulk`→`/audit?lean=true&offset&limit&filter` (and the `filter` that was silently dropped is now wired through); `get_chat_bulk`→`/chat?offset&limit`; both capped at 200. Added offset/limit to `AuditStore.get_chat_history` + the `/chat` endpoint (mirrors `/audit`). **Deleted** the three `/{audit,chat,graph}/bulk` endpoints + `get_job_audit_bulk`/`get_chat_history_bulk`/`get_graph_deltas_bulk` store methods. `/graph/bulk` had **zero** consumers. New tests: 5 MCP-client (params/caps/filter) + 3 chat-pagination contract; **303 audit/chat/mcp/archiver tests pass**, ruff clean. |
| **Legacy `mongodb.py` cleanup** (2026-06-29) | Removed the matching `get_job_audit_bulk`/`get_chat_history_bulk`/`get_graph_deltas_bulk` from the retired `MongoDB` store (zero call sites; the live reader is `AuditStore`) + the stale docstring example. The non-bulk methods that real callers still use are untouched; MongoDB/MCP/pagination tests green. |
| **P4 — chat lean + hydration** (2026-07-30, shipped `e4244dfe` + `a5d93f71`) | Chat finally gets the P1/P2 treatment it was explicitly denied in P3 (see §0.1): `lean=` on `/chat` + `GET /chat/entry/{entry_id}`, `ChatTraceService.hydrateEntry`, expand-in-place everywhere, injected context collapsed to one strip, tool results resolved via a window-wide `tool_call_id` map. Fixed en route: the write-side delta bug that had been dropping real tool results (`docs/done/chat_history_tail_injections_swallow_the_conversation_delta.md`). |

**Browser-verified (697-step job):** selecting a job issues only `/audit?…lean=true` + `/chat?page` (**no `/*/bulk`**); Agent 100/697, Chat 100/160; expand → `/audit/step/{id}` fills heavy args; filter re-queries server-side (errors→0, tools→159); sort → `order=desc` shows #697 first; slider gone; **0 console errors**.

### 0.1 P4 — the chat panel catches up (2026-07-30)

P3 migrated chat onto paging but deliberately stopped there: *"chat content
renders inline, so (unlike the audit trace) there is no lean projection or
per-row detail fetch."* That held only while turns were small. Once the
transient injections (todos, memory, knowledge, instruction files) moved to the
message-list **tail** for prompt-cache reasons, every archived turn started
carrying the whole re-injected frame — on one dev job, **99.2 % of stored chat
input bytes** — and the panel rendered it as conversation, one `<active_tasks>`
wall per turn. Chasing that surfaced a genuine write-side bug: the archiver's
"delta = everything after the last `AIMessage`" heuristic was anchoring on the
*synthetic* `AIMessage` of an injection pair and **dropping the real tool
results**, which is why every tool card read "Result pending or not available".

Root cause, measurements, and the write-side fix are in
`docs/done/chat_history_tail_injections_swallow_the_conversation_delta.md`. The
read/render half is a straight application of this document's own pattern:

| Concern | Audit (P1/P2) | Chat (P4) |
|---|---|---|
| Lean list projection | `_STITCH_LEAN` + `lean=` on `/audit` | `_lean_chat_doc` + `lean=` on `/chat` (previews + `truncated`/`chars`) |
| Per-record detail | `GET /audit/step/{step_id}` | `GET /chat/entry/{entry_id}` |
| Frontend | `AuditTraceService` + row expand → detail fetch | `ChatTraceService.hydrateEntry()` swaps the full row in place; every body gets a `Show full (5.2 kB)` control |
| Heavy-but-never-rendered field | `metadata.resolved_config` (stripped at write, P0) | the injected context frame (stored as `type="context"` descriptors; full text only when its hash changes) |

Two things beyond the audit pattern, both forced by chat's shape:

- **Cross-row references.** A tool *result* lands in a later turn than its
  *call*, so `getToolResult` peeked at `entries[idx+1]` — fragile across
  empty-delta turns and page boundaries. Now a `tool_call_id → result` map over
  the whole loaded window. Follow-up `a5d93f71`: only the **last loaded** turn
  can legitimately be waiting on data, so `resolveToolResultState()` separates
  `unloaded` (spinner + a "Load it" action) from `missing` ("No result recorded")
  — otherwise every result the write-side bug destroyed was labelled as still
  coming.
- **Retroactive classification.** The ~66k rows already written can't be fixed
  by the write-side change, so the component classifies legacy raw injections
  client-side (`<active_tasks>` prefix, `*_inject_` tool-call ids) into the same
  context strip. The MCP `_format_chat_entry` does the same collapse.

Also removed here: the chat panel's shell-state widget (~150 lines + styles +
i18n), keyed on a `ChatEntry.shell_state` field no reader has emitted since the
Postgres cutover — the same "delete what the UI never renders" sweep as §8.

**MCP migration verified on k3d (real job `74c2371a`, admin auth, live orchestrator):** `get_audit_bulk`→3/21 lean entries (no `metadata`/`tool.arguments`), `get_chat_bulk`→3/5 turns, both formatted; `limit=5000` clamps to 200; `filter=errors`→0 vs `all`→21 (filter now reaches the endpoint). OpenAPI confirms the three `/*/bulk` routes are gone and `/chat` exposes `offset`+`limit`. Real-data pagination: `offset=0,limit=2` and `offset=2,limit=2` return disjoint row ids on a 24k-turn job; legacy `page=2,pageSize=2` is byte-equivalent.

**Verified on the main (dev) cluster after deploy — via the real `orchestrator-cluster` MCP** (2026-06-29, pushed + redeployed by owner): on a 2841-entry job, `get_audit_bulk` `filter` is now wired (`all`→2841 / `tools`→761 / `errors`→57, with **global** step numbers, not renumbered); `get_chat_bulk` honors `offset` (`offset=0`→turns 1-3, `offset=3`→turns 4-6); page-based `get_audit_trail` + `get_chat_history` unchanged (no regression from adding offset/limit to `/chat`); `get_graph_changes` fine (761 tool calls cross-checks the `tools` total). **Headline:** the *original* control-plane-killing job **`19707fa1` (6306 entries, fat un-backfilled metadata still on disk)** now reads cleanly through `get_audit_bulk` — including the formerly-fatal `limit=5000` — with no 504/OOM. *(One cosmetic note: this session's cached MCP tool schema still advertises the old "max 500" — pre-redeploy connection; the live server clamps to 200, description-only, refreshes on reconnect.)*

### Remaining (deferred; none blocking)

- **Dev/prod metadata backfill not run** — existing `19707fa1` rows still carry fat `resolved_config` metadata. This is now only a **storage-weight** concern, not an OOM vector: with the bulk endpoints gone, **no read path materializes 5000 fat rows anymore** (the lean UI path strips at read time; the lean MCP path drops metadata server-side). Backfill is a cleanup, no longer a crash fix.
- **`IndexedDbService`** audit/chat/graph cache methods are now dead (no callers) but the Dexie DB still serves session `thread*` tables — trimming wants a schema-version bump. Low value.
- **Live tail** (§7-P3 step 2) — auto-refresh now just reloads the job list; per-stream keyset tail / follow-mode is future work.
- Unused timeline CSS (`.scrubber`/`.play-button`/…) — cosmetic.

### Files (all shipped on `develop`; P1 `86e8fd46`, P3 `83ba4a6e` + `1dcd9415`, P4 `e4244dfe` + `a5d93f71`, atop the deployed P0–P2)

New: `cockpit/src/app/core/services/{audit-trace,chat-trace}.service.ts`. Changed: `api.service.ts`, `data.service.ts` (+ `.spec`), `agent-activity.component.ts`, `chat-history.component.ts`, `timeline.component.ts`, `graph.service.ts`, `orchestrator/database/audit_store.py`, `orchestrator/main.py`. MCP migration adds: `orchestrator/mcp/client.py`, `orchestrator/mcp/server.py`, `orchestrator/database/mongodb.py` (legacy bulk methods removed), `tests/test_mcp.py` (+`TestAsyncCockpitClientAuditChatBulk`), `tests/test_audit_pagination.py` (+`TestChatPaginationContract`).

**P4 adds** (shipped in `e4244dfe`, follow-up `a5d93f71`): `src/core/archiver.py`, `orchestrator/services/formatters.py`, `cockpit/src/app/core/models/chat.model.ts`, `cockpit/src/assets/i18n/{en,de-DE}.json`; new `tests/test_chat_lean.py`, `cockpit/src/app/core/services/chat-trace.service.spec.ts`, `cockpit/src/app/views/chat-history/chat-history.component.spec.ts` (+ new cases in `tests/test_archiver_pg.py`).

---

## 1. Problem

The job debug dashboard (the "agent audit replay" view — CHAT HISTORY / AGENT ACTIVITY / REQUEST VIEWER panels + a step slider) **eagerly downloads the entire job** — all audit steps, all chat turns, all graph deltas — into IndexedDB before the view is usable, then windows it client-side with a slider. This:

- **Crashes on large jobs.** Opening the 6,386-step loop job `19707fa1` fires `GET /api/jobs/{id}/audit/bulk?offset=0&limit=5000`; the orchestrator materializes ~½ GB of JSON (see §2 measurements) and **OOM-kills the entire control plane** (both replicas, CrashLoopBackOff). The view shows "0 entries / No audit entries" after a 10 s gateway timeout (504/502).
- **Is slow even on normal jobs.** Every audit row ships ~130 kB of duplicated config (a field the UI never renders — see §2), so a few-hundred-step job downloads tens of MB when it should download a few hundred KB.
- **Re-fires the OOM every 15 s on running jobs.** Auto-refresh (`data.service.ts:482` `autoRefreshTick`) detects any new step and does `clearJob()` + full `loadJob()` — a complete re-download. A running loop job with auto-refresh on re-pulls ~½ GB every 15 seconds.

### Live measurements (from the companion OOM issue, main cluster 2026-06-27)

Job `19707fa1`, `agent_audit` (8,808 rows, 6,386 steps):

| column | avg | max | sum | rendered in UI? |
|---|---|---|---|---|
| `payload` | 292 B | 1,288 B | ~2.5 MB | yes (row + expanded) |
| `metadata` | **130 kB** | 130 kB | **476 MB** | **no — never displayed** |
| └ `metadata.resolved_config` | 127 kB | | | **no** |

The single heaviest field on the wire — `metadata.resolved_config`, ~127 kB stamped on every row — **is not shown anywhere in the UI** (confirmed: the row shows `step_number/step_type/node_name/latency/timestamp`; the expanded view shows `tool.*`, `llm.*`, `error.*`). We OOM the control plane shipping dead weight.

---

## 2. Current architecture (what's wrong, with refs)

### Frontend — eager download + slider window

- `DataService.fetchAndCacheJob` (`data.service.ts:520-592`): `clearJob()` then three `while (hasMore)` loops paging **audit, chat, graph** in `BULK_FETCH_SIZE = 5000` (`:39`) chunks into IndexedDB. Nothing renders until all three streams are fully cached.
- `loadWindow` (`:597-630`): audit is windowed to `WINDOW_SIZE = 1000` (`:37`) around the slider, **but chat and graph are loaded in full** (`:616-624` "Load ALL chat entries and graph deltas") — the in-memory hog for big jobs.
- The slider (`timeline.component.ts:77-87`) drives `setSliderIndex` (`data.service.ts:343`); `visibleAuditEntries`/`visibleChatEntries`/`visibleGraphDeltas` (`:164-223`) are **computed client-side** from `sliderIndex` + `currentTimestamp()`. Filter tabs (All/Messages/Tools/Errors) also filter **client-side** over the loaded window — no server round-trip.
- Bulk API callers: `api.service.ts` `getJobAuditBulk:381`, `getChatHistoryBulk:413`, `getGraphDeltasBulk:445` → `/api/jobs/{id}/{audit,chat,graph}/bulk` (`main.py:11834/11871/11908`).
- Auto-refresh (`data.service.ts:440-513`, 15 s): on any new step → `clearJob` + `loadJob` (full re-download).
- IndexedDB (`indexed-db.service.ts`, Dexie schema v4 `:17`): tables `auditEntries`/`chatEntries`/`graphDeltas`/`jobMetadata` (debug) + `threadCursors`/`threadMessages` (**sessions — out of scope, must survive**).

### Backend — bulk path ships fat rows

- `get_job_audit_bulk` (`audit_store.py:356`) selects via `_STITCH_CORE` (`:85-112`), which includes `f.metadata` and the full merged `payload`; `conn.fetch()` materializes the whole page; `_audit_row_to_doc` (`:115-144`) copies `metadata` onto the wire. With `limit=5000`, one call = the whole config-bearing pile.

---

## 3. What already exists server-side (the leverage)

This refactor is small because the hard backend parts are already written:

| Need | Already there |
|---|---|
| Paged audit with **server-side filter pushdown** | `get_job_audit` (`audit_store.py:204-289`) — page/offset + `filter_category` → `FILTER_MAPPINGS` (`:57`, `WHERE f.step_type = ANY($2)`), count, asc/desc, `hasMore`. Wired at `main.py:10863` `GET /api/jobs/{id}/audit`. |
| Counts (for filter-tab badges) | `get_audit_count` (`:291`), `get_audit_counts` (`:306`) |
| Time range (for jump/scrub markers) | `get_audit_time_range` (`:334`), `main.py:10952` |
| **Lazy single-record detail** | `get_request` (`:602`), `main.py:10917` `GET /api/requests/{id}` — what the Request Viewer already uses (`request.service.ts:53`) |
| **Keyset cursor** (for live tail) | `iter_tool_calls` (`:464`, `WHERE f.id > $2 ... LIMIT 1000`) — the pattern to generalize |

**The only true backend gap:** `_STITCH_CORE` selects `f.metadata` + the full payload, so even `get_job_audit` ships fat rows. We need a **lean projection** + a **per-step detail-by-id** endpoint.

---

## 4. Target architecture

A post-hoc (and live-tail) execution-trace inspector — same family as a CI log viewer, a Jaeger span list, or the DevTools network panel. The invariant: **render only what's on screen; fetch only the range you're looking at.**

| | Today | Target |
|---|---|---|
| Fetch | eager: all audit+chat+graph before first render | lazy: fetch the visible range on demand |
| Row payload | fat (~130 kB: metadata + full payload) | lean (~hundreds of bytes); heavy detail on click |
| Render | 1000-row client window driven by a global slider | incremental paged **infinite scroll** (as built — CDK virtual scroll deferred, §0) |
| Filter | client-side over downloaded data | server-side pushdown (already supported) |
| Live job | re-download everything every 15 s | keyset tail — append only `after_id` |
| Storage | Dexie cache + schema-versioning + invalidation tax | none for debug streams (optional slim cache later) |
| Panels | one global slider → synchronized "state as of N" | independent virtualized panels — no slider, no shared cursor, no replay (§6) |

### Lean list row vs. heavy detail (from the confirmed interfaces)

`AuditEntry` (`audit.model.ts:64-87`) already separates row-fields from expand-only fields. The projections map cleanly:

**Lean list row** (collapsed row + filtering): `step_number, step_type, node_name, timestamp, latency_ms, completed_at` (pending dot), `phase, phase_number, iteration`; for tool rows `tool.name, tool.success`; for llm rows `llm.model, llm.request_id` (the clickable link), `llm.has_tool_calls`/count; for error rows `error.type` + truncated `error.message`. Bounded `*_preview` fields may stay (they're short by design).

**Heavy detail** (lazy, on expand/select): `tool.arguments`, `tool.result`, `error.traceback`, `state`, full `llm` response content. Fetched per step via the new detail endpoint. The linked `llm_request` keeps loading via the existing `/api/requests/{id}`.

**`metadata`/`resolved_config`** belongs in **neither** projection — it's identical for every step and never rendered. If a job's resolved config is ever needed in the UI, expose it **once** via a job-level config view, not per step.

---

## 5. Design decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Navigation metaphor | **Remove all sliders** (global `timeline.component.ts:77` + graph-timeline's own `:103`); infinite scroll (as built — not CDK, §0) + filter tabs + an asc/desc **sort toggle** + phase markers | Demo-only widget; nobody uses it; debug-scoped, safe to delete. |
| 2 | Synchronized replay | **Removed entirely — not replaced** (owner: demo-only). Independent panels; the `request_id`→Request Viewer cross-link stays (lazy detail, not replay). | No real consumer; the replay windowing is what drives the eager load. |
| 3 | IndexedDB for debug streams | **Drop** `auditEntries`/`chatEntries`/`graphDeltas`/`jobMetadata` tables | A workaround for fat eager downloads; with lean server paging it's pure complexity (schema v4, `clearJob` dance, the chat id-shape cutover bug noted at `data.service.ts:521`). **Keep `threadCursors`/`threadMessages`** (sessions). Re-add a slim cache only if repeat-open latency annoys. |
| 4 | Paging model | **Offset** for v1 (finished jobs); **keyset** (`after_id`) for the live-tail path | Offset is simplest and `get_job_audit` already does it; offset drifts as rows append, so live tail needs keyset. |
| 5 | Filtering | **Server-side** via existing `filter_category` pushdown | Switching filter re-queries; never download-all-then-filter. |
| 6 | Detail loading | **Lazy per step** (generalize the Request Viewer pattern) | Row-click fetches that step's heavy fields + linked request on demand. |
| 7 | Search parity | Preserve via server filter + step/phase jump now; full-text over payloads later | The old "everything in memory" implicitly allowed client-side Ctrl-F; don't silently lose it. |
| 8 | Graph + chat | Same lazy+virtualized treatment | Graph scales with tool calls; chat is smaller (turns) but should follow the pattern. Graph already has its own independent slider (`graph-timeline.component.ts:103`) — the codebase is already drifting toward independent panels. **Amended P4 (§0.1): "chat is smaller" was wrong once tail-injection landed — a turn carries the whole re-injected context frame. Chat needed the full lean+detail split, not just paging.** |

---

## 6. Decided: the slider is removed entirely (no synchronized replay)

**Owner decision (2026-06-29): remove the slider outright.** It was built only to drive a demo video; nothing depends on it, and we do **not** want per-component sliders either. So this refactor drops *all* sliders — the global one (`timeline.component.ts:77-87`) **and** graph-timeline's own (`graph-timeline.component.ts:103-110`) — and does **not** replace the synchronized "replay to step N" semantic (`setSliderIndex(N)` filtering audit `≤ N`, chat `timestamp ≤ currentTimestamp(N)`, graph `toolCallIndex ≤ N`, `data.service.ts:164-223`) with anything. Panels scroll independently.

What stays: the existing **lazy cross-link** — clicking a step's `request_id` loads that request in the Request Viewer (`request.service.ts:53`). That's on-demand detail, not replay, and it's genuinely useful — keep it.

**Scope guard (owner): don't let slider removal bloat this feature.** The primary Agent Activity view drops the slider in Phase 2 regardless. If fully decoupling the *secondary* panels (the `/chat-history` route and `graph-timeline`, which currently read the shared `sliderIndex`/`currentTimestamp`) turns out to be heavy, that decoupling may split into a separate follow-up feature rather than expand this one — they can keep showing all entries unfiltered in the interim.

---

## 7. Implementation roadmap

Sequenced so value lands early and risk stays low. Each phase is independently shippable and **verified locally on k3d before commit** (per `CLAUDE.md` Plan→Develop→Verify gate).

### Phase 0 — Stop the bleeding (backend; ships alone) — *detailed in the companion issue* ✅ committed `7ea0d798`

Write-side fix from `docs/issues/audit_metadata_config_duplication_ooms_orchestrator.md`: stop persisting heavy job-level blobs (`resolved_config`, `config_override`, `datasources`, `repositories`) into per-row audit `metadata`; backfill existing partitions. This alone stops the crash and makes every job ~400× lighter, **with the current UI untouched** — so it is the immediate unblock and a hard prerequisite for nothing else (the UI refactor can proceed in parallel).

- **Files:** `src/api/dual_app.py` (+ mirror `src/api/app.py`) metadata build; backfill SQL per live partition.
- **Acceptance:** `GET /api/jobs/19707fa1/audit/bulk?limit=5000` returns < 5 MB (was ~½ GB); orchestrator RSS stays flat; no OOM; the existing dashboard opens the 6k job.

### Phase 1 — Lean projection + per-step detail (backend) ✅ done + k3d-verified, shipped `86e8fd46`

1. **Lean list projection** — add `_STITCH_LEAN` (or a `detail: bool = False` arg threaded through `get_job_audit`) that selects only render columns and projects a small payload (tool name/success, model, request_id, error type + truncated message, bounded previews) — **excluding** `metadata`, `tool.arguments`, `error.traceback`, `state`, full content. Keep the existing fat path as the non-default so MCP `get_audit_trail` and any other `get_job_audit` callers are unchanged.
2. **Per-step detail endpoint** — `GET /api/jobs/{id}/audit/{step_id}` → the full stitched doc for one step (heavy is fine for a single row). Backed by a `get_audit_step(job_id, step_id)` reusing `_STITCH_CORE` with `WHERE f.id = $2`.
3. *(Optional, enables Phase 3 live tail)* add `after_id` keyset param to the lean list query, mirroring `iter_tool_calls`.

- **Files:** `orchestrator/database/audit_store.py`, `orchestrator/main.py` (new detail route; `lean=` on the `/audit` route).
- **Acceptance:** lean rows average < 1 KB; `/audit/{step_id}` returns one full step; `get_audit_trail` MCP output byte-identical to before (fat path intact); 5000 lean rows < ~2 MB.

### Phase 2 — Agent Activity → virtual scroll (frontend; the core refactor) ✅ built + deployed to dev (as **infinite scroll**, not CDK — see §0)

1. Build a paging **CDK `cdk-virtual-scroll-viewport`** for Agent Activity backed by a `DataSource` that range-fetches lean rows from `/api/jobs/{id}/audit?offset&limit&filter` with a small in-memory LRU of pages (no IndexedDB).
2. Filter tabs → server re-query (drop `FILTER_STEP_TYPES` client filtering); tab badges from `get_audit_counts`.
3. Row expand/select → lazy `GET /audit/{step_id}` into the detail/Request Viewer; keep the existing `request_id` → `/api/requests/{id}` link (`request.service.ts:53`).
4. Remove the global slider entirely — no replacement cursor; panels scroll independently. Keep the existing `request_id` → `/api/requests/{id}` cross-link (lazy detail, not replay).
5. **Delete** for the audit stream: the slider (`timeline.component.ts` range input), `fetchAndCacheJob`'s audit loop, `loadWindow`'s audit windowing, `visibleAuditEntries` slider math, `getJobAuditBulk`, and the `auditEntries` Dexie table usage.

- **Files:** `data.service.ts`, `agent-activity.component.ts`, `timeline.component.ts`, `api.service.ts`, `indexed-db.service.ts`; new virtual-scroll datasource.
- **Acceptance (k3d, DevTools network open):** opening the 6,386-step job renders the first screen in < 1 s and issues **no 5000-row request** — only small range fetches as you scroll; switching filters re-queries server-side; resident audit entries bounded to the viewport+overscan (not 1000+); slider gone; `simple/` mobile layout + `/chat-history` route still build and run.

### Phase 3 — Graph + chat + live tail + dead-code removal ✅ chat/graph migration + slider removal + frontend **and backend** dead-code sweep done incl. **MCP migration + `/*/bulk` removal** (see §0); ⏳ only live-tail deferred

1. Apply the same lean+virtualized pattern to **graph** (`graph-timeline`) and **chat** (`/chat-history` route) streams; stop full-loading them in `loadWindow` (`:616-624`); **remove graph-timeline's own slider** (`:103-110`) and decouple both panels from the shared `sliderIndex`/`currentTimestamp`. *(Per §6 scope guard, this secondary-panel decoupling may split to a follow-up feature if heavy.)*
2. **Live tail:** replace `autoRefreshTick`'s `clearJob` + `loadJob` (`data.service.ts:482`) with a keyset poll (or SSE off the existing events infra) that fetches only `after_id` and **appends**; add follow-mode ("stick to bottom").
3. **Remove** the now-dead surface: ✅ **done (2026-06-29)** for the `/api/jobs/{id}/{audit,chat,graph}/bulk` endpoints + `get_job_audit_bulk`/`get_chat_history_bulk`/`get_graph_deltas_bulk` (`audit_store.py`) and the frontend `getJobAuditBulk`/`getChatHistoryBulk`/`getGraphDeltasBulk` (`api.service.ts`, removed in the §0 sweep). Prerequisite was the **MCP migration** (the only remaining backend-bulk consumer): `get_audit_bulk`→`/audit?lean=true`, `get_chat_bulk`→`/chat?offset&limit`. ⏳ still deferred: the `chatEntries`/`graphDeltas`/`jobMetadata` Dexie tables (bump Dexie schema version, keep `threadCursors`/`threadMessages`).

- **Acceptance:** ✅ bulk endpoints return 404 (OpenAPI confirms routes gone); no references to the deleted bulk methods remain (grep clean except retired `mongodb.py`); 303 backend audit/chat/mcp tests + ruff green; MCP tools verified live on k3d. ⏳ a *running* loop job tails new steps without a full re-download (verify no `clearJob` on tick) — part of deferred live-tail.

---

## 8. What gets deleted (the "dragging along for so long" payoff)

`getJobAuditBulk`/`getChatHistoryBulk`/`getGraphDeltasBulk` + callers · `fetchAndCacheJob` + `loadWindow` + the window/slider signals · the slider component · the audit/chat/graph half of `IndexedDbService` (schema v4 → bump) · the three `/*/bulk` endpoints + `get_*_bulk` store methods · the 15 s full-re-download auto-refresh. Net: less code, an entire class of cache-invalidation bugs gone, and a view that scales to 50k-step loop jobs instead of dying at 6k.

---

## 9. Risks & open questions

- **Secondary-panel decoupling** — `/chat-history` and `graph-timeline` read the shared `sliderIndex`/`currentTimestamp`; Phase 3 must re-point them to independent scroll. Per §6 this may split to a follow-up; Agent Activity drops the slider in Phase 2 regardless.
- **Other consumers of `DataService`** — `/chat-history` is a separate route on the same service; it must move onto the paging model in Phase 3, not break in Phase 2. **Pre-deletion check:** confirm the `simple/` mobile layout and sessions views don't import the debug components / Dexie debug tables before removing them.
- **Offset drift on live jobs** — Phase 2 uses offset paging; if used on a *running* job, appends shift offsets. Mitigated by Phase 3 keyset tail; until then, treat the live view as best-effort or gate virtual-scroll on terminal jobs.
- **Search regression** — server-side filter + jump must land with Phase 2 so we don't ship a view that's worse at "find the failing step" than the old client-side one.
- **MCP parity** — `get_audit_trail` and any other `get_job_audit` callers must keep the fat path; the lean projection is opt-in only.

---

## 10. Best-practices checklist

- [ ] List rows are lean; heavy detail is lazy and per-record.
- [ ] Filtering/counting/search push down to SQL, not the client.
- [ ] Rendering is virtualized (viewport + overscan), not windowed-in-memory.
- [ ] Live data tails incrementally (keyset), never re-downloads.
- [ ] Caching, if any, holds lean rows only and is invalidation-trivial.
- [ ] No endpoint can be asked for an unbounded materialization (cap page size; the `le=5000` ceiling drops once bulk is gone).
- [ ] Deletions verified against *all* consumers (debug dashboard, `/chat-history`, `simple/`, MCP) before removal.
