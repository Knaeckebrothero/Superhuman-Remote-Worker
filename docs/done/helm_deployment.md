# Fleet Helm Deployment Issues — 2026-04-17

After resolving the initial fresh-deploy issues (see `helm_fresh_deploy_issues.md`),
Fleet still fails to deploy a fully working stack. This document tracks the
remaining Fleet/Helm integration issues blocking production deployment.

---

## Issue A: Fleet bundle does not include Helm chart content

**Severity**: Critical (root cause of all deployment failures)

**Symptom**: The postgres StatefulSet deployed by Fleet is missing the
`lifecycle.postStart` hook that exists in the local Helm template. Local
`helm template` renders the hook correctly, but the Helm release on the
cluster does not contain it.

**Root cause**: The Fleet `GitRepo` watches `paths: ["deployment"]`. The
`deployment/fleet.yaml` references `chart: ../helm`, but Fleet only packages
files from within the watched path into the bundle's content resource. The
`helm/` directory is outside `deployment/`, so Fleet cannot resolve the
`../helm` chart reference against the latest git content. Instead, it appears
to use a stale cached version of the chart from a previous successful
deployment (pre-lifecycle-hook).

**Evidence**:
- `kubectl get contents <id> -o jsonpath='{.content}' | base64 -d | gunzip`
  shows only `deployment/` files (legacy/, nats/, deploy.sh, etc.) — no
  `helm/` templates.
- The Helm release secret (v5) contains rendered manifests without the
  `lifecycle` block on the postgres container, even though the bundle label
  says `fleet.cattle.io/commit: fba1f0a` (the latest commit which includes
  the hook).
- Local `helm template srw helm/ -f deployment/values-experimental.yaml`
  renders the lifecycle hook correctly.

**Attempted fixes and results**:

| Attempt | Result |
|---------|--------|
| Add `helm` to GitRepo `paths` | Fleet creates a second bundle from `helm/Chart.yaml` as a standalone chart (no values) → `ErrApplied: global.domain is required` |
| Remove `helm` from GitRepo `paths` | Only one bundle, but `../helm` chart content is stale/missing |
| Delete bundle + force-sync | Fleet rebuilds bundle but still packages stale chart content |
| Delete BundleDeployment + force-sync | Fleet recreates BundleDeployment, deploys, but lifecycle hook still absent |

**Proposed solution**: Add `helm/fleet.yaml` with `targets: []` so Fleet
scans `helm/` (providing content) without deploying it as a standalone
bundle:

```yaml
# helm/fleet.yaml
targets: []
```

Combined with `paths: ["deployment", "helm"]` in the GitRepo. **Untested** —
it is unclear whether Fleet shares content across bundles from separate paths,
or whether each bundle only gets files from its own path.

**Alternative solutions** (if the above doesn't work):

1. **Move chart into `deployment/chart/`** — guaranteed to work since the
   chart is within the bundle's path. Requires updating CI references to
   `helm/`.
2. **Move `fleet.yaml` to repo root** with `chart: helm` and
   `valuesFiles: ["deployment/values-experimental.yaml"]`. Requires careful
   handling to avoid triggering bundles from other directories (HomeLab,
   deployment-vms, etc.).
3. **Publish chart to OCI/HTTP repo** and reference via `helm.repo` in
   `fleet.yaml`. Adds build step but cleanly separates chart packaging from
   deployment config.

---

## Issue B: Orphaned BundleDeployments block Fleet reconciliation

**Severity**: High (caused 3+ hour outage)

**Symptom**: After applying the GitRepo and deleting the namespace, Fleet
showed the bundle as "Modified" with 80/91 resources missing, but no pods
were created for 3+ hours.

**Root cause**: A stale `BundleDeployment` on the downstream fleet-agent
(namespace `cluster-fleet-default-c-9bthm-*`) referenced a missing
`contents.fleet.cattle.io` resource. Fleet could not deploy because the
content didn't exist, but it also didn't recreate it automatically.

The error in the BundleDeployment conditions was:
```
contents.fleet.cattle.io "s-5ad4523d..." not found
```

**Fix applied**: Manually deleted the BundleDeployment:
```bash
kubectl --context local delete bundledeployment superhuman-remote-worker-deployment \
  -n cluster-fleet-default-c-9bthm-1ad1c8198b02
```
Fleet then recreated the BundleDeployment and deployed successfully.

**Lesson**: When Fleet is stuck in Modified/ErrApplied after major
reconfigurations, check the BundleDeployment on the Rancher cluster for
stale content references. Deleting the BundleDeployment forces a clean
rebuild.

---

## Issue C: Keycloak PostStartHookError — password auth failed

**Severity**: Critical (blocks entire dependency chain)

**Symptom**: Even after Fleet deploys the chart, Keycloak fails with:
```
FATAL: password authentication failed for user "keycloak"
DETAIL: Role "keycloak" does not exist.
```

**Root cause**: This is a direct consequence of Issue A. The postgres
postStart lifecycle hook (which creates the `keycloak` role idempotently on
every start) was not included in the deployed StatefulSet because Fleet used
stale chart content. The `docker-entrypoint-initdb.d/init_sso_dbs.sh` script
was also skipped because the PVC has existing data from a previous
incarnation (Longhorn `Retain` policy).

**Dependency cascade**:
```
postgres (Running, but no keycloak role)
  → keycloak (PostStartHookError / CrashLoopBackOff)
    → gitea (Init:0/1 — wait-for-keycloak)
      → orchestrator (Init:3/5 — wait-for-gitea)
        → cockpit (Init:0/1 — wait-for-orchestrator)
        → mcp (Init:0/1 — wait-for-orchestrator)
    → opencloud (Init:0/1 — wait-for-keycloak)
```

**Fix**: Resolving Issue A will deploy the postgres lifecycle hook, which
creates the keycloak role on start, breaking the deadlock.

---

## Issue D: Recurring orphaned helm bundle

**Severity**: Low (cosmetic errors in Fleet status, no functional impact
once `helm/fleet.yaml` with `targets: []` is deployed)

**Symptom**: After removing `helm` from GitRepo paths and deleting the helm
bundle, the `superhuman-remote-worker-helm` BundleDeployment kept getting
recreated on the downstream agent.

**Root cause**: Fleet controller re-creates BundleDeployments to match the
bundle spec. Even after deleting the bundle on Rancher, the fleet-agent
retried from its local cache. The error-loop in fleet-agent logs showed
exponential backoff retries up to ~17 minute intervals.

**Fix**: Adding `helm/fleet.yaml` with `targets: []` cleanly prevents
deployment. Without it, removing `helm` from paths is the only option (but
breaks Issue A resolution).

---

## Resolution Plan

1. **Push `helm/fleet.yaml`** with `targets: []` to develop branch
2. **Update GitRepo** to `paths: ["deployment", "helm"]`
3. **Force Fleet re-sync** and verify the deployment bundle's content
   resource now includes `helm/` templates with the lifecycle hook
4. **If step 3 fails** (Fleet doesn't share content across path bundles):
   fall back to moving the chart into `deployment/chart/`
5. **Verify cascade**: postgres lifecycle hook creates keycloak role →
   keycloak starts → gitea → orchestrator → cockpit/mcp/opencloud
6. **Clean up orphaned PVs** (48 Released PVs, ~350 GiB — see
   `helm_fresh_deploy_issues.md` Issue 6)
