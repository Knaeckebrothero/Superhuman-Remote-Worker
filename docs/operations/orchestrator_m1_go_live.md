# Finish M1 — Go Live with `replicas: 2` (Orchestrator HA)

> Companion to `docs/features/orchestrator_ha_scaling.md` (Milestones M0 + M1) and
> the failover runbook `docs/operations/orchestrator_failover.md`. M0 (active-passive
> hardening) and M1 (leader election) are **shipped on `origin/develop` and
> k3d-verified**, but the chart still defaults to `replicas: 1` — so the orchestrator
> is still a hard SPOF *in practice*. This runbook is the one-time operation that
> turns HA on: validate the M1 wiring on the live dev cluster, then flip the count.
>
> **Run on dev, in a quiet window.** Steps 4-5 are destructive (they delete the
> leader pod and drain a node). Nobody should be mid-test on the cluster.

The k3d two-replica run already proved the mechanism (one leader, loops leader-gated,
failover ×2, graceful step-down — see `docs/tests/orchestrator_m1_leader_election_verification.md`).
This runbook re-confirms it on a **multi-node cluster under real traffic** and adds
what k3d couldn't: a mid-dispatch job re-dispatched exactly once, a persistent session
reattach, no duplicate emails, and the **hard-kill (~40s keepalive) path**.

```bash
# Operator sets these once.
CTX=--context=dev                 # your dev kube-context
NS=superhuman-remote-worker
SEL='-l app.kubernetes.io/component=orchestrator'
```

---

## 0. Preconditions (check before touching anything)

- [ ] **M1 code is actually on dev.** Fleet renders `srw-dev` from the chart version pinned in `deployment/fleet.yaml`; CI bumps `deployment/values-experimental.yaml` image tags on every `develop` push. Confirm the *running* orchestrator carries the leader-election code — at `replicas: 1` the leader task still runs, so its log line must be present:
  ```bash
  kubectl $CTX -n $NS logs $SEL --tail=-1 | grep -m1 "leader_election: acquired leadership" \
    || echo "M1 NOT on dev yet — wait for CI to publish + Fleet to sync the post-M1 image"
  ```
  If absent, stop: dev is still on a pre-M1 image. Check `kubectl $CTX -n $NS get deploy srw-orchestrator -o jsonpath='{.spec.template.spec.containers[0].image}'` against the latest `develop` build sha.
- [ ] **Postgres is NOT behind a transaction-mode pooler.** Session advisory locks break behind PgBouncer/pgcat/RDS-Proxy in txn mode. Dev uses the in-cluster Postgres (direct) — fine. If dev were ever pointed at an external pooled endpoint (`databases.postgres.externalHost`), this whole scheme silently fails. (See `docs/researches/orchestrator_leader_election.md`.)
- [ ] **M0 hardening is live** (it shipped in the same push): `kubectl $CTX -n $NS get deploy srw-orchestrator -o yaml | grep -E "preStop|terminationGracePeriodSeconds|startupProbe"` shows the drain hook + grace + startup probe.
- [ ] **Quiet window confirmed.** Announce it; nobody mid-test.

---

## 1. Baseline (at `replicas: 1`)

```bash
kubectl $CTX -n $NS get pods $SEL -o wide
kubectl $CTX -n $NS get pdb srw-orchestrator-pdb     # expect minAvailable: 0 at replicas:1
```
Record: one pod, Ready. Start a health poll in a side terminal (see the poll loop in `orchestrator_failover.md` §Chaos test) so you can measure any blackout during the flip.

---

## 2. The flip — `replicas: 2` + `pdb.minAvailable: 1`

Add a **top-level `orchestrator:` block** to `deployment/values-experimental.yaml` (today it only has `image.orchestrator.tag`; the chart default is `replicas: 1` / `pdb.minAvailable: 0`):

```yaml
# deployment/values-experimental.yaml  (top level, NOT under image:)
orchestrator:
  replicas: 2
  pdb:
    # At replicas:2 this becomes 1 (chart comment: "Flip minAvailable to 1 when
    # replicas>=2"). Keeps one orchestrator up during a voluntary disruption,
    # while still allowing a single-pod drain.
    minAvailable: 1
```

Then commit + push to `develop` and let Fleet apply it:

```bash
git add deployment/values-experimental.yaml
git commit -m "deploy(dev): orchestrator replicas:2 + pdb.minAvailable:1 — M1 go-live"
git push origin develop
# Fleet syncs within its polling interval; or force a reconcile if you run the Fleet CLI.
kubectl $CTX -n $NS rollout status deploy/srw-orchestrator --timeout=5m
```

> **Why not `helm/values.yaml`?** That changes the chart default for *every* install (incl. prod). Keep the default at 1; flip dev via the Fleet override first. The default-count flip is the very last step (§7), done deliberately once dev has soaked.

---

## 3. Verify steady state (exactly one leader, loops gated)

```bash
# Two pods, both Ready.
kubectl $CTX -n $NS get pods $SEL -o custom-columns=\
'POD:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount'

# Exactly ONE pod logs leadership; the other does not.
for p in $(kubectl $CTX -n $NS get pods $SEL -o jsonpath='{.items[*].metadata.name}'); do
  echo "$p acquired=$(kubectl $CTX -n $NS logs $p | grep -c 'acquired leadership')"
done
```
- [ ] Exactly one pod with `acquired=1`; the other `acquired=0`.
- [ ] **DB confirms a single lock holder** (exec into the Postgres pod; adjust pod/user):
  ```bash
  kubectl $CTX -n $NS exec srw-postgres-0 -- \
    psql -U srw -d srw -tAc \
    "SELECT count(*), array_agg(DISTINCT a.client_addr) \
       FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid \
      WHERE l.locktype='advisory' AND l.granted;"
  # Expect: count = 1, client_addr = the leader pod's IP. (key = 0x5352575F4C454144 'SRW_LEAD')
  ```
- [ ] **`run_when_leader` gates the singletons.** The leader logs the 7 leader-gated loop starts; the follower logs none of them:
  ```bash
  PAT='Auto-assign dispatcher started|Stale agent detector started|Agent pool reconciler started|Lifecycle reconciler loop started|Delegation timeout sweeper started|permission-notify sweeper started|Quota poll loop'
  for p in $(kubectl $CTX -n $NS get pods $SEL -o jsonpath='{.items[*].metadata.name}'); do
    echo "$p gated-loop-starts=$(kubectl $CTX -n $NS logs $p | grep -ciE "$PAT")"
  done
  # Expect: leader = 7, follower = 0. Non-gated loops (cron dispatcher, prune
  # sweepers, project-loop, …) run on BOTH — that's correct.
  ```
- [ ] The health poll showed **no blackout** during the rollout (peers stay up — that's the M1 payoff vs M0's single-pod bounce).

---

## 4. Failover tests

### 4a. Graceful leader kill (the everyday case)

```bash
LEADER=$(for p in $(kubectl $CTX -n $NS get pods $SEL -o jsonpath='{.items[*].metadata.name}'); do
  kubectl $CTX -n $NS logs $p | grep -q 'acquired leadership' && echo $p; done | head -1)
echo "leader=$LEADER"
kubectl $CTX -n $NS delete pod "$LEADER" --wait=false
# Watch a surviving pod log 'acquired leadership' and the dispatcher/loops resume.
kubectl $CTX -n $NS get pods $SEL -w
```
- [ ] A surviving replica re-acquires leadership; **never two holders** (re-run the §3 DB check mid-failover — count stays ≤ 1).
- [ ] Expected wall-clock **~20s**, dominated by `preStopDrainSeconds: 15` (the dying leader holds the lock until its `finally` `pg_advisory_unlock`) + the ~10s follower poll. The dying pod logs `released leadership` (graceful step-down — a clean handoff, not a timeout).

### 4b. Hard kill (the ~40s keepalive path — k3d did NOT cover this)

```bash
LEADER=$(... as above ...)
kubectl $CTX -n $NS delete pod "$LEADER" --grace-period=0 --force
```
- [ ] No graceful unlock fires; the lock is held by the dead Postgres session until **TCP-keepalive detection (~40s** with the chart's `tcp_keepalives_idle/interval/count = 10/10/3`), then a survivor acquires. Confirm recovery completes in ~40-50s, not ~2h (the untuned default). This is the worst-case failover and the reason the keepalives were tuned.

### 4c. Exactly-once dispatch + session reattach under real traffic

Set up the three in-flight conditions from `orchestrator_failover.md` §Chaos test (a job mid-dispatch, an open sudo prompt, a session mid-turn), then kill the leader (4a) **during** them:
- [ ] The mid-dispatch job ends `paused` then is re-dispatched **exactly once** (the dispatcher is leader-gated + the CAS `claim_job_for_agent` guards the transient dual-leader window). Check `list_jobs` / the job's audit trail for a single assignment.
- [ ] The persistent session reattaches after a cockpit refresh; **no duplicated assistant turn**.
- [ ] **No duplicate emails** — if an IMAP reply / a quiet-hours digest / a permission-pending email is in flight across the failover, exactly one is sent (Task-5 DB guards). Spot-check the recipient inbox + `message_log` / `thread_notifications`.

---

## 5. PDB test — surgical, **NOT** a full node drain

> **Do NOT `kubectl drain` a whole node on a packed cluster.** On the homelab the
> orchestrator nodes also host the **production** orchestrator (`srw-prod-private`),
> the data tier (`srw-postgres`/`pgvector`/`auditdb`), `nats`, the LLM inference
> pods, and the Cloudflare tunnel — a full drain evicts *all* of them (homelab-wide
> outage). The PDB only needs a single-pod eviction to verify, so evict **only the
> follower** orchestrator pod and watch the budget close.

```bash
# Follower = the pod whose log has NO "acquired leadership".
FNODE=$(kubectl $CTX -n $NS get pod <follower> -o jsonpath='{.spec.nodeName}')
# --pod-selector evicts ONLY the orchestrator pod off that node (not its neighbours);
# uncordon immediately so the brief cordon can't strand a critical neighbour.
kubectl $CTX drain "$FNODE" --pod-selector='app.kubernetes.io/component=orchestrator' \
  --ignore-daemonsets --delete-emptydir-data --disable-eviction=false --timeout=8s; \
  kubectl $CTX uncordon "$FNODE"
kubectl $CTX -n $NS get pdb srw-orchestrator-pdb -o jsonpath='{.status.disruptionsAllowed}{"\n"}'
```
- [ ] The follower eviction is **accepted** (`pdb.minAvailable: 1` permits 1 disruption), then `disruptionsAllowed` drops **1 → 0** — proving the PDB now protects the remaining leader (a 2nd voluntary eviction would `429`). The evicted pod **reschedules onto another node**; the leader (advisory-lock holder) is **unaffected**.

> The raw policy/v1 Eviction API (`kubectl create --raw …/pods/<pod>/eviction`) is
> cleaner (no cordon) but **does not route through a Rancher kube-API proxy** — even
> a raw GET returns a bogus "namespace not found". `kubectl drain --pod-selector`
> uses the eviction API through kubectl's normal path, which Rancher *does* proxy.
> Eviction RBAC is fine regardless (`kubectl auth can-i create pods/eviction` → yes).

---

## 6. Decision + record

- **PASS:** record the results (downtime numbers, failover times for 4a/4b, exactly-once confirmations) in `docs/tests/orchestrator_m1_leader_election_verification.md` — move the "live (dev) cluster run" out of "Still owed". Leave dev at `replicas: 2` to soak.
- **FAIL / anomaly:** roll back (§7), capture logs (`kubectl $CTX -n $NS logs <pod> --previous`), and file against `docs/features/orchestrator_ha_scaling.md`.

---

## 7. Rollback (if needed) — and the eventual default flip

**Rollback dev:** revert the §2 change and let Fleet scale back to 1:
```bash
git revert <the replicas:2 commit>   # or delete the orchestrator: block from values-experimental.yaml
git push origin develop
```
At `replicas: 1` the leader task simply always wins its own lock — no behavior change, fully safe to revert at any time.

**The default flip (do LAST, after dev soaks):** only once dev has run `replicas: 2` cleanly for a while, bump the chart default in `helm/values.yaml` (`orchestrator.replicas: 1 → 2`, `pdb.minAvailable: 0 → 1`) so fresh installs are HA by default. That's the M4/Phase-5 "declare done" step — and the point at which README/marketing may state the orchestrator is multi-replica. **Do not flip the chart default before this runbook passes on a live cluster.**

---

## Pass criteria (summary)

| # | Criterion |
|---|---|
| Steady state | 2 pods Ready; exactly one leader (1 granted advisory lock); 7 gated loops on leader, 0 on follower; no blackout on the flip |
| Graceful failover (4a) | survivor re-acquires ~20s; never two holders; dying leader logs `released leadership` |
| Hard failover (4b) | survivor re-acquires ~40-50s (keepalive detection), not hours |
| Exactly-once (4c) | mid-dispatch job re-dispatched once; no dup assistant turn; no dup emails |
| Drain (5) | drain proceeds at `minAvailable: 1`; pod reschedules; leadership intact |
