# Debug audit view refactor — from "download the whole job" to a windowed trace inspector

**Status:** **Proposed — design + implementation roadmap.** 2026-06-29. **Slider + synchronized replay: decided — removed entirely (owner: demo-only).** **Phase 0 ✅ committed (`7ea0d798`); Phase 1 (lean projection + `/audit/step/{id}` + `?lean=`) ✅ done + k3d-verified, uncommitted.** The crash that motivated this is tracked + root-caused in the companion issue `docs/issues/audit_metadata_config_duplication_ooms_orchestrator.md` (the write-side OOM fix is **Phase 0** here). The key enabler: most of the server-side primitives this refactor needs **already exist** (`get_job_audit` is paged + filtered, `get_request` is lazy-detail, `iter_tool_calls` is keyset) — so this is largely a **frontend deletion + one lean projection**, not a rewrite.
**Component:** Cockpit debug dashboard (`cockpit/src/app/debug/**`, `cockpit/src/app/core/services/data.service.ts`, `indexed-db.service.ts`, `api.service.ts`) · audit read path (`orchestrator/database/audit_store.py`, `orchestrator/main.py` `/api/jobs/{id}/audit*`).
**Related:** `docs/issues/audit_metadata_config_duplication_ooms_orchestrator.md` (P0 root cause) · memory topics `project_self_improvement_loop`, `project_loop_repo_compounding` (loop jobs run the most steps → most exposed) · `project_cross_pod_checkpointer_d3` (separate checkpoint-blob bloat).

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
| Render | 1000-row client window driven by a global slider | virtualized viewport (~30 visible rows) via Angular CDK |
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
| 1 | Navigation metaphor | **Remove all sliders** (global `timeline.component.ts:77` + graph-timeline's own `:103`); virtual scroll + "jump to start/end/step N" + filter tabs + phase markers on the scrollbar | Demo-only widget; nobody uses it; debug-scoped, safe to delete. |
| 2 | Synchronized replay | **Removed entirely — not replaced** (owner: demo-only). Independent panels; the `request_id`→Request Viewer cross-link stays (lazy detail, not replay). | No real consumer; the replay windowing is what drives the eager load. |
| 3 | IndexedDB for debug streams | **Drop** `auditEntries`/`chatEntries`/`graphDeltas`/`jobMetadata` tables | A workaround for fat eager downloads; with lean server paging it's pure complexity (schema v4, `clearJob` dance, the chat id-shape cutover bug noted at `data.service.ts:521`). **Keep `threadCursors`/`threadMessages`** (sessions). Re-add a slim cache only if repeat-open latency annoys. |
| 4 | Paging model | **Offset** for v1 (finished jobs); **keyset** (`after_id`) for the live-tail path | Offset is simplest and `get_job_audit` already does it; offset drifts as rows append, so live tail needs keyset. |
| 5 | Filtering | **Server-side** via existing `filter_category` pushdown | Switching filter re-queries; never download-all-then-filter. |
| 6 | Detail loading | **Lazy per step** (generalize the Request Viewer pattern) | Row-click fetches that step's heavy fields + linked request on demand. |
| 7 | Search parity | Preserve via server filter + step/phase jump now; full-text over payloads later | The old "everything in memory" implicitly allowed client-side Ctrl-F; don't silently lose it. |
| 8 | Graph + chat | Same lazy+virtualized treatment | Graph scales with tool calls; chat is smaller (turns) but should follow the pattern. Graph already has its own independent slider (`graph-timeline.component.ts:103`) — the codebase is already drifting toward independent panels. |

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

### Phase 1 — Lean projection + per-step detail (backend) ✅ done + k3d-verified (uncommitted)

1. **Lean list projection** — add `_STITCH_LEAN` (or a `detail: bool = False` arg threaded through `get_job_audit`) that selects only render columns and projects a small payload (tool name/success, model, request_id, error type + truncated message, bounded previews) — **excluding** `metadata`, `tool.arguments`, `error.traceback`, `state`, full content. Keep the existing fat path as the non-default so MCP `get_audit_trail` and any other `get_job_audit` callers are unchanged.
2. **Per-step detail endpoint** — `GET /api/jobs/{id}/audit/{step_id}` → the full stitched doc for one step (heavy is fine for a single row). Backed by a `get_audit_step(job_id, step_id)` reusing `_STITCH_CORE` with `WHERE f.id = $2`.
3. *(Optional, enables Phase 3 live tail)* add `after_id` keyset param to the lean list query, mirroring `iter_tool_calls`.

- **Files:** `orchestrator/database/audit_store.py`, `orchestrator/main.py` (new detail route; `lean=` on the `/audit` route).
- **Acceptance:** lean rows average < 1 KB; `/audit/{step_id}` returns one full step; `get_audit_trail` MCP output byte-identical to before (fat path intact); 5000 lean rows < ~2 MB.

### Phase 2 — Agent Activity → virtual scroll (frontend; the core refactor)

1. Build a paging **CDK `cdk-virtual-scroll-viewport`** for Agent Activity backed by a `DataSource` that range-fetches lean rows from `/api/jobs/{id}/audit?offset&limit&filter` with a small in-memory LRU of pages (no IndexedDB).
2. Filter tabs → server re-query (drop `FILTER_STEP_TYPES` client filtering); tab badges from `get_audit_counts`.
3. Row expand/select → lazy `GET /audit/{step_id}` into the detail/Request Viewer; keep the existing `request_id` → `/api/requests/{id}` link (`request.service.ts:53`).
4. Remove the global slider entirely — no replacement cursor; panels scroll independently. Keep the existing `request_id` → `/api/requests/{id}` cross-link (lazy detail, not replay).
5. **Delete** for the audit stream: the slider (`timeline.component.ts` range input), `fetchAndCacheJob`'s audit loop, `loadWindow`'s audit windowing, `visibleAuditEntries` slider math, `getJobAuditBulk`, and the `auditEntries` Dexie table usage.

- **Files:** `data.service.ts`, `agent-activity.component.ts`, `timeline.component.ts`, `api.service.ts`, `indexed-db.service.ts`; new virtual-scroll datasource.
- **Acceptance (k3d, DevTools network open):** opening the 6,386-step job renders the first screen in < 1 s and issues **no 5000-row request** — only small range fetches as you scroll; switching filters re-queries server-side; resident audit entries bounded to the viewport+overscan (not 1000+); slider gone; `simple/` mobile layout + `/chat-history` route still build and run.

### Phase 3 — Graph + chat + live tail + dead-code removal

1. Apply the same lean+virtualized pattern to **graph** (`graph-timeline`) and **chat** (`/chat-history` route) streams; stop full-loading them in `loadWindow` (`:616-624`); **remove graph-timeline's own slider** (`:103-110`) and decouple both panels from the shared `sliderIndex`/`currentTimestamp`. *(Per §6 scope guard, this secondary-panel decoupling may split to a follow-up feature if heavy.)*
2. **Live tail:** replace `autoRefreshTick`'s `clearJob` + `loadJob` (`data.service.ts:482`) with a keyset poll (or SSE off the existing events infra) that fetches only `after_id` and **appends**; add follow-mode ("stick to bottom").
3. **Remove** the now-dead surface: `/api/jobs/{id}/{audit,chat,graph}/bulk` endpoints (`main.py:11834/11871/11908`), `get_job_audit_bulk`/`get_chat_history_bulk`/`get_graph_deltas_bulk` (`audit_store.py`), `getChatHistoryBulk`/`getGraphDeltasBulk` (`api.service.ts`), and the `chatEntries`/`graphDeltas`/`jobMetadata` Dexie tables (bump Dexie schema version, keep `threadCursors`/`threadMessages`).

- **Acceptance:** a *running* loop job tails new steps without a full re-download (verify no `clearJob` on tick); bulk endpoints return 404; cockpit build + `vitest` + `pytest tests/` green; no references to the deleted bulk methods remain.

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
