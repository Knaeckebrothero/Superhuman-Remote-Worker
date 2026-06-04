# OpenRouter System-Provider Routing — Verification Runbook

Verifies the two fixes for the 2026-06-03 incident where starting a session on a
system-anchored OpenRouter model 401'd — the `sk-or-v1…` key was "rejected" by
`platform.openai.com`. The key was fine; the request was being sent to OpenAI's
endpoint instead of OpenRouter's.

Background: `docs/api_key_resolution.md` and `docs/features/db_backed_model_catalog.md`
(the routing model); memory `openrouter_system_provider_routing.md` (root-cause write-up).

**Root cause (one line):** a `provider_kind='system'` catalog row carries
`base_url=None` (`src/core/model_registry.py:251-257`), and the dispatch injected
only the api_key — never `provider`/`base_url` — so the agent's `create_llm`
(`src/core/loader.py:2098`) defaulted to the OpenAI factory (`api.openai.com`).

**What was fixed** (`orchestrator/main.py`, branch `develop`):
1. **Provider injection** — inject `meta.provider` (the `_factory_provider` name)
   into the llm section for system-anchored rows, via `setdefault`, at both
   `_inject_dispatch_credentials` (inline main-llm) and `_inject_model_credentials`
   (phases / aux / user-default / session). OpenRouter rows then route through
   `_create_openrouter_llm` → `openrouter.ai` (which also strips the `openrouter/`
   prefix back to the gateway slug).
2. **Prefix normalization** — `_normalize_catalog_model_id()` prepends `openrouter/`
   on create *and* update for system + `openrouter` rows (Admin → Models).

**Coverage map** — what each layer proves:

| Layer | Proves | Needs |
|---|---|---|
| §0 Automated unit tests | injection + normalization logic | local pytest |
| §1 Static check | the fix is actually present in the deployed code | repo / image |
| §2 Catalog API / DB | new OpenRouter adds store the `openrouter/` prefix | admin access or DB |
| §3 Live session | end-to-end: no 401, the session responds | dev cluster + deploy |
| §4 Existing row | the un-prefixed M3 row recovers on deploy (no re-add) | dev cluster |
| §5 Agent logs | the call goes to `openrouter.ai`, not `api.openai.com` | pod logs |

Target time: **~15 min** (§0 automated: ~1 min; §3–§5 live: ~10 min after deploy).

---

## 0. Automated tests (quick gate — no cluster)

These exercise the injection + normalization logic with mocked Postgres. They run
locally despite the env being noisy for the full suite (see memory
`local_test_env_vs_ci_and_ruff`).

```bash
python -m pytest \
  tests/test_dispatch_phase_credentials.py::TestSystemProviderRouting \
  tests/test_admin_models_api.py::TestNormalizeCatalogModelId -q
```

**Pass criteria:** `8 passed` (3 routing + 5 normalization).

Broader regression (no routing/dispatch test should have broken):

```bash
python -m pytest \
  tests/test_dispatch_phase_credentials.py tests/test_admin_models_api.py \
  tests/test_model_registry.py tests/test_loader_routing.py tests/test_message_triage.py -q
```

**Pass criteria:** all green (135 at the time of writing).

---

## 1. Static check: the fix is present

Run against the checked-out branch (or `kubectl exec` into the orchestrator pod and
`rg` inside `/app/orchestrator/main.py`) to confirm the deployed image actually
carries the change.

```bash
rg -n "factory_provider" orchestrator/main.py        # provider injection
rg -n "_normalize_catalog_model_id" orchestrator/main.py  # normalization
```

**Pass criteria:**
- Two `factory_provider = meta.provider if meta is not None else …` injection sites
  (inline dispatch ~L954 and `_inject_model_credentials` ~L2030), each followed by a
  `setdefault("provider", factory_provider)`.
- `_normalize_catalog_model_id` defined once + called in both
  `admin_create_catalog_model` and `admin_update_catalog_model`.

---

## 2. Normalization: new OpenRouter adds get the `openrouter/` prefix

Add a throwaway OpenRouter chat model with an **un-prefixed** slug and confirm it is
stored **with** the prefix. Requires an admin session cookie against the dev
orchestrator (`$ORCH`, `$ADMIN_COOKIE`).

```bash
curl -sS -X POST "$ORCH/api/admin/providers/models" \
  -H "Cookie: $ADMIN_COOKIE" -H 'Content-Type: application/json' \
  -d '{"provider_kind":"system","provider_ref":"openrouter",
       "model_id":"qwen/qwen3-max","display_label":"Qwen3 Max (OR test)",
       "capabilities":["chat"],"family":"qwen"}' | jq -r .model_id
```

**Pass criteria:** prints `openrouter/qwen/qwen3-max` (the prefix was added on write).

DB cross-check (every openrouter row should be prefixed):

```sql
SELECT provider_kind, provider_ref, model_id
FROM models WHERE provider_ref = 'openrouter' ORDER BY created_at DESC;
```

> Clean up the throwaway row afterwards
> (`DELETE /api/admin/providers/models/{id}` or via Admin → Models).

---

## 3. Live end-to-end: start a session on the M3 model (dev cluster)

> Prereq: the `develop` image carrying these changes is built and rolled out to dev
> (dev tracks `sha-XXX` tags from develop CI — see memory `deployment_topology`).

1. In Cockpit (dev), start a **new session**.
2. Select the OpenRouter model `minimax/minimax-m3` (listed under the **Openrouter**
   provider group).
3. Send a trivial prompt, e.g. `hi`.

**Pass criteria:** the model responds normally.

**Fail signal (regression):** the turn errors with
`401 - Incorrect API key provided: sk-or-v1…` referencing `platform.openai.com`.

---

## 4. The existing un-prefixed M3 row recovers on deploy

The live `minimax/minimax-m3` row was created **before** the fix, so its stored
model_id has no prefix. The provider-injection fix makes it work **without re-adding**
(the OpenRouter factory only strips a prefix when present, so it sends the slug
verbatim — which is the correct OpenRouter id).

- **Primary check:** §3 passes with the existing row untouched.
- **Optional:** edit that row's model_id in Admin → Models (any PATCH that includes
  `model_id`) and confirm it is rewritten to `openrouter/minimax/minimax-m3`. Both the
  prefixed and un-prefixed forms must work.

---

## 5. Confirm routing in the agent logs

Find the agent pod handling the session and grep its logs for the LLM-creation line.

```bash
kubectl -n <dev-namespace> logs <agent-pod> | grep -E "Created (OpenRouter|OpenAI) LLM"
```

**Pass criteria:** a line like

```
Created OpenRouter LLM: model=minimax/minimax-m3, …, base_url=https://openrouter.ai/api/v1, …
```

**Fail signal (regression):**

```
Created OpenAI LLM: model=minimax/minimax-m3, …, base_url=default, …
```

(`base_url=default` → the OpenAI SDK hits `api.openai.com` → the 401.)

---

## Known gap — NOT covered by this fix

The **builder / message-triage** LLM resolves its base_url through a separate path
(`get_builder_base_url` / `message_triage._resolve_triage_config`), which still leaves
a system-anchored OpenRouter model pointed at `api.openai.com`. If you set the
**builder default model** (Admin → Defaults → builder) to an OpenRouter system model,
expect it to 401 the same way until that path is fixed too. Track separately; it is
not exercised by the session flow above.

---

## Rollback

The change is additive: two `setdefault("provider", …)` blocks plus a normalization
helper. To revert, drop the two `factory_provider` blocks and the
`_normalize_catalog_model_id` definition + its two call sites. Already-stored model_ids
are unaffected by reverting normalization (the prefix stays on rows written while the
fix was live; both forms route correctly).
