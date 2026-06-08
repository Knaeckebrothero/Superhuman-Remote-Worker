# Ephemeral Headscale Registrations for Container Agents — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SRW container-agent headscale registrations ephemeral (so headscale's 30m inactivity GC reclaims them), add a self-healing tailscale sidecar, and recycle pods whose tunnel is permanently dark — stopping the `tag:agent` node leak that overloaded the coordinator.

**Architecture:** One flag on the shared `tag:agent` pre-auth key (`headscale-bootstrap.sh`); a supervision loop + liveness probe on the tailscale sidecar built in `agent_provisioner._build_pod_manifest`; a `tunnel_dark` reap category mirroring the existing `crashed` category; a helm-tunable dark-timeout. No new orchestrator privilege, no per-pod key minting (deferred). Full design: [`docs/superpowers/specs/2026-06-04-headscale-agent-ephemeral-keys-design.md`](../specs/2026-06-04-headscale-agent-ephemeral-keys-design.md).

**Tech Stack:** Python 3.12 (CI gate; local env is Py3.14-noisy — CI is authoritative), pytest + `unittest.mock`, Kubernetes Python client (mocked in tests), Helm, bash.

**Out of scope (operational — HomeLab session owns):** re-minting the key with `--ephemeral`, `vault kv patch TAILSCALE_AUTH_KEY`, ESO refresh, purging the ~1,465-node backlog.

**Setup:** Work on a feature branch off `develop`, e.g. `git switch -c feat/headscale-agent-ephemeral`. All commit commands below end with the project's co-author trailer:
`-m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`.
Run tests with the project interpreter: `python -m pytest <target> -v`.

---

### Task 1: Make the shared `tag:agent` pre-auth key ephemeral

**Files:**
- Modify: `headscale-bootstrap.sh:97-103`
- Test (new): `tests/test_headscale_bootstrap.py`

The agent key is created `--reusable` with no `--ephemeral`, so every pod becomes a permanent node. Add the flag and a regression guard (the spec calls out that a future key rotation must not silently drop it).

- [ ] **Step 1: Write the failing guard test**

Create `tests/test_headscale_bootstrap.py`:

```python
"""Guard: the agent pre-auth key must be ephemeral, or agent nodes leak.

See docs/superpowers/specs/2026-06-04-headscale-agent-ephemeral-keys-design.md.
"""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "headscale-bootstrap.sh"


def test_preauthkey_creation_is_ephemeral():
    text = SCRIPT.read_text()
    # Isolate the `headscale preauthkeys create ...` invocation.
    m = re.search(r"headscale preauthkeys create.*?--tags", text, re.DOTALL)
    assert m, "could not find the preauthkeys create block"
    block = m.group(0)
    assert "--reusable" in block, "agent key must stay reusable (shared key)"
    assert "--ephemeral" in block, "agent key MUST be --ephemeral or agent nodes leak"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_headscale_bootstrap.py -v`
Expected: FAIL — `assert "--ephemeral" in block` (flag absent).

- [ ] **Step 3: Add `--ephemeral` to the script**

In `headscale-bootstrap.sh`, change the echo (line 97) and the create call (lines 98-103):

```bash
echo "Generating pre-auth key (reusable, ephemeral, tags=$PREAUTH_TAGS, TTL=$PREAUTH_TTL)..."
AUTH_KEY=$(kubectl --context "$CONTEXT" -n "$NS" exec "$POD" -- \
  headscale preauthkeys create \
  --user "$USER_ID" \
  --reusable \
  --ephemeral \
  --expiration "$PREAUTH_TTL" \
  --tags "$PREAUTH_TAGS")
```

Add a comment directly above the `headscale preauthkeys create` line:

```bash
# --ephemeral is REQUIRED: agent pods use `tailscaled --state=mem:` + a unique
# per-pod hostname, so each registration is throwaway. Without it, every pod
# becomes a permanent node and they accumulate until the coordinator is
# overloaded (2026-06-03 incident). Ephemeral nodes are GC'd 30m after going
# offline. Do not remove on key rotation.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_headscale_bootstrap.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add headscale-bootstrap.sh tests/test_headscale_bootstrap.py
git commit -m "fix(headscale): make agent pre-auth key ephemeral to stop node leak" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Self-healing supervision loop in the tailscale sidecar

**Files:**
- Modify: `orchestrator/services/agent_provisioner.py:1083-1102` (the `tailscale_args` string inside `_build_pod_manifest`)
- Test: `tests/test_agent_provisioner.py`

Today the sidecar authenticates once then `wait $TSPID` — if the node is ever deauthed/GC'd while alive, nothing re-ups. Replace the blocking tail with a loop that re-`up`s whenever the backend isn't `Running`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent_provisioner.py` (new test class near the end):

```python
# =============================================================================
# TestTailscaleSidecar
# =============================================================================


class TestTailscaleSidecar:
    """Tests for the tailscale sidecar built by _build_pod_manifest()."""

    def _manifest_with_tailscale(self, dark_timeout=600):
        p, _ = _make_provisioner()
        p._tailscale_enabled = True
        p._headscale_url = "https://headscale.test"
        p._tailscale_dark_timeout = dark_timeout
        manifest = p._build_pod_manifest(
            pod_name="srw-agent-test",
            purpose="job",
            thread_id=None,
            config_name="srw-config",
            cpu_request="100m",
            memory_request="256Mi",
            cpu_limit="1000m",
            memory_limit="2Gi",
        )
        return next(
            c for c in manifest["spec"]["containers"] if c["name"] == "tailscale"
        )

    def test_sidecar_has_self_heal_loop(self):
        ts = self._manifest_with_tailscale()
        args = ts["args"][0]
        assert "kill -0" in args, "supervision loop must watch tailscaled"
        assert "BackendState" in args, "must re-up based on backend state"
        assert args.count("tailscale up") >= 2, "initial auth + re-up"
        assert "wait $TSPID" not in args, "blocking tail must be replaced"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_provisioner.py::TestTailscaleSidecar::test_sidecar_has_self_heal_loop -v`
Expected: FAIL — current args end with `wait $TSPID` and contain no `BackendState`/`kill -0`.

- [ ] **Step 3: Replace the blocking tail with the supervision loop**

In `_build_pod_manifest`, the `tailscale_args` assignment currently ends with `'echo "Auth retry in 15s..."; sleep 15; done; '` then `"wait $TSPID"`. Replace the final `"wait $TSPID"` line with:

```python
                'echo "Auth retry in 15s..."; sleep 15; done; '
                # Supervision loop: re-up if the node loses its registration
                # (headscale #2006 lastSeen false-GC, control-plane restart,
                # network/DERP blip). `tailscale up` is idempotent when the
                # backend is already Running. Permanent loss is handled by the
                # liveness probe + the tunnel_dark reaper, not here.
                'while kill -0 "$TSPID" 2>/dev/null; do '
                "if ! tailscale status --json 2>/dev/null | "
                "grep -q '\"BackendState\":\"Running\"'; then "
                "tailscale up "
                '--auth-key="${TS_AUTHKEY}" '
                f'--login-server="{self._headscale_url}" '
                '--hostname="${POD_NAME}" '
                "--accept-dns=false --timeout=60s || true; "
                "fi; sleep 30; done"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_provisioner.py::TestTailscaleSidecar -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/agent_provisioner.py tests/test_agent_provisioner.py
git commit -m "feat(agent-provisioner): self-heal tailscale sidecar on lost registration" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Dark-timeout env + liveness probe on the tailscale sidecar

**Files:**
- Modify: `orchestrator/services/agent_provisioner.py:88-91` (`__init__`, add the env read) and the tailscale container dict at `:1104-1138` (add `livenessProbe`)
- Test: `tests/test_agent_provisioner.py`

The liveness probe gives the kubelet the hysteresis: sustained dark → kubelet kills the sidecar (`restartPolicy: Never` keeps it dead) → Task 4's reaper recycles the pod. `failureThreshold = ceil(darkTimeout / 30)`.

- [ ] **Step 1: Write the failing tests**

Add to `class TestTailscaleSidecar`:

```python
    def test_sidecar_liveness_probe_default_threshold(self):
        ts = self._manifest_with_tailscale(dark_timeout=600)
        probe = ts["livenessProbe"]
        assert "BackendState" in probe["exec"]["command"][-1]
        assert probe["periodSeconds"] == 30
        assert probe["failureThreshold"] == 20  # ceil(600 / 30)
        assert probe["initialDelaySeconds"] >= 90  # startup auth must finish first

    def test_sidecar_liveness_threshold_scales_with_timeout(self):
        ts = self._manifest_with_tailscale(dark_timeout=300)
        assert ts["livenessProbe"]["failureThreshold"] == 10  # ceil(300 / 30)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_agent_provisioner.py::TestTailscaleSidecar -v`
Expected: FAIL — `KeyError: 'livenessProbe'` (sidecar has no probe yet).

- [ ] **Step 3: Read the dark-timeout env in `__init__`**

In `__init__`, immediately after the `self._headscale_url` line (`:91`), add:

```python
        # Sustained-dark window for the tailscale sidecar liveness probe.
        # After this long without a Running backend, the kubelet kills the
        # sidecar and the tunnel_dark reaper recycles the pod. The self-heal
        # loop recovers transient loss well inside this window.
        self._tailscale_dark_timeout: int = int(
            os.environ.get("AGENT_TAILSCALE_DARK_TIMEOUT_SECONDS", "600")
        )
```

- [ ] **Step 4: Add the liveness probe to the tailscale container**

In `_build_pod_manifest`, inside the `containers.append({... "name": "tailscale" ...})` dict (currently `:1104-1138`), add a `"livenessProbe"` key alongside `"resources"`. First compute the threshold just before the `containers.append`:

```python
            # ceil(dark_timeout / 30s); >=1. The kubelet measures the sustained
            # dark window so the reaper needs no duration bookkeeping.
            dark_failures = max(1, (self._tailscale_dark_timeout + 29) // 30)
```

Then add to the tailscale container dict (e.g. right before `"resources"`):

```python
                    "livenessProbe": {
                        "exec": {
                            "command": [
                                "/bin/sh",
                                "-c",
                                "tailscale status --json 2>/dev/null | "
                                "grep -q '\"BackendState\":\"Running\"'",
                            ]
                        },
                        "initialDelaySeconds": 120,
                        "periodSeconds": 30,
                        "failureThreshold": dark_failures,
                    },
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_provisioner.py::TestTailscaleSidecar -v`
Expected: PASS (all four sidecar tests).

- [ ] **Step 6: Commit**

```bash
git add orchestrator/services/agent_provisioner.py tests/test_agent_provisioner.py
git commit -m "feat(agent-provisioner): liveness gate on tailscale sidecar for dark tunnels" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `tunnel_dark` reap category

**Files:**
- Modify: `orchestrator/services/agent_provisioner.py` — add `_has_dead_tunnel_sidecar` + `_is_tunnel_dark`; add `tunnel_dark` to `reap_pods` (`:517` stats + `:566-578` dispatch + signature); parametrize `_capture_agent_logs_before_reap` (`:591`) by category
- Test: `tests/test_agent_provisioner.py` — extend `_make_pod`, add tests, update existing exact-dict assertions

Mirror the existing `crashed` category: a pod whose **`tailscale`** sidecar terminated (the kubelet killed it after sustained dark) but whose **`agent`** container is still alive gets reaped, freeing the pool slot.

- [ ] **Step 1: Extend the `_make_pod` test helper**

In `tests/test_agent_provisioner.py`, add a parameter to `_make_pod` (signature at `:55`). Add `tailscale_terminated_at=None` to the signature, and before `pod.status.container_statuses = statuses` (`:103`) add:

```python
    if tailscale_terminated_at is not None:
        cs = MagicMock()
        cs.name = "tailscale"
        cs.state.waiting = None
        cs.state.terminated.finished_at = datetime.now(timezone.utc) - timedelta(
            seconds=tailscale_terminated_at
        )
        statuses.append(cs)
```

- [ ] **Step 2: Write the failing tests**

Add to `class TestReapPods`:

```python
    @pytest.mark.asyncio
    async def test_reaps_tunnel_dark_pod_past_grace(self):
        # Agent container alive, but the kubelet killed the tailscale sidecar
        # after a sustained dark window. The pod is useless — recycle it.
        p, conn = _make_provisioner()
        conn.fetch.return_value = []
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod("srw-agent-j-dark", phase="Running", tailscale_terminated_at=120),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result["tunnel_dark"] == 1
        assert p._core_api.delete_namespaced_pod.call_count == 1

    @pytest.mark.asyncio
    async def test_preserves_tunnel_dark_pod_within_grace(self):
        p, conn = _make_provisioner()
        conn.fetch.return_value = []
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod("srw-agent-j-dark", phase="Running", tailscale_terminated_at=5),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result["tunnel_dark"] == 0
        assert p._core_api.delete_namespaced_pod.call_count == 0

    @pytest.mark.asyncio
    async def test_dead_agent_takes_precedence_over_tunnel_dark(self):
        # Both containers terminated → "crashed" (the agent dying is the more
        # fundamental failure), not tunnel_dark.
        p, conn = _make_provisioner()
        conn.fetch.return_value = []
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod(
                "srw-agent-j-both-dead",
                phase="Running",
                agent_terminated_at=120,
                tailscale_terminated_at=120,
            ),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result["crashed"] == 1
        assert result["tunnel_dark"] == 0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_agent_provisioner.py::TestReapPods -v`
Expected: FAIL — `KeyError: 'tunnel_dark'` (stats has no such key).

- [ ] **Step 4: Add the predicates**

In `agent_provisioner.py`, right after `_has_dead_agent_container` (`:437-449`), add:

```python
    @staticmethod
    def _has_dead_tunnel_sidecar(pod) -> bool:
        """True if the "tailscale" sidecar container has terminated.

        The kubelet kills the sidecar via its liveness probe after a sustained
        dark window; with restartPolicy=Never it stays terminated. The agent
        container may still be Running, so this is checked independently of
        _has_dead_agent_container.
        """
        for cs in getattr(pod.status, "container_statuses", None) or []:
            if cs.name != "tailscale":
                continue
            state = getattr(cs, "state", None)
            if state and getattr(state, "terminated", None) is not None:
                return True
        return False
```

After `_is_crashed` (`:648-670`), add:

```python
    @staticmethod
    def _is_tunnel_dark(pod, grace_seconds: int) -> bool:
        """Pod is Running but the tailscale sidecar terminated past the grace.

        Brief grace mirrors _is_crashed (let final writes land). The long
        hysteresis already happened in the kubelet liveness probe.
        """
        if pod.status.phase != "Running":
            return False
        for cs in getattr(pod.status, "container_statuses", None) or []:
            if cs.name != "tailscale":
                continue
            state = getattr(cs, "state", None)
            terminated = getattr(state, "terminated", None) if state else None
            if terminated is None:
                return False
            finished_at = getattr(terminated, "finished_at", None)
            if finished_at is None:
                return False
            age = (datetime.now(timezone.utc) - finished_at).total_seconds()
            return age >= grace_seconds
        return False
```

- [ ] **Step 5: Wire `tunnel_dark` into `reap_pods`**

In `reap_pods` (`:517`): add `tunnel_dark_grace_seconds: int = 60` to the signature; add `"tunnel_dark": 0,` to the `stats` dict; and add a dispatch branch **after** `crashed` (so a dead agent wins) in the `:566-578` chain:

```python
            elif self._is_crashed(pod, crashed_grace_seconds):
                category = "crashed"
            elif self._is_tunnel_dark(pod, tunnel_dark_grace_seconds):
                category = "tunnel_dark"
            elif self._is_stale_running(pod, offline_hostnames):
                category = "stale"
```

Also update the `reap_pods` docstring list to mention `tunnel_dark` (Running + tailscale sidecar terminated).

- [ ] **Step 6: Capture the right container's logs on reap**

In `_capture_agent_logs_before_reap` (`:591`), pick the container by category. At the top of the method add:

```python
        target = "tailscale" if category == "tunnel_dark" else "agent"
```

Then replace the two literal `"agent"` uses: the `if cs.name != "agent":` guard (`:605`) becomes `if cs.name != target:`, and `container="agent"` in the `read_namespaced_pod_log` call (`:617`) becomes `container=target`.

- [ ] **Step 7: Update the existing exact-dict assertions**

Adding the stats key breaks 4 tests that assert the full dict. Add `"tunnel_dark": 0,` to each expected dict:

- `test_noop_when_k8s_not_available` (`:125-130`)
- `test_noop_when_no_reapable_pods` (`:145-151`)
- `test_preserves_pending_pods_with_benign_waiting_reason` (`:336-343`)

Each of those becomes:

```python
        assert result == {
            "completed": 0,
            "crashed": 0,
            "tunnel_dark": 0,
            "stale": 0,
            "drained": 0,
            "unstartable": 0,
        }
```

- `test_mixed_pod_set_reaps_all_three_categories` (`:411-417`) becomes:

```python
        assert result == {
            "completed": 1,
            "crashed": 0,
            "tunnel_dark": 0,
            "stale": 1,
            "drained": 0,
            "unstartable": 1,
        }
```

- [ ] **Step 8: Run the full reap suite to verify it passes**

Run: `python -m pytest tests/test_agent_provisioner.py::TestReapPods -v`
Expected: PASS (new tunnel_dark tests + all updated dict assertions).

- [ ] **Step 9: Commit**

```bash
git add orchestrator/services/agent_provisioner.py tests/test_agent_provisioner.py
git commit -m "feat(agent-provisioner): add tunnel_dark reap category for dead tailscale sidecar" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Skip tunnel-dark pods in `active_counts_by_purpose`

**Files:**
- Modify: `orchestrator/services/agent_provisioner.py:419-426`
- Test: `tests/test_agent_provisioner.py` (`class TestActiveCountsByPurpose`, `:769`)

A tunnel-dark pod is a zombie awaiting reap; it must not pin the `MAX_AGENTS` ceiling — exactly like a dead-agent pod.

- [ ] **Step 1: Write the failing test**

Add to `class TestActiveCountsByPurpose`:

```python
    @pytest.mark.asyncio
    async def test_excludes_tunnel_dark_pods(self):
        p, _ = _make_provisioner()
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod("j-ok", purpose="job"),
            _make_pod("j-dark", purpose="job", tailscale_terminated_at=120),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            counts = await p.active_counts_by_purpose()
        assert counts["job"] == 1  # the tunnel-dark pod is not counted
        assert counts["total"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_provisioner.py::TestActiveCountsByPurpose::test_excludes_tunnel_dark_pods -v`
Expected: FAIL — `counts["job"] == 2` (dark pod still counted).

- [ ] **Step 3: Add the skip**

In `active_counts_by_purpose`, right after the existing dead-agent skip (`:425-426`):

```python
                if self._has_dead_agent_container(pod):
                    continue
                if self._has_dead_tunnel_sidecar(pod):
                    continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_provisioner.py::TestActiveCountsByPurpose -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/agent_provisioner.py tests/test_agent_provisioner.py
git commit -m "fix(agent-provisioner): exclude tunnel-dark zombies from active agent count" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Helm wiring for the dark-timeout

**Files:**
- Modify: `helm/values.yaml:142-144` (`agent.tailscale`), `helm/templates/configmap.yaml:218-220`, `helm/templates/orchestrator/deployment.yaml:762-768`

Expose `darkTimeoutSeconds` so operators can widen the gate on small/idle deployments. No pytest; verify by rendering.

- [ ] **Step 1: Add the value**

In `helm/values.yaml`, the `agent.tailscale` block becomes:

```yaml
  tailscale:
    enabled: false
    image: ghcr.io/tailscale/tailscale:v1.82.5
    # -- Seconds the sidecar may stay disconnected before the liveness probe
    # kills it and the tunnel_dark reaper recycles the pod. Widen on small
    # deployments with long-idle pool agents. Self-heal recovers transient loss.
    darkTimeoutSeconds: 600
```

- [ ] **Step 2: Add the ConfigMap key**

In `helm/templates/configmap.yaml`, after the `AGENT_TAILSCALE_ENABLED` line (`:220`):

```yaml
  AGENT_TAILSCALE_ENABLED: {{ .Values.agent.tailscale.enabled | quote }}
  # Sustained-dark window (seconds) for the agent tailscale sidecar liveness probe.
  AGENT_TAILSCALE_DARK_TIMEOUT_SECONDS: {{ .Values.agent.tailscale.darkTimeoutSeconds | default 600 | quote }}
```

- [ ] **Step 3: Add the Deployment env**

In `helm/templates/orchestrator/deployment.yaml`, after the `AGENT_TAILSCALE_ENABLED` env block (`:763-768`):

```yaml
            - name: AGENT_TAILSCALE_DARK_TIMEOUT_SECONDS
              valueFrom:
                configMapKeyRef:
                  name: {{ include "srw.configMapName" . }}
                  key: AGENT_TAILSCALE_DARK_TIMEOUT_SECONDS
                  optional: true
```

- [ ] **Step 4: Verify the render**

Run:
```bash
helm template helm/ | grep -A2 AGENT_TAILSCALE_DARK_TIMEOUT_SECONDS
```
Expected: the ConfigMap shows `AGENT_TAILSCALE_DARK_TIMEOUT_SECONDS: "600"` and the orchestrator Deployment shows the `configMapKeyRef` env entry. (If `helm` is unavailable locally, confirm by inspection that all three files reference the key with identical spelling.)

- [ ] **Step 5: Commit**

```bash
git add helm/values.yaml helm/templates/configmap.yaml helm/templates/orchestrator/deployment.yaml
git commit -m "feat(helm): expose agent.tailscale.darkTimeoutSeconds for the tunnel-dark gate" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Reconcile the design vault

**Files:**
- Modify: `docs/done/headscale_mesh.md` (`:304-308`, Vault table `~:589`, Security "Ephemeral nodes" `~:595`, Known Issues "High node churn")
- Modify: `docs/issues/local_e2e_testing.md:431`
- Modify: `docs/features/external_headscale.md:153-154`

Prevents a future key rotation from silently dropping `--ephemeral`. No test; documentation only.

- [ ] **Step 1: Fix the agent-keys section in `headscale_mesh.md`**

Replace the "Agent keys — long-lived, reusable" heading/sentence (`:304`) and its CLI example (`:307`) with:

```markdown
**Agent keys** — reusable **and ephemeral**, stored in Vault/ESO. Agent pods are
transient (`tailscaled --state=mem:`, unique per-pod hostname), so without
`--ephemeral` every pod becomes a permanent node and they leak (see Known Issues).

```bash
headscale preauthkeys create --user srw --reusable --ephemeral --expiration 365d --tags tag:agent
```
```

- [ ] **Step 2: Annotate the Vault secrets table**

In the secrets table (`~:589`), change the agent-key row description to: `Reusable + ephemeral pre-auth key for agent pods (tag:agent)`.

- [ ] **Step 3: Update the "Ephemeral nodes" security bullet**

In Security Considerations (`~:595`), change the bullet to note **both** VMs and agent pods register as ephemeral nodes reclaimed after `ephemeral_node_inactivity_timeout`.

- [ ] **Step 4: Add the incident note under "High node churn"**

Append to the "High node churn" known-issue bullet:

```markdown
  *2026-06-03 incident:* the agent key was created without `--ephemeral`, so
  ~1,465 `tag:agent` nodes accumulated and overloaded the coordinator (~486m CPU,
  ~28s/registration), starving an unrelated tailscale sidecar into a crash loop.
  Fixed by making the agent key ephemeral + a self-healing sidecar + a
  tunnel_dark reaper. See
  docs/superpowers/specs/2026-06-04-headscale-agent-ephemeral-keys-design.md.
```

- [ ] **Step 5: Fix the e2e + external-headscale recipes**

In `docs/issues/local_e2e_testing.md:431`, add `--ephemeral` to the `headscale preauthkeys create` command. In `docs/features/external_headscale.md:153-154` (the `PREAUTH_KEY_*` knob docs), add a line noting the agent pre-auth key is always created `--ephemeral`.

- [ ] **Step 6: Commit**

```bash
git add docs/done/headscale_mesh.md docs/issues/local_e2e_testing.md docs/features/external_headscale.md
git commit -m "docs(headscale): reconcile design vault with ephemeral agent keys" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full affected test module**

Run: `python -m pytest tests/test_agent_provisioner.py tests/test_headscale_bootstrap.py -v`
Expected: all PASS. (Local Py3.14 env may emit unrelated collection noise — the CI Py3.12 run is the gate.)

- [ ] **Step 2: Lint**

Run: `ruff check orchestrator/services/agent_provisioner.py tests/test_agent_provisioner.py tests/test_headscale_bootstrap.py`
Expected: no errors. Fix any and amend the relevant commit.

- [ ] **Step 3: Confirm spec coverage**

Eyeball that each spec section (§3.1 key, §3.2 docs, §3.3 self-heal, §3.4 health-gate) maps to a committed task. The operational steps in spec §6 are intentionally NOT in this plan.

- [ ] **Step 4: Push and open a PR (only if the user asks)**

```bash
git push -u origin feat/headscale-agent-ephemeral
```

---

## Notes for the implementer

- **Why the agent key stays `--reusable`:** all agent pods share one key from a static Secret; we are not minting per-pod keys (deferred — see spec §1 non-goals). Reusable **+** ephemeral is the proven garage-ingress pattern.
- **Re-up after server-side delete (spec §5 open item):** if a node was deleted server-side and `tailscale up` does not cleanly re-register against the in-memory node identity, add `--force-reauth` to **both** the initial-auth `up` and the supervision-loop `up`. Confirm against a real headscale during cluster verification; if needed, it's a one-line addition to the `tailscale_args` string in Task 2/`_build_pod_manifest`.
- **No behavior change when `agent.tailscale.enabled=false`** (chart default): the whole sidecar block is skipped, so default installs are unaffected by every code change here.
