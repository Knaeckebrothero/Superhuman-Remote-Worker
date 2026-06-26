# Usage Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grow the Cockpit Admin → Usage page into a fused fleet-monitor + per-principal consumption dashboard (quantity-first, visibility-scoped) so an admin can see who/what is driving token + compute usage and how the fleet is doing.

**Architecture:** Backend adds one read method (`UsageLedger.query_grouped`) and one endpoint (`GET /api/usage/breakdown`) that aggregate the existing `usage_events` ledger by user / model / project, with cross-DB label enrichment in the orchestrator (auditdb aggregate + app-pool lookup merged in Python). Fleet/ops panels (jobs, throughput, agents) reuse **existing** endpoints (`/api/stats/jobs`, `/api/stats/daily`, `/api/stats/agents`) — zero new backend. The frontend grows the existing standalone `AdminUsageComponent` with new panels, a window selector, and a Grafana-style auto-refresh toggle; KPI trends come from two windowed `/api/usage` calls (no backend compare needed).

**Tech Stack:** Python 3.12 / FastAPI / asyncpg (orchestrator); Angular 21 standalone + signals + OnPush, vitest (cockpit); Postgres `usage_events` (srw-auditdb) + `users`/`projects` (app DB); k3d + Tilt for local deploy.

## Global Constraints

- **Branch/commits:** Work directly on `develop` (no feature branches). Commit per task. **Never push** without explicit user approval.
- **Test gate:** CI runs on Python **3.12** (local env may be noisy — 3.12 is the gate). `ruff` auto-runs on push and may rewrite formatting.
- **Quantity-first:** `cost_usd` is `0`/unpriced today (`usage_rates` empty). Cost columns render **"—"** when a row's cost is `0`/falsy; tokens & compute-hours are the headline. No rate-seeding / Edit-Rates UI this round.
- **Visibility (G5):** Reuse `_visibility_kwargs_for_stats(user)` server-side. A **non-admin breakdown is strictly scoped to `user_id = self`** (must NOT disclose co-project users). Admin-only panels (fleet status, agents-in-field KPI) are gated on `userService.currentUser()?.is_admin` in the frontend AND the admin-only endpoints (`/api/stats/agents`) server-side.
- **No new npm dependencies** (the cockpit dependency-audit + 2MB bundle-budget gates are strict). Charts are CSS/flex bars, no charting library.
- **Frontend build/test:** `cd cockpit && npx vitest run <spec>` for unit tests. A full `ng build` needs `npm install --no-save @monaco-editor/loader` first (pre-existing quirk).
- **Endpoint base:** cockpit services use `environment.apiUrl` (already `…/api`); append paths like `/usage/breakdown`.

---

### Task 1: `UsageLedger.query_grouped()` — aggregate the ledger by user/model/project

**Files:**
- Modify: `orchestrator/services/usage_ledger.py` (add method + a module constant on `UsageLedger`)
- Test: `tests/test_audit_store.py` (new methods in `class TestUsageLedger`)

**Interfaces:**
- Consumes: existing `UsageLedger(self._pool)`, `_uuid()` helper (same module).
- Produces: `async def query_grouped(self, *, from_ts: datetime, to_ts: datetime, group_by: str, owner_user_id: Optional[str]=None, visible_project_ids: Optional[Sequence[str]]=None, scope_project_id: Optional[str]=None) -> List[Dict[str, Any]]` returning rows `{"key": str, "unit": str, "quantity": float, "cost_usd": float, "events": int}`. Raises `ValueError` on an unsupported `group_by`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_audit_store.py` inside `class TestUsageLedger` (mirrors the existing `test_record_and_query_unpriced` style — `_audit_pool(pg_dsn)`, `UsageEvent`, `record_events`):

```python
    async def test_query_grouped_by_user_and_model(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            now = datetime.now(timezone.utc)
            ua, ub, pid = uuid4(), uuid4(), uuid4()
            def ev(uid, model, qty, unit, sid):
                return UsageEvent(category="llm", resource=model, quantity=qty,
                                  unit=unit, source="litellm", source_id=sid, ts=now,
                                  user_id=str(uid), project_id=str(pid))
            await ledger.record_events([
                ev(ua, "gemma", 100, "prompt-token", "a1"),
                ev(ua, "gemma", 30, "completion-token", "a1"),
                ev(ub, "opus", 200, "prompt-token", "b1"),
            ])
            window = dict(from_ts=now - timedelta(days=1), to_ts=now + timedelta(days=1))
            by_user = await ledger.query_grouped(group_by="user", **window)
            keys = {r["key"] for r in by_user}
            assert keys == {str(ua), str(ub)}
            ua_prompt = next(r for r in by_user if r["key"] == str(ua) and r["unit"] == "prompt-token")
            assert ua_prompt["quantity"] == 100.0 and ua_prompt["events"] == 1
            by_model = await ledger.query_grouped(group_by="model", **window)
            assert {r["key"] for r in by_model} == {"gemma", "opus"}

    async def test_query_grouped_nonadmin_scoped_to_self(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            now = datetime.now(timezone.utc)
            me, other, shared = uuid4(), uuid4(), uuid4()
            def ev(uid, sid):
                return UsageEvent(category="llm", resource="gemma", quantity=10,
                                  unit="prompt-token", source="litellm", source_id=sid,
                                  ts=now, user_id=str(uid), project_id=str(shared))
            await ledger.record_events([ev(me, "m1"), ev(other, "o1")])
            window = dict(from_ts=now - timedelta(days=1), to_ts=now + timedelta(days=1))
            # Non-admin (owner set) + a shared visible project must STILL only see self.
            rows = await ledger.query_grouped(
                group_by="user", owner_user_id=str(me),
                visible_project_ids=[str(shared)], **window)
            assert {r["key"] for r in rows} == {str(me)}

    async def test_query_grouped_rejects_bad_dimension(self, pg_dsn):
        async with _audit_pool(pg_dsn) as pool:
            ledger = UsageLedger(pool, UsageRates(None))
            now = datetime.now(timezone.utc)
            with pytest.raises(ValueError):
                await ledger.query_grouped(group_by="evil",
                    from_ts=now - timedelta(days=1), to_ts=now)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_audit_store.py::TestUsageLedger::test_query_grouped_by_user_and_model -v`
Expected: FAIL — `AttributeError: 'UsageLedger' object has no attribute 'query_grouped'`.

- [ ] **Step 3: Implement `query_grouped`**

In `orchestrator/services/usage_ledger.py`, add a module-level constant near the top and the method inside `class UsageLedger` (after `query_usage`):

```python
# Dimension → the usage_events column it groups on. The allow-list is also the
# validation guard: an unknown group_by raises rather than interpolating SQL.
_GROUP_COLS = {"user": "user_id", "model": "resource", "project": "project_id"}
```

```python
    async def query_grouped(
        self,
        *,
        from_ts: datetime,
        to_ts: datetime,
        group_by: str,
        owner_user_id: Optional[str] = None,
        visible_project_ids: Optional[Sequence[str]] = None,
        scope_project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Aggregate quantity/cost/events grouped by (dimension, unit).

        ``group_by`` ∈ {'user','model','project'} → user_id / resource / project_id.
        Rows with a NULL group key (unattributed fleet-key LLM traffic) are excluded
        so they don't collapse into one bogus bucket. Visibility differs from
        ``query_usage`` on purpose: a **non-admin** (``owner_user_id`` set) is scoped
        strictly to their OWN rows — a breakdown must not disclose other users who
        merely share a visible project. Admins pass no owner (full fleet) or a
        ``scope_project_id``.
        """
        if group_by not in _GROUP_COLS:
            raise ValueError(f"unsupported group_by: {group_by!r}")
        col = _GROUP_COLS[group_by]
        if self._pool is None:
            return []
        clauses = ["ts >= $1", "ts < $2", f"{col} IS NOT NULL"]
        params: List[Any] = [from_ts, to_ts]
        if owner_user_id is not None:
            params.append(_uuid(owner_user_id))
            clauses.append(f"user_id = ${len(params)}")  # strict self-scope
        elif scope_project_id is not None:
            params.append(_uuid(scope_project_id))
            clauses.append(f"project_id = ${len(params)}")
        sql = (
            f"SELECT {col} AS key, unit, SUM(quantity) AS quantity, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd, COUNT(*) AS events "
            f"FROM usage_events WHERE {' AND '.join(clauses)} "
            f"GROUP BY {col}, unit ORDER BY {col}"
        )
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
        except Exception:
            logger.warning("usage grouped query failed (non-fatal)", exc_info=True)
            return []
        return [
            {
                "key": str(r["key"]),
                "unit": r["unit"],
                "quantity": float(r["quantity"]) if r["quantity"] is not None else 0.0,
                "cost_usd": float(r["cost_usd"]),
                "events": r["events"],
            }
            for r in rows
        ]
```

`col` is never user input — it comes only from the `_GROUP_COLS` allow-list, so the f-string interpolation is safe.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_audit_store.py::TestUsageLedger -v`
Expected: PASS (all `TestUsageLedger` tests, including the 3 new ones).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/usage_ledger.py tests/test_audit_store.py
git commit -m "feat(usage): UsageLedger.query_grouped — by user/model/project, self-scoped for non-admins"
```

---

### Task 2: `GET /api/usage/breakdown` endpoint + label enrichment

**Files:**
- Modify: `orchestrator/main.py` (add helpers + the route immediately after `get_usage`, ~line 12466)
- Test: `tests/test_audit_store.py` (new `class TestBreakdownFold` for the pure fold/label-merge helpers)

**Interfaces:**
- Consumes: `UsageLedger.query_grouped` (Task 1); `require_approved_user`, `_visibility_kwargs_for_stats`, `_parse_utc_date`, `postgres_db.fetch` (existing in `main.py`).
- Produces: route `GET /api/usage/breakdown?group_by=&days=&from_date=&to_date=` returning
  `{"available": bool, "group_by": str, "from": str, "to": str, "rows": [{"key", "label", "is_admin"?, "units": {unit: {"quantity","cost_usd","events"}}, "events": int, "cost_usd": float}]}`.
  Module helpers `_fold_breakdown(rows)` and `_merge_labels(folded, labels)` (pure, unit-tested).

- [ ] **Step 1: Write the failing tests for the pure helpers**

Add to `tests/test_audit_store.py` (top-level, near the other test classes). These import the helpers from `orchestrator.main`:

```python
class TestBreakdownFold:
    """Pure (key, unit) → per-key folding + label merge used by /api/usage/breakdown."""

    def test_fold_groups_units_under_key(self):
        from orchestrator.main import _fold_breakdown
        rows = [
            {"key": "u1", "unit": "prompt-token", "quantity": 100.0, "cost_usd": 0.0, "events": 2},
            {"key": "u1", "unit": "completion-token", "quantity": 30.0, "cost_usd": 0.0, "events": 2},
            {"key": "u2", "unit": "prompt-token", "quantity": 50.0, "cost_usd": 0.0, "events": 1},
        ]
        folded = _fold_breakdown(rows)
        assert folded["u1"]["units"]["prompt-token"]["quantity"] == 100.0
        assert folded["u1"]["events"] == 4  # summed across units
        assert folded["u2"]["units"]["prompt-token"]["events"] == 1

    def test_merge_labels_falls_back_to_key(self):
        from orchestrator.main import _fold_breakdown, _merge_labels
        folded = _fold_breakdown([
            {"key": "u1", "unit": "prompt-token", "quantity": 1.0, "cost_usd": 0.0, "events": 1},
            {"key": "u2", "unit": "prompt-token", "quantity": 1.0, "cost_usd": 0.0, "events": 1},
        ])
        out = _merge_labels(folded, {"u1": {"label": "Alice", "is_admin": True}})
        by_key = {r["key"]: r for r in out}
        assert by_key["u1"]["label"] == "Alice" and by_key["u1"]["is_admin"] is True
        assert by_key["u2"]["label"] == "u2"  # unknown id → key as label
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_audit_store.py::TestBreakdownFold -v`
Expected: FAIL — `ImportError: cannot import name '_fold_breakdown'`.

- [ ] **Step 3: Implement the helpers + the route**

In `orchestrator/main.py`, add the pure helpers above the `get_usage` route:

```python
def _fold_breakdown(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fold flat (key, unit) aggregate rows into one object per key.

    Each key carries a ``units`` map plus key-level ``events``/``cost_usd`` totals.
    Order-preserving on first appearance of a key.
    """
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = r["key"]
        o = out.setdefault(k, {"key": k, "units": {}, "events": 0, "cost_usd": 0.0})
        o["units"][r["unit"]] = {
            "quantity": r["quantity"], "cost_usd": r["cost_usd"], "events": r["events"],
        }
        o["events"] += r["events"]
        o["cost_usd"] += r["cost_usd"]
    return out


def _merge_labels(
    folded: dict[str, dict[str, Any]], labels: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach a display label (+ optional is_admin) to each folded key.

    Unknown keys (deleted/aged-out referents — the ledger has no FKs) fall back to
    the raw key as the label. Sorted by total events desc (the leaderboard order).
    """
    out: list[dict[str, Any]] = []
    for k, o in folded.items():
        meta = labels.get(k, {})
        out.append({**o, "label": meta.get("label", k), "is_admin": meta.get("is_admin")})
    out.sort(key=lambda r: r["events"], reverse=True)
    return out


async def _usage_labels(group_by: str, keys: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve display labels for breakdown keys via an app-DB lookup (cross-DB).

    user → users.display_name (+ is_admin); project → projects.name; model → none
    (the key IS the label). Robust to the audit/app DB split — a separate query,
    merged in Python, NOT a SQL join.
    """
    if not keys or group_by == "model":
        return {}
    uids = [uuid.UUID(k) for k in keys]
    if group_by == "user":
        rows = await postgres_db.fetch(
            "SELECT id, display_name, is_admin FROM users WHERE id = ANY($1::uuid[])", uids
        )
        return {
            str(r["id"]): {"label": r["display_name"], "is_admin": r["is_admin"]}
            for r in rows
        }
    rows = await postgres_db.fetch(
        "SELECT id, name FROM projects WHERE id = ANY($1::uuid[])", uids
    )
    return {str(r["id"]): {"label": r["name"]} for r in rows}
```

Then the route (immediately after `get_usage`):

```python
@app.get("/api/usage/breakdown")
async def get_usage_breakdown(
    request: Request,
    group_by: str = Query(..., description="user | model | project"),
    days: int = Query(default=30, ge=1, le=365),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
) -> dict[str, Any]:
    """Per-(user|model|project) usage breakdown over a window (G5-scoped).

    Quantities by unit + key-level event/cost totals, labels enriched from the app
    DB. Non-admins are strictly self-scoped (see query_grouped). ``available=false``
    when the audit tier is off.
    """
    user = await require_approved_user(request, postgres_db)
    if group_by not in ("user", "model", "project"):
        raise HTTPException(status_code=400, detail=f"bad group_by: {group_by}")
    if usage_ledger is None or not usage_ledger.is_available:
        return {"available": False, "group_by": group_by, "rows": []}
    try:
        now = datetime.now(timezone.utc)
        to_ts = _parse_utc_date(to_date) if to_date else now
        from_ts = _parse_utc_date(from_date) if from_date else (to_ts - timedelta(days=days))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid date: {e}") from e
    vis = await _visibility_kwargs_for_stats(user)
    try:
        rows = await usage_ledger.query_grouped(
            from_ts=from_ts, to_ts=to_ts, group_by=group_by,
            owner_user_id=vis.get("owner_user_id"),
            visible_project_ids=vis.get("visible_project_ids"),
            scope_project_id=vis.get("scope_project_id"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    folded = _fold_breakdown(rows)
    labels = await _usage_labels(group_by, list(folded.keys()))
    return {
        "available": True,
        "group_by": group_by,
        "from": from_ts.isoformat(),
        "to": to_ts.isoformat(),
        "rows": _merge_labels(folded, labels),
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_audit_store.py::TestBreakdownFold -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/test_audit_store.py
git commit -m "feat(usage): GET /api/usage/breakdown — per user/model/project, app-DB label enrichment"
```

---

### Task 3: Frontend service — breakdown types + loaders

**Files:**
- Modify: `cockpit/src/app/core/services/admin-usage.service.ts`
- Test: `cockpit/src/app/core/services/admin-usage.service.spec.ts`

**Interfaces:**
- Consumes: `/api/usage/breakdown` (Task 2), existing `/api/usage`.
- Produces: `UsageBreakdownRow`, `UsageBreakdown` interfaces; `AdminUsageService.loadBreakdown(groupBy: 'user'|'model'|'project', days: number): void` writing a `breakdown` signal keyed by groupBy; `loadUsageWindow(days, fromIso?, toIso?)` returning an `Observable<UsageSummary>` (used by the KPI trend's two-window fetch).

- [ ] **Step 1: Write the failing test**

Add to `admin-usage.service.spec.ts` (follow the existing spec's `HttpTestingController` setup):

```typescript
  it('loadBreakdown populates the breakdown signal by groupBy', () => {
    service.loadBreakdown('user', 30);
    const req = httpMock.expectOne((r) => r.url.endsWith('/usage/breakdown'));
    expect(req.request.params.get('group_by')).toBe('user');
    expect(req.request.params.get('days')).toBe('30');
    req.flush({available: true, group_by: 'user', rows: [
      {key: 'u1', label: 'Alice', is_admin: true, events: 3, cost_usd: 0,
       units: {'prompt-token': {quantity: 100, cost_usd: 0, events: 2}}},
    ]});
    expect(service.breakdown('user')?.rows[0].label).toBe('Alice');
  });
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cockpit && npx vitest run src/app/core/services/admin-usage.service.spec.ts`
Expected: FAIL — `service.loadBreakdown is not a function`.

- [ ] **Step 3: Implement the service additions**

Append to `admin-usage.service.ts`:

```typescript
export interface UsageUnitAgg { quantity: number; cost_usd: number; events: number; }
export interface UsageBreakdownRow {
  key: string;
  label: string;
  is_admin?: boolean | null;
  events: number;
  cost_usd: number;
  units: Record<string, UsageUnitAgg>;
}
export interface UsageBreakdown {
  available: boolean;
  group_by: 'user' | 'model' | 'project';
  from?: string;
  to?: string;
  rows: UsageBreakdownRow[];
}
export type BreakdownDim = 'user' | 'model' | 'project';
```

Add inside `AdminUsageService`:

```typescript
  private readonly breakdowns = signal<Partial<Record<BreakdownDim, UsageBreakdown>>>({});
  breakdown(dim: BreakdownDim): UsageBreakdown | null { return this.breakdowns()[dim] ?? null; }

  loadBreakdown(groupBy: BreakdownDim, days = 30): void {
    const params = new HttpParams().set('group_by', groupBy).set('days', String(days));
    this.http
      .get<UsageBreakdown>(`${this.baseUrl}/usage/breakdown`, {params})
      .pipe(catchError(() => of({available: false, group_by: groupBy, rows: []} as UsageBreakdown)))
      .subscribe((res) => this.breakdowns.update((m) => ({...m, [groupBy]: res})));
  }

  /** One-shot windowed fetch (used by the KPI trend's current-vs-previous pair). */
  loadUsageWindow(days: number, fromIso?: string, toIso?: string) {
    let params = new HttpParams().set('days', String(days));
    if (fromIso) params = params.set('from_date', fromIso);
    if (toIso) params = params.set('to_date', toIso);
    return this.http
      .get<UsageSummary>(`${this.baseUrl}/usage`, {params})
      .pipe(catchError(() => of(EMPTY)));
  }
```

- [ ] **Step 4: Run to verify pass**

Run: `cd cockpit && npx vitest run src/app/core/services/admin-usage.service.spec.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cockpit/src/app/core/services/admin-usage.service.ts cockpit/src/app/core/services/admin-usage.service.spec.ts
git commit -m "feat(usage): AdminUsageService breakdown loaders + windowed fetch"
```

---

### Task 4: Window selector + Grafana-style auto-refresh shell

**Files:**
- Modify: `cockpit/src/app/views/admin/usage/admin-usage.component.ts`
- Test: add `cockpit/src/app/views/admin/usage/admin-usage.component.spec.ts`

**Interfaces:**
- Produces: on the component — `refreshIntervalMs = signal<number>(0)` (0 = Off), `readonly refreshOptions = [{label:'Off',ms:0},{label:'10s',ms:10000},{label:'30s',ms:30000},{label:'1m',ms:60000}]`, `setRefresh(ms: number): void`, and a `reloadAll(): void` that all panels' loaders are funneled through. An Angular `effect` (re)arms a `setInterval` whenever `refreshIntervalMs()` changes and clears it on Off / destroy.

- [ ] **Step 1: Write the failing test**

Create `admin-usage.component.spec.ts`:

```typescript
import {TestBed} from '@angular/core/testing';
import {provideHttpClient} from '@angular/common/http';
import {provideHttpClientTesting} from '@angular/common/http/testing';
import {AdminUsageComponent} from './admin-usage.component';

describe('AdminUsageComponent refresh shell', () => {
  beforeEach(() => TestBed.configureTestingModule({
    imports: [AdminUsageComponent],
    providers: [provideHttpClient(), provideHttpClientTesting()],
  }));

  it('setRefresh updates the interval signal', () => {
    const fixture = TestBed.createComponent(AdminUsageComponent);
    const c = fixture.componentInstance;
    expect(c.refreshIntervalMs()).toBe(0);
    c.setRefresh(30000);
    expect(c.refreshIntervalMs()).toBe(30000);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cockpit && npx vitest run src/app/views/admin/usage/admin-usage.component.spec.ts`
Expected: FAIL — `setRefresh is not a function`.

- [ ] **Step 3: Implement the refresh shell**

In `admin-usage.component.ts`, add imports `effect, OnDestroy, signal` (extend the existing `@angular/core` import) and on the class:

```typescript
  readonly refreshOptions = [
    {label: 'Off', ms: 0}, {label: '10s', ms: 10000},
    {label: '30s', ms: 30000}, {label: '1m', ms: 60000},
  ] as const;
  readonly refreshIntervalMs = signal<number>(0);
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    effect(() => {
      const ms = this.refreshIntervalMs();
      if (this.timer) { clearInterval(this.timer); this.timer = null; }
      if (ms > 0) this.timer = setInterval(() => this.reloadAll(), ms);
    });
  }

  setRefresh(ms: number): void { this.refreshIntervalMs.set(ms); }

  /** Single funnel every panel's loader goes through (used by auto-refresh + window change). */
  reloadAll(): void {
    const d = this.windowDays();
    this.usage.loadUsage(d);
  }

  ngOnDestroy(): void { if (this.timer) clearInterval(this.timer); }
```

Change `setWindow` to route through `reloadAll`, and add `OnDestroy` to the class `implements`:

```typescript
  setWindow(days: number): void { this.windowDays.set(days); this.reloadAll(); }
```

Add the refresh control to the template toolbar (beside the existing window chips, inside `.usage-toolbar`):

```html
            <div class="refresh-control">
              @for (o of refreshOptions; track o.ms) {
                <button type="button" class="filter-chip"
                  [class.active]="refreshIntervalMs() === o.ms" (click)="setRefresh(o.ms)">
                  {{ o.label }}
                </button>
              }
            </div>
```

- [ ] **Step 4: Run to verify pass**

Run: `cd cockpit && npx vitest run src/app/views/admin/usage/admin-usage.component.spec.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cockpit/src/app/views/admin/usage/admin-usage.component.ts cockpit/src/app/views/admin/usage/admin-usage.component.spec.ts
git commit -m "feat(usage): window + Grafana-style auto-refresh toggle shell"
```

---

### Task 5: KPI row (tokens / compute-hours / events / jobs / agents-in-field)

**Files:**
- Modify: `cockpit/src/app/views/admin/usage/admin-usage.component.ts`
- Test: `cockpit/src/app/views/admin/usage/admin-usage.component.spec.ts`

**Interfaces:**
- Consumes: `usage.usage()` (existing summary), `ApiService.getJobStatistics()` + `getAgentStatistics()` (existing — `cockpit/src/app/core/services/api.service.ts`), `UserService.currentUser()` (`cockpit/src/app/core/services/user.service.ts`).
- Produces: computed signals `tokensTotal()`, `computeHours()`, `eventsTotal()`, plus `jobStats` / `agentStats` signals; KPI cards in the template. Agents-in-field card rendered only when `isAdmin()`.

- [ ] **Step 1: Write the failing test**

Add to `admin-usage.component.spec.ts`:

```typescript
  it('tokensTotal sums prompt + completion token quantities', () => {
    const fixture = TestBed.createComponent(AdminUsageComponent);
    const c = fixture.componentInstance;
    (c as any).usage.usage.set({available: true, total_cost_usd: 0, by_category: [
      {category: 'llm', unit: 'prompt-token', quantity: 100, cost_usd: 0, events: 1},
      {category: 'llm', unit: 'completion-token', quantity: 25, cost_usd: 0, events: 1},
      {category: 'compute', unit: 'vcpu-hour', quantity: 2, cost_usd: 0, events: 1},
    ]});
    expect(c.tokensTotal()).toBe(125);
    expect(c.computeHours()).toBe(2);
  });
```

- [ ] **Step 2: Run to verify fail**

Run: `cd cockpit && npx vitest run src/app/views/admin/usage/admin-usage.component.spec.ts`
Expected: FAIL — `c.tokensTotal is not a function`.

- [ ] **Step 3: Implement KPI computeds + cards**

Inject `ApiService` and `UserService` and add computeds:

```typescript
  private readonly api = inject(ApiService);
  private readonly users = inject(UserService);
  readonly isAdmin = computed(() => this.users.currentUser()?.is_admin === true);

  readonly jobStats = signal<JobStatistics | null>(null);
  readonly agentStats = signal<AgentStatistics | null>(null);

  private qty(unit: string): number {
    return (this.summary()?.by_category ?? [])
      .filter((r) => r.unit === unit).reduce((s, r) => s + r.quantity, 0);
  }
  readonly tokensTotal = computed(() => this.qty('prompt-token') + this.qty('completion-token'));
  readonly computeHours = computed(() => this.qty('vcpu-hour') + this.qty('gib-hour'));
  readonly eventsTotal = computed(() =>
    (this.summary()?.by_category ?? []).reduce((s, r) => s + r.events, 0));
```

Extend `reloadAll()` to also pull stats:

```typescript
    this.api.getJobStatistics().subscribe((s) => this.jobStats.set(s));
    if (this.isAdmin()) this.api.getAgentStatistics().subscribe((s) => this.agentStats.set(s));
```

Add a KPI row to the template above the existing toolbar/table (use existing card styling conventions; `fmtQty` already exists):

```html
        <section class="kpi-row">
          <div class="kpi-card"><span class="kpi-label">Tokens</span>
            <span class="kpi-value">{{ fmtQty(tokensTotal()) }}</span></div>
          <div class="kpi-card"><span class="kpi-label">Compute-hours</span>
            <span class="kpi-value">{{ fmtQty(computeHours()) }}</span></div>
          <div class="kpi-card"><span class="kpi-label">Events</span>
            <span class="kpi-value">{{ fmtQty(eventsTotal()) }}</span></div>
          <div class="kpi-card"><span class="kpi-label">Jobs completed</span>
            <span class="kpi-value">{{ fmtQty(jobStats()?.completed ?? 0) }}</span></div>
          @if (isAdmin()) {
            <div class="kpi-card"><span class="kpi-label">Agents in-field</span>
              <span class="kpi-value">{{ agentStats()?.working ?? 0 }}</span></div>
          }
        </section>
```

Add minimal `.kpi-row`/`.kpi-card`/`.kpi-label`/`.kpi-value` styles to the `styles` array (flex row, gap, card border — keep modest per the bundle budget). Import `JobStatistics`, `AgentStatistics` from `../../../core/models/api.model` and `ApiService`/`UserService` from core services.

- [ ] **Step 4: Run to verify pass**

Run: `cd cockpit && npx vitest run src/app/views/admin/usage/admin-usage.component.spec.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cockpit/src/app/views/admin/usage/
git commit -m "feat(usage): KPI row — tokens/compute/events/jobs + admin agents-in-field"
```

---

### Task 6: Consumption-by-user leaderboard

**Files:**
- Modify: `cockpit/src/app/views/admin/usage/admin-usage.component.ts`
- Test: `cockpit/src/app/views/admin/usage/admin-usage.component.spec.ts`

**Interfaces:**
- Consumes: `AdminUsageService.loadBreakdown('user', days)` + `breakdown('user')` (Task 3).
- Produces: `userRows()` computed mapping `UsageBreakdownRow[]` → `{label, role, prompt, completion, compute, events, share}`; a leaderboard table; share = events / maxEvents.

- [ ] **Step 1: Write the failing test**

```typescript
  it('userRows derives role and per-unit columns with a share fraction', () => {
    const fixture = TestBed.createComponent(AdminUsageComponent);
    const c = fixture.componentInstance;
    (c as any).usage.breakdown = () => ({available: true, group_by: 'user', rows: [
      {key: 'u1', label: 'Alice', is_admin: true, events: 4, cost_usd: 0, units: {
        'prompt-token': {quantity: 100, cost_usd: 0, events: 2},
        'completion-token': {quantity: 30, cost_usd: 0, events: 2}}},
      {key: 'u2', label: 'Bob', is_admin: false, events: 2, cost_usd: 0, units: {
        'vcpu-hour': {quantity: 1.5, cost_usd: 0, events: 2}}},
    ]});
    const rows = c.userRows();
    expect(rows[0].role).toBe('Admin');
    expect(rows[0].prompt).toBe(100);
    expect(rows[0].share).toBe(1);     // max events
    expect(rows[1].share).toBe(0.5);
  });
```

- [ ] **Step 2: Run to verify fail.** `cd cockpit && npx vitest run src/app/views/admin/usage/admin-usage.component.spec.ts` → FAIL `c.userRows is not a function`.

- [ ] **Step 3: Implement.**

```typescript
  readonly userRows = computed(() => {
    const rows = this.usage.breakdown('user')?.rows ?? [];
    const max = Math.max(1, ...rows.map((r) => r.events));
    return rows.map((r) => ({
      label: r.label,
      role: r.is_admin ? 'Admin' : 'User',
      prompt: r.units['prompt-token']?.quantity ?? 0,
      completion: r.units['completion-token']?.quantity ?? 0,
      compute: (r.units['vcpu-hour']?.quantity ?? 0) + (r.units['gib-hour']?.quantity ?? 0),
      events: r.events,
      cost: r.cost_usd,
      share: r.events / max,
    }));
  });
```

Add `this.usage.loadBreakdown('user', this.windowDays());` to `reloadAll()`. Add the leaderboard table to the template (header label depends on admin: "Consumption by user" for admin, "My consumption" for non-admin via `isAdmin()`), with a share bar `[style.width.%]="r.share * 100"` and cost cell rendering `fmtCost(r.cost)` only when `r.cost` else `—`.

- [ ] **Step 4: Run to verify pass.** Same vitest command → PASS.

- [ ] **Step 5: Commit.**

```bash
git add cockpit/src/app/views/admin/usage/
git commit -m "feat(usage): consumption-by-user leaderboard (admin all / non-admin self)"
```

---

### Task 7: By-model + By-project breakdown tables

**Files:**
- Modify: `cockpit/src/app/views/admin/usage/admin-usage.component.ts`
- Test: `cockpit/src/app/views/admin/usage/admin-usage.component.spec.ts`

**Interfaces:**
- Consumes: `loadBreakdown('model'|'project', days)` + `breakdown(...)`.
- Produces: `modelRows()` / `projectRows()` computeds → `{label, prompt, completion, events, cost}` (model) and `{label, tokens, compute, events, cost}` (project).

- [ ] **Step 1: Write the failing test**

```typescript
  it('modelRows lists per-model token columns', () => {
    const fixture = TestBed.createComponent(AdminUsageComponent);
    const c = fixture.componentInstance;
    (c as any).usage.breakdown = (dim: string) => dim === 'model' ? ({available: true,
      group_by: 'model', rows: [{key: 'gemma', label: 'gemma', events: 2, cost_usd: 0,
      units: {'prompt-token': {quantity: 100, cost_usd: 0, events: 1},
              'completion-token': {quantity: 20, cost_usd: 0, events: 1}}}]}) : null;
    expect(c.modelRows()[0].prompt).toBe(100);
    expect(c.modelRows()[0].label).toBe('gemma');
  });
```

- [ ] **Step 2: Run to verify fail** → FAIL `c.modelRows is not a function`.

- [ ] **Step 3: Implement** the two computeds (model + project), add `loadBreakdown('model'|'project', d)` to `reloadAll()`, and add two tables to the template (project table only when `isAdmin()` or scoped to the user's visible projects — the endpoint already scopes it):

```typescript
  readonly modelRows = computed(() => (this.usage.breakdown('model')?.rows ?? []).map((r) => ({
    label: r.label,
    prompt: r.units['prompt-token']?.quantity ?? 0,
    completion: r.units['completion-token']?.quantity ?? 0,
    events: r.events, cost: r.cost_usd,
  })));
  readonly projectRows = computed(() => (this.usage.breakdown('project')?.rows ?? []).map((r) => ({
    label: r.label,
    tokens: (r.units['prompt-token']?.quantity ?? 0) + (r.units['completion-token']?.quantity ?? 0),
    compute: (r.units['vcpu-hour']?.quantity ?? 0) + (r.units['gib-hour']?.quantity ?? 0),
    events: r.events, cost: r.cost_usd,
  })));
```

- [ ] **Step 4: Run to verify pass** → PASS.

- [ ] **Step 5: Commit.**

```bash
git add cockpit/src/app/views/admin/usage/
git commit -m "feat(usage): by-model + by-project breakdown tables"
```

---

### Task 8: Throughput chart + Fleet-status panel (admin)

**Files:**
- Modify: `cockpit/src/app/views/admin/usage/admin-usage.component.ts`
- Test: `cockpit/src/app/views/admin/usage/admin-usage.component.spec.ts`

**Interfaces:**
- Consumes: `ApiService.getDailyStatistics(days)` → `DailyStatistics[]`, `agentStats()` (Task 5).
- Produces: `dailyBars()` computed → `{date, completed, height}` (height = completed/maxCompleted*100); a fleet-status list mapping `AgentStatistics` buckets. Both reuse stats endpoints; chart is CSS bars (no library).

- [ ] **Step 1: Write the failing test**

```typescript
  it('dailyBars scales bar height to the busiest day', () => {
    const fixture = TestBed.createComponent(AdminUsageComponent);
    const c = fixture.componentInstance;
    (c as any).daily.set([
      {date: '2026-06-24', jobs_created: 0, jobs_completed: 5, jobs_failed: 0, jobs_cancelled: 0},
      {date: '2026-06-25', jobs_created: 0, jobs_completed: 10, jobs_failed: 0, jobs_cancelled: 0},
    ]);
    const bars = c.dailyBars();
    expect(bars[1].height).toBe(100);
    expect(bars[0].height).toBe(50);
  });
```

- [ ] **Step 2: Run to verify fail** → FAIL `c.daily.set is not a function`.

- [ ] **Step 3: Implement**

```typescript
  readonly daily = signal<DailyStatistics[]>([]);
  readonly dailyBars = computed(() => {
    const d = this.daily();
    const max = Math.max(1, ...d.map((x) => x.jobs_completed));
    return d.map((x) => ({date: x.date, completed: x.jobs_completed,
      height: (x.jobs_completed / max) * 100}));
  });
```

Add to `reloadAll()`: `this.api.getDailyStatistics(this.windowDays()).subscribe((d) => this.daily.set(d));`. Import `DailyStatistics`. Template: a `.throughput` panel of `@for (b of dailyBars())` flex bars with `[style.height.%]="b.height"`, and an admin-only `.fleet-status` block listing `agentStats()` buckets mapped to labels (working→"In-field", ready→"Idle", booting→"Standing by", offline+failed→"Signal lost").

- [ ] **Step 4: Run to verify pass** → PASS.

- [ ] **Step 5: Commit.**

```bash
git add cockpit/src/app/views/admin/usage/
git commit -m "feat(usage): throughput chart + admin fleet-status panel"
```

---

### Task 9: Deploy to k3d and eyeball

**Files:** none (deploy + manual verification)

- [ ] **Step 1: Run the full backend + frontend test suites once**

Run: `python -m pytest tests/test_audit_store.py -v && cd cockpit && npx vitest run src/app/views/admin/usage src/app/core/services/admin-usage.service.spec.ts`
Expected: all green.

- [ ] **Step 2: Build + deploy to local k3d via Tilt**

If `tilt up` is already running it rebuilds on the commits above. Otherwise:
Run: `tilt up` (k3d cluster `srw`, registry `:5005` — the local dev stack). Wait for orchestrator + cockpit resources green in the Tilt UI.

- [ ] **Step 3: Open the page and verify**

Navigate to the cockpit (the dev URL Tilt prints), log in (seeds the first admin user), go to **Admin → Usage**. Confirm: KPI row populates; window chips (7/30/90d) re-query; the auto-refresh toggle (Off/10s/30s/1m) re-polls; the consumption-by-user leaderboard, by-model, by-project, by-category tables render; throughput bars + fleet-status (admin) show. Cost cells read "—" (unpriced). If the audit tier is off, panels show the metering-disabled/empty states rather than erroring.

- [ ] **Step 4: Capture a screenshot for the user to react to**

Use the browser tooling (serve nothing — it's the live cockpit) to screenshot the rendered page. This is the artifact the user iterates from (the whole point: see it, then decide which panels stay/grow/go).

- [ ] **Step 5: Commit any deploy-manifest/image-tag changes only if the repo's deploy flow requires them** (dev uses `sha-XXX` tags via CI; do not hand-edit prod tags). Otherwise nothing to commit.

---

## Self-Review

**Spec coverage** (against `docs/features/usage_dashboard.md`):
- KPI row (tokens/compute/jobs/agents-in-field, admin-gated) → Task 5. ✅
- Throughput + fleet-status (admin-only row) → Task 8. ✅
- Consumption-by-user leaderboard (admin all / non-admin self) → Task 6 + the strict self-scope in Task 1. ✅
- By-model / by-category / by-project → Task 7 (by-category preserved from the existing page). ✅
- Window chips + Grafana auto-refresh (debug pattern) → Task 4. ✅
- Quantity-first / cost "—" → Global Constraints + per-table cost cells (Tasks 6–7). ✅
- Visibility-scoped, one page → reused `_visibility_kwargs_for_stats` (Task 2) + frontend `isAdmin()` gating (Task 5). ✅
- Additive `/api/usage` (back-compat) → new `/api/usage/breakdown`, `/api/usage` untouched. ✅
- Cross-DB enrichment (not a SQL join) → `_usage_labels` app-pool lookup merged in Python (Task 2). ✅
- Deferred (provider, CSV, edit-rates, per-job LLM, live RPM) → **not** in any task, by design. ✅

**Placeholder scan:** No TBD/TODO; every code step shows real code; test code is concrete. The only "investigate" is Task 9 Step 5 (deploy-flow conditional), which is an environment fact, not code.

**Type consistency:** `UsageBreakdown`/`UsageBreakdownRow`/`BreakdownDim` defined in Task 3 and consumed in Tasks 6–7; `query_grouped` row shape (`key/unit/quantity/cost_usd/events`) produced in Task 1 and folded in Task 2; `JobStatistics`/`AgentStatistics`/`DailyStatistics` are existing model types (confirmed in `api.model.ts`). `reloadAll()` is introduced in Task 4 and extended (not redefined) in Tasks 5–8.

**Known v1 trims (intentional, noted for the reviewer):** the leaderboard's literal "agents-per-user" count column is deferred (needs a per-user jobs count query) — v1 shows role + events + share; KPI trend deltas use the windowed two-call pattern and can be added as a thin enhancement if the rendered page wants them.
</content>
