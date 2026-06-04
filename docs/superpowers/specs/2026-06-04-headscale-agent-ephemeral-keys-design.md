# Ephemeral Headscale Registrations for Container Agents — Design

**Status:** designed, not yet implemented.
**Date:** 2026-06-04
**Builds on:** [`docs/done/headscale_mesh.md`](../../done/headscale_mesh.md) (the canonical headscale mesh design this corrects), [`docs/features/external_headscale.md`](../../features/external_headscale.md) (bootstrap key env knobs).

> **TL;DR** — Container agent pods register with headscale using a **shared, non-ephemeral `tag:agent` pre-auth key** and are never cleaned up, so every per-job/pool pod becomes a permanent node. ~1,465 stale nodes accumulated and overloaded the headscale coordinator (486m CPU, ~28s/registration), which starved an unrelated garage-proxy sidecar into a crash loop. The fix is to treat container agents the way the VM path already treats VMs: **ephemeral**. Add `--ephemeral` to the shared key so headscale's built-in 30m inactivity GC reclaims them; add a **self-healing** re-up loop so a pod that loses its registration re-registers within ~30s; add a **tunnel-dark health-gate** so a pod whose tunnel is *permanently* dead is recycled. We do **not** move to per-pod minted keys (deferred), and the one-time backlog purge + key re-mint is operational (owned by the HomeLab session).

---

## 1. Motivation & goals

A container agent pod gets a tailscale sidecar (`orchestrator/services/agent_provisioner.py:1082-1142`) that runs `tailscale up --auth-key="${TS_AUTHKEY}" --hostname="${POD_NAME}"`, where `TS_AUTHKEY` is the shared `TAILSCALE_AUTH_KEY` secret. That key is created by `headscale-bootstrap.sh:98-103` as `--reusable`, `--tags tag:agent`, `--expiration 8760h` — **with no `--ephemeral`**. Combined with `tailscaled --state=mem:` (fresh node identity per pod) and a unique `--hostname=$POD_NAME`, every pooled/per-job pod registers a **new, permanent** headscale node. Nothing in the orchestrator ever deletes those nodes. Over months of pool churn this leaked ~1,465 `tag:agent` nodes.

By contrast the **VM path is already correct**: `vm/controller/headscale_client.py:105-111` mints a per-VM key with `"reusable": False, "ephemeral": True`, and `delete_node()` (`:132`) removes the node on teardown. **Persistent thread pods** (`orchestrator/services/persistent_provisioner.py`) build their own manifest with **no tailscale sidecar**, so they don't register at all. The leak is therefore *exclusively* the container-agent path.

`docs/done/headscale_mesh.md:304-308` deliberately specced agent keys as "long-lived, reusable" — it assumed agent *pods* were long-lived like the orchestrator. They are not: they churn like VMs. The same doc even lists "High node churn → resource usage spikes" as a known issue. This design corrects that assumption.

**Goals**

1. Container-agent registrations become **ephemeral**, so headscale's existing `ephemeral_node_inactivity_timeout` (30m) reclaims a pod's node automatically after the pod goes offline. No bespoke cleanup code.
2. An agent that **loses its registration while still alive** re-registers automatically (self-heal), so a transient loss is invisible rather than a silent connectivity outage.
3. An agent whose tunnel is **permanently dark** is recycled by the existing pool machinery, so a dead-tunnel pod is never handed work.
4. **Reconcile the design vault** so a future key rotation does not silently re-introduce the leak.

**Non-goals (explicitly deferred)**

- **Per-pod minted keys / retiring the shared key** (the "Option B" considered during brainstorming). The orchestrator does *not* gain a headscale admin API key or a `HeadscaleClient`. The shared reusable key stays; it just becomes ephemeral. Single-use per-pod keys are a separate, security-motivated change.
- **VM controller and persistent-pod changes.** VMs are already ephemeral with explicit cleanup; persistent pods have no sidecar. Out of scope.
- **The one-time remediation** — re-minting the key with `--ephemeral`, `vault kv patch … TAILSCALE_AUTH_KEY`, ESO refresh, and purging the existing ~1,465-node backlog. These are operational steps against the live cluster/Vault, owned by the HomeLab session. This spec covers only the SRW code/config that prevents recurrence. See §6.
- **Raising `ephemeral_node_inactivity_timeout`.** A headscale-config (HomeLab-side) knob; a complementary mitigation, not part of this change.

---

## 2. Background: how an agent pod joins the tailnet today

`orchestrator/services/agent_provisioner.py:1082-1142`, when `agent.tailscale.enabled` and `HEADSCALE_URL` is set, appends a `tailscale` sidecar container to the agent pod whose command is:

```sh
tailscaled --state=mem: --tun=tailscale0 --no-logs-no-support &
TSPID=$!
# wait for the socket …
while true; do
  if tailscale up --auth-key="${TS_AUTHKEY}" \
       --login-server="<headscale_url>" --hostname="${POD_NAME}" \
       --accept-dns=false --timeout=60s; then
    echo "Tailscale authenticated"; break
  fi
  echo "Auth retry in 15s..."; sleep 15
done
wait $TSPID          # ← authenticates once, then just blocks on tailscaled
```

Key properties that matter:

- `--state=mem:` → node identity is in memory; each pod (and any restart of tailscaled) is a *new* registration. There is no durable node to preserve, so **ephemeral is strictly correct**.
- `--hostname="${POD_NAME}"` → unique per pod.
- After the first successful `up`, the script only `wait`s on `tailscaled`. **If the node is ever deauthed/removed server-side, nothing re-ups** — the pod silently loses tailnet reachability until it is recycled.
- Pod `restartPolicy: Never` (`:1153`); `terminationGracePeriodSeconds: 180`.

### Why idleness alone does not cause GC (and what actually does)

`ephemeral_node_inactivity_timeout` is computed from `lastSeen`, not from application traffic. While `tailscaled` holds its control-plane connection, `lastSeen` is refreshed by the coordinator poll/keepalive regardless of whether the agent is doing work, so a connected node is **online and exempt from reaping**. The real exposure is headscale [#2006](https://github.com/juanfont/headscale/issues/2006) — `lastSeen` getting stuck while the node is alive, so headscale wrongly computes `now - lastSeen > timeout` and reaps a *live* node (documented in `docs/done/headscale_mesh.md` under Known Issues). Because we cannot stop headscale's clock from the SRW layer, the resilience design (§3.3, §3.4) targets **recovery from a lost registration**, not prevention of idle GC.

---

## 3. Design

Four parts. §3.1 is the leak fix; §3.2 prevents recurrence via docs; §3.3 and §3.4 are the resilience the brainstorming concluded is worth building given the #2006 risk.

### 3.1 Ephemeral agent key (the core fix)

`headscale-bootstrap.sh:98-103` — add `--ephemeral` to the `preauthkeys create` call:

```bash
headscale preauthkeys create \
  --user "$USER_ID" \
  --reusable \
  --ephemeral \          # NEW: nodes registered with this key are reaped after
  --expiration "$PREAUTH_TTL" \   #      ephemeral_node_inactivity_timeout (30m offline)
  --tags "$PREAUTH_TAGS"
```

- `--reusable` **stays** — all agent pods share this one key; reusable + ephemeral is exactly the pattern the garage-ingress proxy already uses successfully.
- `--expiration` (how long the key may mint *new* nodes) is **unchanged**; it is unrelated to node lifetime.
- Update the `echo` at `:97` to say "reusable, ephemeral, …" and add a one-line comment explaining *why* it must be ephemeral, so the flag is not "cleaned up" later.

No orchestrator code change is required for the key itself — `agent_provisioner.py` keeps consuming `TAILSCALE_AUTH_KEY` from the secret verbatim. The tag remains `tag:agent`, so **ACLs (`tagOwners`, deny-by-default rules) are untouched.**

### 3.2 Design-vault reconciliation

The leak exists because the canonical doc specced the key as long-lived/reusable. These edits keep a future key rotation from re-introducing it:

- `docs/done/headscale_mesh.md`:
  - Agent-keys section (`:304-308`): change "long-lived, reusable" → "reusable **and ephemeral**", add `--ephemeral` to the CLI example, and one sentence noting agent pods are transient (mem state, per-pod hostname) so they must be ephemeral or they leak.
  - Vault secrets table (~`:589`): annotate the agent key row as ephemeral.
  - Security Considerations "Ephemeral nodes" bullet (~`:595`): include agents, not just VMs.
  - Known Issues / "High node churn": add a short dated note (2026-06-03 incident) recording that the missing flag caused the coordinator overload + garage-proxy starvation.
- `docs/issues/local_e2e_testing.md:431` and `docs/features/external_headscale.md:153-154`: make the key-creation recipe / env-knob docs consistent (the agent key is always ephemeral).
- **Not edited here:** the vendored `HomeLab/deployments_managed/headscale/README.md:135` recipe also omits `--ephemeral`, but that file belongs to the HomeLab repo (untracked in SRW). Flag it for the HomeLab session.

### 3.3 Self-healing sidecar loop

Replace the terminal `wait $TSPID` (`agent_provisioner.py:1101`) with a supervision loop that, every ~30s while `tailscaled` is alive, re-`up`s whenever the backend is not `Running`:

```sh
while kill -0 "$TSPID" 2>/dev/null; do
  if ! tailscale status --json 2>/dev/null | grep -q '"BackendState":"Running"'; then
    tailscale up --auth-key="${TS_AUTHKEY}" \
      --login-server="<headscale_url>" --hostname="${POD_NAME}" \
      --accept-dns=false --timeout=60s || true   # reusable+ephemeral → fresh ephemeral node
  fi
  sleep 30
done
```

- `tailscale up` is idempotent when already `Running` (fast no-op), so this is cheap on the happy path.
- Recovers transient loss — #2006 false-GC, headscale restart/redeploy, network/DERP blip — within ~30s, **regardless of root cause**.
- The loop runs **indefinitely** (never gives up). During a real headscale outage, retrying is the correct, harmless behavior; giving up is the health-gate's job (§3.4), gated on a much longer window.

### 3.4 Tunnel-dark health-gate

Goal: a pod whose tunnel is **permanently** dark (key revoked, node persistently rejected, tailscaled wedged) must be recycled — but a *transient* dark window (which §3.3 heals) must not trigger churn. The orchestrator already has the machinery; we reuse it rather than build new state.

**Mechanism — let the kubelet measure duration, let the existing reaper recycle:**

1. **Liveness probe on the tailscale sidecar** (added to the sidecar container in the pod manifest, `agent_provisioner.py:1103-1139`):
   - `exec`: `tailscale status --json | grep -q '"BackendState":"Running"'` (same check as the self-heal loop).
   - `periodSeconds: 30`, `failureThreshold` derived from a tunable window (default ~10 min → `failureThreshold: 20`), and a generous `initialDelaySeconds` (≈120s) so first-auth completes before the probe starts measuring.
   - Sustained failure → kubelet kills the sidecar; `restartPolicy: Never` keeps it terminated. The kubelet's `failureThreshold × periodSeconds` **is** the hysteresis window, so the orchestrator needs no in-memory "how long has it been dark" tracking. The self-heal loop (§3.3) and this probe cooperate: self-heal is the recovery actor, the probe is the sustained-failure detector — a transient blip is healed long before the threshold trips.

2. **Reaper extension** (`agent_provisioner.py`):
   - Add `_has_dead_tunnel_sidecar(pod)` — true when the `tailscale` container has a `terminated` state — mirroring the existing `_has_dead_agent_container()` (`:437`).
   - Add an `_is_tunnel_dark(pod)` predicate and a `tunnel_dark` branch + `stats` key in `reap_pods()` (`:517`, predicate-dispatch at `:566-578`), positioned after `crashed`. Capture the **tailscale sidecar** logs via `_capture_agent_logs_before_reap` (extended to log the relevant container) so "why was it recycled?" is answerable, then `delete_agent_pod()`.
   - `active_counts_by_purpose()` (`:419-426`) must skip tunnel-dark pods (as it already skips dead-agent pods) so a dead-tunnel sidecar doesn't pin the `MAX_AGENTS` ceiling.
   - `ensure_warm_pool()` (`:467`) then provisions a replacement through the normal floor/buffer logic — no new replacement path.

3. **Tunable window** — expose `agent.tailscale.darkTimeoutSeconds` (helm value, default `600`) injected as an env var the provisioner reads (same pattern as `_tailscale_enabled`/`_headscale_url`, `:88-91`); the provisioner computes `failureThreshold = ceil(darkTimeoutSeconds / 30)`.

---

## 4. Behavior summary

| Situation | What happens | Outcome |
|---|---|---|
| Pod alive, tailscaled connected (idle or busy) | `lastSeen` refreshed by control connection → online → exempt from GC | Node persists; **no leak, no false reap** |
| Pod gone (job done / pool scale-down / crash) | tailscaled exits → node offline → reaped after `ephemeral_node_inactivity_timeout` (30m) | Node cleaned up automatically (**the fix**) |
| Transient registration loss (#2006, hs restart, net blip) | self-heal loop re-`up`s within ~30s | Connectivity restored; invisible |
| Permanent dark tunnel | self-heal keeps failing → liveness fails ~10 min → kubelet kills sidecar → reaper `tunnel_dark` → pool replaces | Dead pod recycled, never handed work |

---

## 5. Risks & tradeoffs

- **Headscale outage longer than the gate window** recycles all agent pods, and their replacements also can't register → churn until headscale recovers. Accepted: the window is tunable, retrying replacements are harmless, and a >10-min headscale outage is already a major, alarming incident. We deliberately avoid "is it just me or is headscale down?" detection in the sidecar (complexity not worth it).
- **#2006 is pre-existing**; §3.3 mitigates it rather than fixing headscale.
- **Re-up after a server-side delete with `--state=mem:`** must produce a fresh registration. If `tailscale up` does not re-register cleanly against a deleted node, add `--force-reauth`. To be confirmed during implementation (TDD).
- **No behavior change when `agent.tailscale.enabled=false`** (the chart default): the whole sidecar block is skipped, so default installs are unaffected.

---

## 6. Rollout order (code change is step 1; the rest is operational)

1. **(This change)** Merge code + docs: `--ephemeral`, self-heal loop, health-gate, doc reconciliation.
2. **(Ops — HomeLab session)** Re-run `headscale-bootstrap.sh` (now emits an ephemeral key), `vault kv patch … TAILSCALE_AUTH_KEY=<new>`, force ESO refresh. New pods then register ephemeral.
3. **(Ops — HomeLab session)** Purge the backlog: delete **offline** `tag:agent` nodes under user `srw`, leaving `tag:vm` and the garage nodes untouched.
4. **Verify:** new agent pods register as ephemeral; offline nodes disappear after 30m; headscale CPU drops and registration latency returns to sub-second; both garage-proxy pods stay up and `s3.h4ll.app` holds.

Existing pods keep their current (non-ephemeral) nodes until they cycle; the step-3 purge clears those.

---

## 7. Testing

Following the existing `tests/test_vm_controller.py` (manifest/template-shape assertions) and `tests/test_headscale_client.py` (client unit tests) patterns; add to / create the agent-provisioner test module:

- **Sidecar manifest:** build the agent pod manifest with tailscale enabled and assert the `tailscale` container's args contain the self-heal loop (re-up on non-`Running`) and that the container has the liveness probe with the derived `failureThreshold`.
- **Reaper:** unit-test `_has_dead_tunnel_sidecar` / `_is_tunnel_dark` against fake pod objects (terminated `tailscale` container, live `agent` container) and assert `reap_pods()` categorizes them as `tunnel_dark` and that `active_counts_by_purpose()` excludes them.
- **Bootstrap script:** the shell script is not unit-tested today; at minimum assert in review that the rendered `preauthkeys create` includes `--ephemeral`. (A lightweight test that greps the script for the flag is optional.)
- **Env caveat:** local `pytest` is noisy (Py3.14, missing optional deps); **CI (Py3.12) is the gate**, and the push workflow auto-runs ruff.

---

## 8. Files touched

| File | Change |
|---|---|
| `headscale-bootstrap.sh` | add `--ephemeral`; update echo + comment |
| `orchestrator/services/agent_provisioner.py` | self-heal loop in sidecar args; liveness probe on sidecar; `_has_dead_tunnel_sidecar`/`_is_tunnel_dark` + `tunnel_dark` reap category; skip in `active_counts_by_purpose`; read `darkTimeoutSeconds` env |
| `helm/values.yaml` (+ `templates/`) | `agent.tailscale.darkTimeoutSeconds` value + env injection |
| `docs/done/headscale_mesh.md` | reconcile agent-key sections + incident note |
| `docs/issues/local_e2e_testing.md`, `docs/features/external_headscale.md` | consistent ephemeral recipe/knob docs |
| `tests/` | sidecar-manifest + reaper unit tests |
