# Mistral AI First-Class Provider — Verification Runbook

Verifies the first-class **`mistral`** LLM provider added in commit `afc03bac`
("Add Mistral as a first-class system LLM provider") and the four flagship models
seeded with it:

| Display | model_id | caps | notes |
|---|---|---|---|
| Mistral Large 3 | `mistral-large-latest` | chat, vision | flagship |
| Mistral Medium 3.5 | `mistral-medium-latest` | chat, vision | agentic, reasoning_effort |
| Mistral Small 4 | `mistral-small-latest` | chat, vision | cheap tier |
| Codestral | `codestral-latest` | chat | coding, text-only |

Background: memory `llm_catalog_seeding_mechanism` (how a provider/family is wired
end-to-end) and the sibling runbook `openrouter_system_provider_routing_verification.md`
(Mistral routes through the **same** OpenAI-compatible path and the same
dispatch-time provider injection).

**Design (one line):** `mistral` is `provider_kind='system'`; its catalog rows carry
`base_url=None`, dispatch injects `provider='mistral'` (the `_factory_provider` name),
and the agent's `create_llm` routes to `_create_mistral_llm` → `ReasoningChatOpenAI`
@ `https://api.mistral.ai/v1`. The wire is OpenAI-compatible, so no native SDK and no
new dependency. It is a **settings-only** family (uses `default` prompts + `default.yaml`
guardrails); only vision/context/sampling live in the `mistral` matrix row.

**What was added** (the surface a new provider touches):

| Layer | File |
|---|---|
| Provider CHECK migration | `orchestrator/database/migrations/app/0029_add_mistral_provider.sql` |
| Factory + dispatch | `src/core/loader.py` (`_create_mistral_llm`, `create_llm` arm) |
| Provider registry | `src/core/model_registry.py` (`_FACTORY_PROVIDERS`, `family_of`) |
| Env-key map | `orchestrator/main.py` (`_PROVIDER_ENV_KEYS`) |
| Discovery | `orchestrator/services/discovery.py` (`_fetch_mistral`, `_FETCHERS`, multimodal patterns) |
| Family detection | `orchestrator/services/family_matcher.py` |
| Matrix family (settings) | `config/model_config_matrix.yaml` (`mistral:`) |
| Seed (dev) | `deployment/values-experimental.yaml` (`systemApiKeys.mistral` + provider-direct `systemModels`) |
| Cockpit | `api.model.ts`, `admin-providers.component.ts`, `settings.component.ts`, `agent-settings.types.ts` |

**Coverage map** — what each section proves:

| Section | Proves | Needs |
|---|---|---|
| §0 Automated tests | family + routing + discovery logic | local pytest + cockpit vitest |
| §1 Static check | the change is present in the deployed code | repo / image |
| §2 Prerequisites | key, migration, seed are in place | dev cluster / Vault |
| §3 Direct API | the Mistral key + model ids are valid (isolates provider-side issues) | a Mistral key |
| §4 Discovery | Admin key-save enumerates Mistral + flags vision | admin access |
| §5 Catalog | `/api/models` exposes the Mistral group | admin access |
| §6 Live session | end-to-end per model: chat, tools, vision | dev cluster + deploy |
| §7 Routing + settings | call hits `api.mistral.ai`; matrix settings applied | pod logs |

Target time: **~20 min** (§0 automated ~2 min; §3 direct ~1 min; §4–§7 live ~12 min after deploy).

---

## 0. Automated tests (quick gate — no cluster)

Pure-function family resolution, the matrix row, loader routing, and discovery — all
run locally despite the noisy env (memory `local_test_env_vs_ci_and_ruff`; CI/Py3.12 is
the real gate).

```bash
# Family / matrix / registry / loader routing
python -m pytest \
  tests/test_prompt_matrix.py tests/test_settings_matrix.py \
  tests/test_model_registry.py tests/test_family_matcher.py \
  tests/test_loader_routing.py -q

# Discovery + admin providers
python -m pytest \
  tests/test_discovery_service.py tests/test_admin_providers_db.py \
  tests/test_admin_providers_api.py tests/test_instruction_matrix.py -q
```

**Pass criteria:** all green (259 + 86 = **345 passed** at time of writing).

Frontend family detector:

```bash
cd cockpit && npx vitest run src/app/views/agent-settings/agent-settings.types.spec.ts
```

**Pass criteria:** the `detectModelFamily — Mistral` suite passes.

Inline routing smoke (no cluster, no network — construction only):

```bash
python - <<'PY'
from src.core.model_registry import family_of, _factory_provider, _FACTORY_PROVIDERS
assert "mistral" in _FACTORY_PROVIDERS and _factory_provider("mistral") == "mistral"
for m in ("mistral-large-latest", "codestral-latest", "openrouter/mistralai/mistral-large"):
    assert family_of(m) == "mistral", (m, family_of(m))
from src.core.loader import create_llm, LLMConfig
llm = create_llm(LLMConfig(model="mistral-large-latest", provider="mistral",
                           api_key="sk-test", temperature=0.0))
base = getattr(llm, "openai_api_base", None) or str(getattr(getattr(llm, "client", None), "base_url", ""))
assert type(llm).__name__ == "ReasoningChatOpenAI" and "api.mistral.ai" in base, (type(llm).__name__, base)
print("OK: family_of→mistral ; create_llm(mistral)→ReasoningChatOpenAI @", base)
PY
```

**Pass criteria:** prints `… ReasoningChatOpenAI @ https://api.mistral.ai/v1`.

---

## 1. Static check: the change is present

Run against the checked-out branch (or `kubectl exec` into the orchestrator pod and
`rg` inside `/app`) to confirm the deployed image carries it.

```bash
rg -n '"mistral"' src/core/model_registry.py            # _FACTORY_PROVIDERS + family_of branch
rg -n "_create_mistral_llm" src/core/loader.py          # factory + create_llm dispatch arm
rg -n "MISTRAL_API_KEY" orchestrator/main.py            # _PROVIDER_ENV_KEYS
rg -n "_fetch_mistral|api.mistral.ai" orchestrator/services/discovery.py
rg -n "^mistral:" config/model_config_matrix.yaml       # matrix settings family
ls orchestrator/database/migrations/app/0029_add_mistral_provider.sql
```

**Pass criteria:** each prints a hit; `_create_mistral_llm` appears twice in `loader.py`
(the `elif provider == "mistral"` arm + the function definition).

---

## 2. Prerequisites (must be true before live tests)

1. **API key in Vault.** `MISTRAL_API_KEY` is a property in the `srw-secrets` bundle
   (`homelab/superhuman-remote-worker/srw-secrets`). ESO syncs the whole bundle into the
   `srw` Secret; the seed Job reads it as `SEED_MISTRAL_API_KEY`.
   ```bash
   kubectl -n <dev-ns> get secret srw -o jsonpath='{.data.MISTRAL_API_KEY}' | head -c4; echo " …(present)"
   ```
2. **Migration applied.** The orchestrator applies `0029` at startup before the seed
   Job's health gate passes.
   ```bash
   kubectl -n <dev-ns> exec deploy/srw-orchestrator -- \
     psql "$DATABASE_URL" -c "\d+ system_api_keys" | grep valid_system_api_key_provider
   ```
   **Pass:** the CHECK lists `'mistral'`.
3. **Seed ran.** The `srw-llm-seed` Job created the key row + 4 model rows.
   ```sql
   SELECT provider FROM system_api_keys WHERE provider = 'mistral';
   SELECT model_id, capabilities FROM models WHERE provider_ref = 'mistral' ORDER BY display_label;
   ```
   **Pass:** one key row + four model rows; the three non-Codestral rows include `vision`.

> If the key is absent when the chart syncs, the seed Job pod fails to start
> (`secretKeyRef` to a missing key). Add the Vault key **first**.

---

## 3. Direct API sanity (isolates provider-side issues)

Confirms the key works and the `-latest` aliases resolve — run this first if anything
downstream 4xxs, to tell "our wiring" from "Mistral/key".

```bash
# Models list — the four seeded aliases must appear
curl -sS https://api.mistral.ai/v1/models \
  -H "Authorization: Bearer $MISTRAL_API_KEY" \
  | jq -r '.data[].id' | grep -E 'mistral-(large|medium|small)-latest|codestral-latest'

# Minimal chat completion
curl -sS https://api.mistral.ai/v1/chat/completions \
  -H "Authorization: Bearer $MISTRAL_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"mistral-large-latest","messages":[{"role":"user","content":"Reply with one word: pong"}]}' \
  | jq -r '.choices[0].message.content'
```

**Pass criteria:** the aliases are listed; the chat call returns `pong`. A 401 here means
the key itself is bad — fix that before §4+. If an alias is missing, pin the dated id in
the seed (`mistral-medium-2604` etc.) and re-seed.

---

## 4. Discovery: Admin key-save enumerates Mistral

In Cockpit (dev) **Admin → Providers**, add/rotate the **Mistral** system key
(the provider now appears in the dropdown). On save, the orchestrator calls
`discover_models("mistral", key)` → `GET /v1/models`.

**Pass criteria:** the confirmation dialog lists Mistral models; `mistral-large/medium/small-*`
are flagged **supported** (family `mistral`, not "Generic") and the multimodal ones suggest
the `vision` capability. Codestral is chat-only.

API equivalent (admin cookie):

```bash
curl -sS "$ORCH/api/admin/providers/mistral/discovery" -H "Cookie: $ADMIN_COOKIE" \
  | jq '.candidates[] | {model_id, detected_family, supported, suggested_capabilities}' | head -40
```

---

## 5. Catalog: `/api/models` exposes the Mistral group

```bash
curl -sS "$ORCH/api/models" -H "Cookie: $ADMIN_COOKIE" \
  | jq '.groups[] | select(.provider=="mistral") | {group, provider, models: [.models[].model_id]}'
```

**Pass criteria:** a `mistral` group with the four model_ids. They also appear in the
chat-model picker (and the vision dropdown for the three multimodal rows).

---

## 6. Live end-to-end per model (dev cluster)

> Prereq: the `develop` image carrying `afc03bac` is rolled out to dev
> (dev tracks `sha-XXX` tags from develop CI — memory `deployment_topology`).

For **each** of the four models, start a new session in Cockpit selecting it under the
**Mistral** group:

1. **Chat** — send `hi`. → responds normally (no 401/500).
2. **Tool use** — ask something that forces a tool, e.g. *"list the files in the workspace
   root"*. → the agent emits a tool call and continues. (Exercises `parallel_tool_calls: true`
   — the model may batch multiple calls in one turn.)
3. **Vision** (skip Codestral) — attach a screenshot/PDF page and ask *"what's in this image?"*.
   → the image is described, and the turn does **not** compact prematurely (see §7).

**Pass criteria:** all four chat + tool; the three multimodal models also pass vision.

**Fail signal:** a turn errors with `401 … Incorrect API key … platform.openai.com` — that
means provider injection didn't fire and the call went to OpenAI (see §7).

> Local k3d alternative (no Cockpit): drive a job via the orchestrator API with
> `X-Internal-Key: dev_mcp_internal_key` and an in-pod `python3 urllib` call
> (memory `local_k3d_testing_via_orchestrator_api`), setting
> `config_override.llm.model = "mistral-large-latest"`.

---

## 7. Confirm routing + matrix settings in the agent logs

Find the agent pod for the session and grep the LLM-creation line.

```bash
kubectl -n <dev-ns> logs <agent-pod> | grep -E "Created (Mistral|OpenAI) LLM"
```

**Pass criteria:**

```
Created Mistral LLM: model=mistral-large-latest, …, base_url=https://api.mistral.ai/v1, …
```

**Fail signal (provider injection regressed):**

```
Created OpenAI LLM: model=mistral-large-latest, …, base_url=default, …
```

(`base_url=default` → OpenAI SDK hits `api.openai.com` → 401. This is the same
dispatch-injection mechanism the OpenRouter runbook covers; Mistral depends on it.)

**Matrix settings (the `multimodal` correctness check):** after a vision turn, confirm the
context manager counted image tokens via the `mistral` family, not the `default`
(`multimodal: false`) fallback. In the agent logs the per-image estimate should track the
`anthropic_patches`/3025-cap scheme (≤3025 tok/image), and a multi-image session must **not**
trip compaction at the 128k default window — it should use the family's 256k. If images are
mis-counted, the `mistral` matrix row isn't resolving (check `family_of`/`detect_family`).

---

## Known gaps — NOT covered

- **Embeddings.** No Mistral embedding model is seeded (`mistral-embed`); the embedding-
  provider settings dropdown intentionally omits Mistral. Add a row + UI option if needed.
- **Reasoning.** `reasoning_level` is unset for all four rows. Mistral Medium 3.5 supports
  `reasoning_effort`, but it is not wired; reasoning extraction falls through to the `api`
  method (harmless while unrequested).
- **Magistral** was deliberately omitted (deprecates 2026-07-31; reasoning lives in Medium 3.5).
- **Builder / message-triage** model path resolves base_url separately (same caveat as the
  OpenRouter runbook). Setting the builder default to a Mistral model is unverified here.
- **Prod.** This runbook targets dev (`values-experimental.yaml`). Prod is a separate manual
  cut with its own values file + Vault path.

---

## Rollback

The change is additive. To revert: drop the `_create_mistral_llm` factory + its `create_llm`
arm, remove `mistral` from `_FACTORY_PROVIDERS` / `family_of` / `_PROVIDER_ENV_KEYS` /
discovery / `family_matcher` / the matrix family / the cockpit lists, and remove the seed
block. Migration `0029` is a CHECK-widening; leaving it applied is harmless (no row uses
`mistral` once the seed is gone), but a down-migration would need to delete any `mistral`
key/model rows first or the re-narrowed CHECK will fail to validate.

---

## Acceptance checklist

- [ ] §0 automated: 345 pytest + cockpit vitest green; inline routing snippet prints the Mistral base_url
- [ ] §1 static: all rg hits present in the deployed image
- [ ] §2 prereqs: Vault key present; `0029` CHECK lists `mistral`; 1 key row + 4 model rows seeded
- [ ] §3 direct API: aliases listed; chat returns `pong`
- [ ] §4 discovery: Mistral models enumerated + flagged supported/vision
- [ ] §5 catalog: `mistral` group with 4 models in `/api/models`
- [ ] §6 live: all 4 models chat + tools; 3 multimodal pass vision
- [ ] §7 logs: `Created Mistral LLM … base_url=https://api.mistral.ai/v1`; images counted via the mistral family
