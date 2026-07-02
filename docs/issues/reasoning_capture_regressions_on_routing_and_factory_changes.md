# Reasoning capture silently regresses when a model's routing/factory changes (recurring)

**Date:** 2026-07-02
**Status:** **OPEN (systemic guard not built).** The *pattern* is documented here;
the latest instance (k3d codex via gateway) is **fixed**; a **continuous
reasoning-capture canary + a dispatch-time invariant test are PROPOSED** below
as the "do something about it" work. This doc is deliberately class-level — it
supersedes the "nice-to-have daily check" footnotes scattered across the
instance docs.
**Severity:** **Medium-High — because it's silent.** No outage, no exception, no
failed job. Reasoning simply vanishes from both cockpit surfaces (job-debug chat
+ sessions chat) and stays gone until a human happens to notice. It erodes trust
in a headline feature and, worse, *masks* model/routing regressions that we'd
otherwise catch.
**Component (the whole capture chain — any link breaks it):**
- **Dispatch routing** — `orchestrator/main.py` `_inject_dispatch_credentials` / `_inject_model_credentials` / `_inject_thread_dispatch_credentials`, `_should_route_via_gateway` (`:1345`). Decides *which endpoint/provider* a model talks to and can **force `provider="openai"`** (`:3852-3860`).
- **Factory selection** — `src/core/loader.py` `_create_{openai,codex,google,openrouter,mistral}_llm`. Only the *matching* factory requests the reasoning summary/thinking (`_create_codex_llm` → Responses API `reasoning.summary:"auto"`; `_create_google_llm` → thinking config; `_create_openai_llm` → `reasoning_effort` or `chat_template_kwargs.enable_thinking`).
- **Capture tap** — `src/llm/reasoning_chat.py` (`_SSEReasoningTap`, `_extract_reasoning_from_delta`, `_extract_responses_api_reasoning`). Reads the *provider-specific* field (`reasoning_content` / `reasoning` / `reasoning_details` / Responses `reasoning` blocks).
- **Reasoning capability model** — `config/model_config_matrix.yaml` `reasoning` block + `resolve_reasoning_plan()` / `reasoning_capability()` (see [[family_centered_reasoning]]).
- **Persistence + render** — `chat_history.reasoning` (worker) / `thread_messages.thinking` (session) → cockpit request-viewer / persistent-chat.

**Related (annotated):**
- [[litellm_gateway_drops_gpt_codex_reasoning_capture]] — **the canonical prior instance** (gpt-5.x/codex, 06-24): gateway routing resolved codex to the *openai* factory (Chat Completions, no summary requested) → 40%→0% capture. Its own "Fix" section ends with: *"A lightweight daily check that per-model-class reasoning capture doesn't collapse to 0% remains a nice-to-have."* — that check is what this doc proposes building.
- [[session_empty_response_gpt5_codex_stop]] — same `agent → codex-proxy` family; the empty-`stop`-completion sibling (reasoning + answer both vanish on a bad turn).
- [[codex_session_gateway_baseurl_401]] — a **stale gateway `base_url` pinned on a codex model** → 401; another way a config/routing artifact silently diverts a reasoning model off its path.
- [[gemini3_thinking_temperature_loop]] — **different model, same class**: Gemini 3.x thinking models degenerate at temp-0 → silent tool-call runs → empty response (thinking handling in `_create_google_llm` + persistent path).
- [[reasoning_effort_injected_without_capability_guard]] / [[family_centered_reasoning]] — **gemma / gpt-oss / minimax** got an *inert or double-injected* reasoning param before capability-gating; the fix (family-centered reasoning) is the config layer this doc's invariant test should assert against.
- [[persistent_chat_reasoning_after_answer_and_replay_duplication]] — gemma-style reasoning **ordering/render** variant (captured, but surfaced wrong).
- [[langchain_responses_api_streaming]] — the Responses-API/streaming fragility (`gpt-5.*`, `o3`, `o4`, all codex-proxy models) that makes the codex capture path brittle in the first place.
- [[litellm_streaming_usage_not_surfaced]] — the *usage-bar* twin: same route, same "empty because the turn errored / was rerouted" confusion (usage and reasoning go dark together).
- [[remove_litellm_proxy_and_gateway_concept]] — the 2026-07-01 decision to remove the gateway; its target arch ("codex bypasses LiteLLM entirely") is exactly the invariant that, when a config drifts off it, reproduces this bug.
- [[route_all_models_through_litellm_gateway]] / [[agent_llm_factory_collapse]] — the routing/factory refactors that repeatedly move models between paths (each move is a chance to silently drop reasoning).

---

## Why this keeps happening (the class)

Reasoning is captured **only when the entire chain lines up at once**:

```
routing picks the endpoint/provider
  → factory matches that provider AND asks for the reasoning summary/thinking
    → the SSE/response tap reads the exact field that provider emits it in
      → streaming vs non-streaming path both preserve it
        → persisted to the right column → rendered
```

Every refactor we do touches one of those links: enabling/disabling the gateway,
adding a canary (`LITELLM_GATEWAY_ROUTED_PROVIDERS`), collapsing factories,
re-registering a model, changing a proxy version, flipping `use_responses_api`.
Any change that moves a **reasoning-capable** model off its blessed path breaks
capture.

**The reason it's always *silent*:** an empty reasoning field is a *valid*
value. Every layer faithfully persists "no reasoning," the cockpit faithfully
renders nothing, and there is **no error, no metric, and no alert**. The failure
is invisible until a person opens a session and notices the "Thinking" bubble is
gone — often days later, as with the latest instance. There is currently **no
automated signal** that a model that *should* be producing reasoning has stopped.

## Timeline of occurrences (various models — this is the point)

| Date | Model(s) | Trigger that moved it off the reasoning path | Doc |
|------|----------|----------------------------------------------|-----|
| 2026-06-15 | **gemma / gpt-oss / minimax** | `reasoning_effort` injected without a capability guard (inert / double-injected) | [[reasoning_effort_injected_without_capability_guard]] |
| 2026-06-15 | **Gemini 3.x** | temp-0 thinking loop → empty responses; thinking-block handling in the google factory | [[gemini3_thinking_temperature_loop]] |
| 2026-06-22→24 | **gpt-5.x / codex** | LiteLLM gateway enablement → codex resolved to the *openai* factory → no summary requested; capture 40%→0% | [[litellm_gateway_drops_gpt_codex_reasoning_capture]] |
| 2026-06-25 | **gpt-5.x / codex** | stale gateway `base_url`/`provider` pinned on a codex session → 401, turn (and reasoning) lost | [[codex_session_gateway_baseurl_401]] |
| 2026-07-01/02 | **gpt-5.x / codex** | k3d `values-local.yaml` still had `routedProviders: ["*"]` → codex forced through the gateway's chat↔Responses bridge, which drops the summary on streaming (see below) | this doc (§ Latest instance) |

Five occurrences across four+ model families in ~3 weeks. It is not a per-model
bug; it is a **structural fragility** in how capture depends on routing.

## Latest instance (2026-07-01/02) — k3d: codex reasoning lost via the gateway

**Symptom.** Session `6ac63684` rendered no reasoning; `thread_messages.thinking`
= 0/5, `llm_requests.additional_kwargs` empty. Cluster-wide gpt-5.5 capture:
**57% on 06-26 → 0% on 07-01.**

**Root cause.** `gpt-5.5` is backed by the `codex-proxy` endpoint; its correct
path is the **codex factory → Responses API + `reasoning.summary:"auto"`**
(reliably emits the summary — verified **3/3** direct trials: 175/96/201 summary
deltas). But P2 (2026-06-28, `3bb72235` + wildcard `480839db`) made codex
*routable through* the gateway, and k3d's gitignored `deployment/values-local.yaml`
still carried **`litellm.enabled: true` + `routedProviders: ["*"]`** from that
canary. With `*`, `_should_route_via_gateway(codex)` returns True →
`_inject_model_credentials` **forces `provider="openai"` + the gateway base_url**
(`main.py:3852-3860`) → the agent builds `Created OpenAI LLM … reasoning=chat_completions`
and talks Chat Completions to the gateway's `use_responses_api` bridge. That
bridge **unreliably drops the reasoning summary on the streaming session path**
(probed live: 0↔77 `reasoning_content` deltas across identical calls; langchain
sends no `stream_options`). The 2026-07-01 gateway-removal
([[remove_litellm_proxy_and_gateway_concept]]) that would have fixed this was
applied to **homelab `values-experimental.yaml` only** — the k3d overlay was
never updated, so k3d sat in the broken mid-migration state.

**Fix (applied + verified).** `deployment/values-local.yaml`:
`litellm.enabled: false` + `routedProviders: []` (matches the homelab P0 and the
chart default `helm/values.yaml:1137/1161`). Gateway removed →
`_gateway_routing_target()` returns None → codex bypasses direct to the codex
proxy → codex factory → reasoning restored. Still metered via the in-process
audit→`usage_events` pipeline. **No code change.** Verified: direct codex
`/responses` path captures reasoning **3/3**; orchestrator healthy on the correct
image; `srw-litellm` deployment gone. (Operational note: applying a
`values-local.yaml` change via a manual `helm upgrade` **while `tilt up` is
running** reverts Tilt's local image injection → the stale chart-default image
crashes on the migration guard, and re-syncing Tilt reconciles via a full chart
reinstall. Under Tilt, edit `values-local.yaml` and let Tilt reconcile / `tilt
trigger srw` — never a manual `helm upgrade`.)

## What we should do about it (proposed — the systemic part)

The instance fixes are all one-offs. The class needs a **standing signal** and a
**pre-merge invariant** so the next routing/factory change can't silently zero
out reasoning again. Recommended, cheapest-first:

1. **Continuous reasoning-capture canary + alert (highest value, cheap).** We
   already have the gateway-independent data: `srw_audit.llm_requests` records
   per-call `response.additional_kwargs.reasoning_content`. Add a periodic check
   (fold into the existing `llm_usage_poll_loop`, or a small cron) that computes,
   per **reasoning-declared** model family (from `model_config_matrix.yaml`),
   the reasoning-present rate over a rolling window and **alerts/logs LOUD when a
   family that historically produced reasoning collapses to ~0%** over N calls.
   This is exactly the "daily check" [[litellm_gateway_drops_gpt_codex_reasoning_capture]]
   deferred. It would have caught all five occurrences within hours instead of days.

2. **Dispatch-time invariant unit test (pre-merge guard).** For every family in
   `model_config_matrix.yaml` that declares a `reasoning` capability, assert over
   the seeded catalog that: (a) the resolved factory is one that *requests*
   reasoning for that provider, and (b) the model is **not** routed onto a path
   we know drops it (e.g. a codex/Responses model forced through the gateway's
   chat-completions bridge). Ride the existing
   `tests/test_dispatch_phase_credentials.py::TestCodexBypassesGateway` +
   `test_model_registry.py` scaffolding — generalize "codex bypasses" into
   "reasoning-capable models resolve to a reasoning-capturing path."

3. **Fail-loud runtime warning (defense in depth).** In the agent, when a model
   whose family declares reasoning capability returns **K consecutive turns with
   empty reasoning**, emit a WARN (audit + pod log) naming the model + factory +
   base_url. Turns an invisible regression into a greppable signal at the source.
   (There is already a "reasoning-starve" warning path — `7ea0d798` — to build on.)

4. **Encode the routing invariant, not just the config.** The latest instance was
   a *config drift* (`routedProviders: ["*"]` on one overlay). Make
   "codex/Responses-only models never route through the chat-completions bridge"
   a **guard in `_should_route_via_gateway`** (refuse + warn), so a stray
   `routedProviders` value can't silently reintroduce it. Aligns with
   [[remove_litellm_proxy_and_gateway_concept]] (codex bypasses entirely) and
   survives any future gateway re-introduction.

## Acceptance criteria (for closing this)

- [ ] A per-model-family reasoning-capture rate is computed from `llm_requests`
      and an alert fires when a reasoning-declared family drops to ~0% over a
      window. (Guard #1)
- [ ] A unit test fails if any reasoning-declared family in the catalog resolves
      to a non-reasoning-capturing factory/route. (Guard #2)
- [ ] Verified by deliberately re-introducing the k3d misconfig
      (`routedProviders: ["*"]`) and confirming the guard(s) fire.

## Appendix — quick diagnosis recipe (for the next occurrence)

1. **Confirm it's capture, not render:** query the source columns, not the UI —
   `thread_messages.thinking` (session) / `chat_history.reasoning` +
   `llm_requests.response->'additional_kwargs'->>'reasoning_content'` (worker).
   Empty at the source ⇒ capture bug (this class); populated ⇒ a render bug
   (see [[persistent_chat_reasoning_after_answer_and_replay_duplication]]).
2. **Date it + scope it by model:** `GROUP BY day, model` on
   `llm_requests` with a `has_reasoning` boolean → pins the regression to a
   deploy/config change and tells you if it's one model or all.
3. **Read the agent factory line:** `Created {Codex|OpenAI|Google} LLM: model=…,
   base_url=…, reasoning=…`. Wrong factory / wrong base_url / `reasoning=chat_completions`
   on a model that needs `responses_api` ⇒ routing moved it off its path.
4. **Isolate the wire:** probe the endpoint directly (bypass the agent) to see if
   the *provider* still emits reasoning; then probe through whatever normalizer
   (gateway) sits in front. The gap localizes the drop.
