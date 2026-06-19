---
tags:
  - security
  - agent-architecture
  - credentials
  - secrets-management
  - cost-control
  - networking
  - idea
aliases:
  - credential broker
  - api key broker
  - key vending
  - secrets broker
  - ephemeral session keys
  - credential proxy
  - per-session virtual keys
related:
  - "[[tool_permission_tiers]]"
  - "[[sudo_permissions]]"
  - "[[custom_llm_endpoints]]"
  - "[[db_backed_llm_config]]"
  - "[[workspace_network_isolation]]"
  - "[[workspace_network_policy_unification]]"
  - "[[saas_billing_and_metering]]"
  - "[[observability_and_quotas]]"
  - "[[security_event_log]]"
  - "[[auth_bff_and_api_tokens]]"
  - "[[external_headscale]]"
  - "[[user_vpn_networks]]"
---

# Credential Broker — per-session keys & resource brokering for agents

> **Status: Idea / early-stage.** Pressure-tested in conversation, **nothing decided, not building.** This doc captures the raw idea, how it reconciles with what SRW already has, an honest threat model, and a pile of open questions. The single most important takeaway is the **credential-vs-access reframe** below — internalize that before any of this gets specced. If it ever does get built, the framing that justifies it is **cost-control + kill-switch + per-session least-privilege**, with credential hygiene as the bonus — *not* the other way around.

## The raw idea (original notes, verbatim)

> - Add an internal router that swaps api keys, so even if we expose them by accident we just expose the key generated for this internal session!
> - This way we can also give credentials to the ai by just adding a middle man that exposes the data source or ai api temporarily!
> - We can also give the llm access to resources that way!
> - The agent can use llms or credit cards through the systems resources and doesn't need its own api key for that.
> - We can also proxy ips and so on.
> - Temp api keys only work if the request comes from the assigned pod!

Three distinct concepts are bundled here, and they pull apart cleanly:

1. **Key brokering** (note 1) — the agent holds a per-session *virtual* key; a middleman swaps it for the real provider key. Leak → you expose the session key, not the master.
2. **Resource brokering** (note 2) — generalize the middleman to *any* credential/resource: datasources, other LLM APIs, "credit cards," outbound IPs.
3. **Pod-binding** (note 3) — a virtual key only works when the request originates from the assigned pod.

## What this actually is (naming the pattern)

A **credential broker** + **egress proxy with credential injection** + **per-workload ephemeral identity**. None of it is novel — it's a well-trodden security pattern with mature analogues:

- **LiteLLM Proxy / Cloudflare AI Gateway** — virtual keys swapped for real provider keys, with budget caps, revocation, and per-key audit. This is *exactly* note 1 for the LLM case.
- **HashiCorp Vault dynamic secrets** — short-lived, scoped, revocable credentials minted on demand.
- **AWS STS / IRSA, SPIFFE/SPIRE** — per-workload identity instead of long-lived shared secrets.

So the design risk here is low (the pattern is proven); the value is incremental hardening, and the interesting work is in the parts people wave away (policy, binding, the broker as a target).

## Prior art in SRW — we're ~70% there already

The striking thing from exploring the codebase: most of the spine already exists. This idea is largely **"promote a pattern we already run to a first-class, per-session-keyed, policy-enforcing broker for the normal path,"** not a green-field build.

- **The middleman already exists** — the **codex proxy** (CLIProxyAPI). The agent sends a bare model name; the proxy injects the real OAuth credential and forwards to the provider; the agent never holds that secret. See `src/core/loader.py` (Codex LLM via the CLIProxyAPI OAuth proxy), `orchestrator/seed/llm_config.py` (`CODEX_PROXY_ENDPOINT_LABEL`, `ensure_*_endpoint`, `CODEX_PROXY_URL`), and `orchestrator/init.py` (boot-time seed of the codex endpoint). **Note 1 is "do what codex already does, but for the normal LLM path."**
- **Stored-vs-live key separation already exists.** The orchestrator *strips* `api_key` from stored/frozen config and *re-injects* credentials at dispatch into `metadata.config_override`. See `src/agent.py:906-909` (`serialize_resolved_config` strips the key; resume dispatch re-injects via `_inject_dispatch_credentials`). Keys aren't persisted *with* the agent.
- **A strong identity substrate already exists** — the **tailnet** (headscale). Every agent pod/VM is a node with an unspoofable WireGuard identity, and the orchestrator already knows the session→node mapping (it provisions them). This is the right primitive for note 3 — see below. ([[external_headscale]], [[user_vpn_networks]])
- **Scoped-credential precedent already exists** — per-workspace, bucket-scoped S3 keys (virtual-workspace S3 provisioning). Same idea (a narrow, tenant-scoped credential) applied to storage rather than LLMs.
- **An audit trail already exists** to hang per-session observability on ([[security_event_log]], [[postgres_audit_store]]).
- **Encrypted endpoint keys at rest** — `orchestrator/security/crypto.py` (`_encrypt_optional`), `llm_endpoints.api_key` stored encrypted.

**The actual gap:** internal auth today is **one shared secret** (`_INTERNAL_KEY` in `orchestrator/security/access.py`), *not* a per-session minted credential. That single fact is the whole distance between "we strip keys at dispatch" and "we broker per-session keys."

## The core reframe — credential vs. access (read this first)

The framing *"even if we expose them by accident we just expose the session key"* treats the **credential** as the asset. For a live, autonomous, prompt-injectable agent, the **access is the asset — and it's live regardless of where the key sits.**

If the agent is compromised mid-session (e.g. prompt injection), the attacker doesn't need to *exfiltrate* the key — they just *use the session*. The virtual key is valid right now, from the right pod, within budget. The broker happily serves the attack, because it cannot distinguish "agent doing its job" from "agent doing the injector's bidding."

So brokering defends against **key exfiltration → lateral/persistent reuse across sessions/tenants**. It does **not** defend against **in-session abuse**. The honest value is "blast radius shrinks to one revocable, capped, bound session, plus a kill-switch" — *not* "exposing keys is now harmless." This changes what you'd invest in: fast revocation, hard budget caps, per-session least-privilege, and audit — i.e. controls that limit a *live* compromised session — rather than an elaborate key-swap mechanism dressed up as making leaks safe.

## Threat model (honest)

**Stops / mitigates:**

- Real master key never enters the agent process → no exfil → no cross-session / cross-tenant reuse.
- Long-lived credential sprawl (today the real key rides into the agent on the normal path at dispatch).
- *Off-pod* replay of a leaked virtual key (with tailnet binding — see note 3).
- "Rotate everything on one leak" collapses to "revoke one session."
- **Bonus, arguably the real prize:** per-session cost attribution, hard budget caps, a global kill-switch, and a single audit chokepoint. ([[observability_and_quotas]], [[saas_billing_and_metering]])
- **Least-privilege per session** — issue a key scoped to exactly the models/datasources a given job needs (only if the policy layer below gets built).

**Does NOT stop:**

- **In-session abuse** by a compromised/injected agent (key is live, on the right pod, under budget — the broker serves the attack).
- **The on-pod attacker** — *any* pod-binding is satisfied by code running on that pod, and "the agent got injected" means the attacker *is* on that pod.
- **Data exfiltration via legitimate access** — brokering the datasource credential doesn't stop the agent reading data it's allowed to read and leaking the *contents* out an egress channel. That's an egress-control problem, not a credential one. ([[workspace_network_isolation]])
- **Confused-deputy abuse**, if the broker only validates "is this a real virtual key?" and then forwards with god-credentials.

**New attack surface introduced:**

- **The broker holds all real keys for all tenants** → compromise it and you've lost everything. You trade *many low-value exposures* for *one high-value target*. Acceptable, but the broker's own hardening + audit become load-bearing.
- **Availability SPOF on the per-request hot path** — every call now traverses the broker. The codex proxy already has this property for one path; generalizing it makes the broker tier critical-path.
- **The issuance/minting path becomes critical** — if an attacker can mint a virtual key for an arbitrary session, or widen a key's scope, that's *worse* than today's sprawl.
- **Replay/forgery of the binding token**, if pod-binding is a bearer pod-token rather than a network-layer identity.

## Concepts, pressure-tested

### Note 1 — broker the normal LLM path

Sound, and the cheapest to reach. The real delta is moving the normal path from *dispatch-time injection* (real key lands in the agent process via `config_override`, `src/agent.py:906-909`) to *network brokering* (real key never in the agent — exactly what the codex proxy already does). Reuses existing code. **Sell it as blast-radius + kill-switch + caps, not key-safety.**

> ⚠️ **Unverified assumption to confirm:** that the normal path *does* currently inject the real provider key into the live agent process. Inferred from the `src/agent.py:906-909` comments + the codex path being the only brokered one. Needs a hands-on trace of the dispatch path before any of this is taken as settled.

### Note 3 — pod-binding

Good instinct; one hard rule: **bind to tailnet identity, not source IP.**

- IP allowlisting is spoofable/reused in-cluster and is trivially satisfied by the on-pod attacker anyway.
- The tailnet gives an unspoofable per-node WireGuard identity; the orchestrator already knows session→node. Nearly free, strictly better.
- **Know its ceiling:** it defeats *off-pod replay only*. It does nothing against the compromised agent *on the bound pod* — which is the primary threat. Worth doing; don't oversell it.
- mTLS / SPIFFE per-pod certs are the "textbook" workload-identity answer but are more machinery than tailnet-binding when the tailnet already exists. Probably YAGNI.

### Note 2 — datasource / other-API brokering

Fine, but the key-swap is the *easy 20%*. The hard 80% is **per-session authorization policy at the broker** — this is a textbook **confused deputy**: the broker holds god-credentials and acts for a low-trust agent. It needs a per-session *policy* (this job may hit *these* models / *this* datasource, up to *this* budget), not just a per-session *key*. Without that, any valid session reaches everything the broker can reach. That policy layer is the actual work, and it overlaps with [[tool_permission_tiers]] / [[sudo_permissions]] thinking (what is this session allowed to *do*).

### Note 2 — "credit cards"

**Hard no — and not (only) for PCI reasons.** An autonomous, injectable agent plus a payment rail is a fraud generator *no matter where the card sits*. The risk was never "the card leaks," it's "the agent spends" — and brokering moves the secret while doing nothing about the spend. The only conceivably-sane version is **virtual single-use cards with hard per-transaction caps + merchant allowlists** (Stripe Issuing / privacy.com style), which is its own product, not a slice of this. Park it.

### Note 2 — IP proxying

**Different control, different threat model.** Credential brokering governs *what secret the agent holds*; an egress proxy governs *where the agent can reach* (SSRF / exfil containment). They compose, but folding them together muddies both — and SRW already has tailnet egress tiers (`home-allowed`, etc.) doing the egress half. Keep them separate. ([[workspace_network_isolation]], [[workspace_network_policy_unification]])

## The broker as a single point of compromise

Pulling the cross-cutting risks together, because they're the reason this is non-trivial:

- **Availability** — per-request hot path; needs an HA / fail-fast / fallback story. (Counterpoint: the orchestrator is already critical-path for dispatch, so this isn't a *new class* of dependency — but it is a new *per-request* dependency vs. per-dispatch.)
- **Compromise concentration** — one place holding every real key. One hardened thing to defend beats keys sprayed into every agent, but it raises the stakes on the broker's own security.
- **Latency** — an extra hop per call. For LLM calls (100s of ms–seconds already) it's noise; for high-QPS resource access it could matter. Probably fine for this workload.
- **Audit upside (a *positive*)** — the broker is a perfect chokepoint for per-session audit: who called what, how much spend, which datasource. Detection, not just prevention; dovetails with the existing audit trail.

## Strategic framing (why this probably isn't "now")

Given the feature-freeze / runway posture: **the security story alone probably doesn't clear the bar to build now.** "Stop accidental key leaks" is real but abstract. The *same machinery's* cost-control story — per-session budget caps, a kill-switch for a runaway agent's spend, and per-job cost attribution — is concrete, runway-relevant, and falls out for free. If this ever gets built, **that** is the framing that justifies it; the credential hygiene is the bonus. See [[saas_billing_and_metering]], [[observability_and_quotas]].

## Open questions

Nothing here is decided. In rough priority:

- **Verify the premise:** does the normal LLM path actually hand the live agent the real provider key today? (See the unverified-assumption flag under Note 1.) If not, note 1's value shrinks.
- **What's the primary goal** if this is ever picked up — leak containment, cost-control/kill-switch, or the broad capability layer? They imply different first slices.
- **Broker shape:** extend the existing codex proxy / CLIProxyAPI to be the general broker, adopt LiteLLM Proxy (which already does virtual keys + caps + audit), or build a thin bespoke one? Buy-vs-build.
- **Where does the policy live** (the confused-deputy layer)? Is it the same substrate as [[tool_permission_tiers]] / [[sudo_permissions]], or separate? Per-session scoping of models + datasources + budget.
- **Virtual-key lifecycle:** mint when (pod creation? dispatch?), TTL, rotation, revocation propagation latency (how fast can a kill-switch actually cut a live session?).
- **Pod-binding mechanism:** tailnet identity confirmed as the binding? How does the broker verify the calling node, and how does it map node→session at request time?
- **Budget caps:** hard vs. soft, per-session vs. per-job vs. per-tenant; what happens to an in-flight call when the cap trips.
- **Availability/HA:** can agents tolerate broker downtime at all, or is it hard-fail? Any local fallback?
- **Audit schema:** what per-request fields, and does it reuse [[postgres_audit_store]] / [[security_event_log]]?
- **Multi-tenancy blast radius:** broker compromise = total compromise. Acceptable? Any per-tenant key partitioning / HSM / envelope encryption to soften it?
- **Scope creep guard:** keep credit cards and IP-proxying explicitly *out*, or leave a deliberate extension seam?

## Explicitly deferred / out of scope (for any first cut)

- **Credit cards / payment rails** — separate (regulated, fraud-shaped) project; see Note 2.
- **Arbitrary outbound IP proxying** — separate egress-control effort; tailnet tiers already cover the near-term need.
- **"Any resource"** — start with one class (LLM keys), prove the ephemeral-key + binding + caps + policy machinery, *then* consider a second (e.g. a scoped datasource credential).

---

*Provenance: written from a pressure-test conversation (June 2026). No spec, no plan, no code — idea capture only. If promoted, the next step is to verify the Note 1 premise, then run the normal brainstorming → spec → plan flow.*
