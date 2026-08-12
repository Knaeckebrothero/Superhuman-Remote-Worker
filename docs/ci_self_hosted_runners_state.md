# Self-hosted CI runners — state of play

**Status: paused mid-bring-up, 2026-08-09.** Infrastructure is deployed and
correct; one bad credential blocks it. No CI job routes to self-hosted yet, so
nothing is at risk while this sits.

This is the *handoff* doc — where the work stands and what to do next. The
reference doc explaining how the thing works is
[`ci_self_hosted_runners.md`](./ci_self_hosted_runners.md), and the operational
runbook is `HomeLab/deployments_managed/arc-base/README.md`.

---

## 1. The one blocker

The GitHub App private key in Vault **has no newlines**. It arrived as a single
line:

```
-----BEGIN RSA PRIVATE KEY-----MIIEogIBA…8hF0glEh6A=-----END RSA PRIVATE KEY-----
```

The key *material* is a valid 2048-bit RSA key — only the line wrapping was lost.
Go's `pem.Decode` (which ARC uses) is strict and refuses it:

```
failed to create JWT for GitHub app: failed to parse RSA private key from PEM:
invalid key: Key must be a PEM encoded PKCS1 or PKCS8 key
```

**The symptom is deeply misleading.** ExternalSecret says `SecretSynced=True`,
the Secret has all three keys, the controller is `Running`, Fleet is green — and
yet no listener pod is ever created and `AutoscalingRunnerSet.status` sits at
`{"phase": "Pending"}`. The only evidence is in the controller log. Note also
that Python's `cryptography` loads the mangled key happily, so "I checked, the
key is valid" proves nothing here; `openssl rsa -check` is strict the way Go is.

### Fix

The rebuilt PEM was written to the session scratchpad, which **is gone after the
reboot**. Either re-run `vault kv patch` with the original `.pem` GitHub gave
you, or regenerate it from the value already in the cluster:

```bash
# 1. Rebuild a correctly-wrapped PEM from the Secret (no GitHub download needed)
kubectl --context=main -n arc-runners get secret arc-github-app \
  -o jsonpath='{.data.github_app_private_key}' | base64 -d > /tmp/pk.raw

python3 - <<'PY'
import re, textwrap, pathlib
raw = pathlib.Path("/tmp/pk.raw").read_text()
m = re.match(r"^-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----\s*$", raw.strip(), re.S)
label, body = m.group(1), re.sub(r"\s+", "", m.group(2))
pem = f"-----BEGIN {label}-----\n" + "\n".join(textwrap.wrap(body, 64)) + f"\n-----END {label}-----\n"
p = pathlib.Path("/tmp/arc-app-key.pem"); p.write_text(pem); p.chmod(0o600)
print("wrote", p, len(pem), "bytes,", pem.count("\n"), "newlines")
PY

# 2. Verify with a STRICT decoder before trusting it
openssl rsa -in /tmp/arc-app-key.pem -noout -check      # expect: RSA key ok

# 3. Write it back — @file is what matters, it passes bytes through verbatim
vault kv patch secret/homelab/arc-runners/arc-github-app \
  github_app_private_key=@/tmp/arc-app-key.pem

# 4. ESO refreshes hourly and the controller caches the credential, so neither
#    picks up a Vault change on its own. Force both.
kubectl --context=main -n arc-runners annotate externalsecret arc-github-app \
  force-sync="$(date +%s)" --overwrite
kubectl --context=main -n arc-system rollout restart deploy/arc-gha-rs-controller

# 5. Clean up
rm -f /tmp/pk.raw /tmp/arc-app-key.pem
```

**Never** use `github_app_private_key="$(cat key.pem)"` — command substitution
strips the trailing newline and some paths collapse the interior ones.

Success looks like two listener pods appearing in `arc-system` within a minute or
two, and `kubectl -n arc-runners get autoscalingrunnersets` showing a non-empty
`STATE` column.

---

## 2. Live cluster state (verified 2026-08-09)

| Thing | State |
|---|---|
| 4 `actions.github.com` CRDs | installed |
| `arc-gha-rs-controller` | Running on node3 |
| `arc-github-app` ExternalSecret | `SecretSynced=True`, 3 keys (values bad — §1) |
| `ci-runner-low` PriorityClass | `-1000`, `preemptionPolicy: Never` |
| LimitRange / ResourceQuota / NetworkPolicy | all applied in `arc-runners` |
| `srw-node4` / `srw-node4-kvm` scale sets | exist, min 0 / max 3 and 2 |
| **Listener pods** | **none — this is the blocker** |
| Runner images on GHCR | **not built yet** |
| `CI_RUNNER_LABEL` / `_KVM` repo variables | **not set** — nothing routes to self-hosted |
| Fork PR approval | `all_external_contributors` ✅ |

Deployed by Fleet from HomeLab commit `29d09f7`.

---

## 3. Uncommitted work, by repo

Working trees survive the reboot; none of this is committed.

**`Superhuman-Remote-Worker`** — the three deletions are of files committed in
`93a45b9d`, so they need a commit to take effect:

```
 D .github/workflows/ci-runner-image.yml     # image moved to Scripts-and-Notebooks
 D docker/Dockerfile.ci-runner
 D docker/ci-runner/job-started-guard.sh
 M scripts/check_ci_runners.py               # check 8 reworked; registry entry removed
 M tests/test_ci_runner_policy.py
 M docs/ci_self_hosted_runners.md
 M .github/workflows/{develop,main,stage1-rebuild}.yml   # step guards (see §5)
```

> The repo also has unrelated modifications from other work (`orchestrator/`,
> `src/`, several tests). Don't sweep those in — commit by path.

**`HomeLab`** — 2 modified, repointing at the new image + adding the allowlist:
```
 M deployments_managed/arc-runners-general/gha-runner-scale-set_helm.yaml
 M deployments_managed/arc-runners-kvm/gha-runner-scale-set_helm.yaml
```

**`Scripts-and-Notebooks`** — untracked, never committed:
```
?? .github/workflows/github-actions-runner.yml
?? devops/github-actions-runner/          # Dockerfile, job-started-guard.sh, README.md
```
> Four other untracked paths in that repo are unrelated (`devops/llm-loadtest/data/*`,
> `devops/university-server/`, `llm_containers/splade-pp-tei/`). Stage by path,
> never `git add -A`.

---

## 4. Next steps, in order

1. **Fix the PEM** (§1). Everything else is blocked on it.
2. **Land the runner image.** Commit the two `Scripts-and-Notebooks` paths. Open
   a PR first if you want the build validated before publishing — the workflow's
   `pull_request` leg builds both targets and runs the 13-case guard proof
   without pushing; merging to `main` then publishes
   `ghcr.io/knaeckebrothero/github-actions-runner{,-kvm}:latest`.
   *Undecided when we stopped: PR-first vs straight to main.*
3. **Commit the SRW + HomeLab changes.** HomeLab needs a push for Fleet to
   repoint the scale sets at the new image.
4. **Phase 2 — one job.** Set `CI_RUNNER_LABEL=srw-node4`, flip only
   `db-migrations.yml`'s `uniqueness` job (2 min, pure bash, no network, no
   docker, no credentials). Verify it lands on node4, that
   `runner.environment` prints `self-hosted` (**every step guard depends on
   this**), that the job-started hook logs `accepted`, and that the pod is gone
   afterwards. Then open a throwaway fork PR and confirm it runs on
   `ubuntu-latest`.
5. **Phases 3–8** per the original plan: rest of `db-migrations` (the dind
   networking proof), `develop.yml` cheap end, test jobs, docker builds, KVM, then
   `main.yml` last.

Rollback at any point is one command: `gh variable delete CI_RUNNER_LABEL`
(or `_KVM`), then `gh run cancel` anything in flight. No commit, no merge.

---

## 5. Already landed and verified

**Both guards, proven negatively.** The runtime hook refuses 16/16 cases locally
(including unset, empty, and glob `ARC_ALLOWED_REPOSITORY`, plus an event name
laundered to `push` while the ref still says `refs/pull/1/merge`).
`check_ci_runners.py` rejects 12/12 mutations — notably a *semantically
equivalent but reworded* `runs-on`, which it refuses on purpose, and a
duplicate-key parser differential.

**Workflow step guards** (all 45 `runs-on` lines still literal `ubuntu-latest`):
7 "Free disk space" steps and 5 "Enable KVM" udev steps now carry
`if: runner.environment == 'github-hosted'`; 5 new unconditional KVM ioctl probes
added; and `develop.yml`'s yq fail-open fixed so a missing tool no longer reads
as "nothing deployed yet, rebuild everything".

---

## 6. Findings worth not rediscovering

- **node4 has ~358 GiB free, not 1.8 TiB.** kubelet advertises 1.71 TiB because
  it knows nothing about Longhorn, which has 793 GiB scheduled on the same disk.
  Disk, not CPU, is the binding constraint, and `/var` is shared with Longhorn —
  CI filling it degrades replicas cluster-wide. Watch
  `kubectl get nodes.longhorn.io node4 -n longhorn-system`, not `describe node`.
- **NetworkPolicy IS enforced on the main cluster.** Probed from a pod on node4:
  reachable before a deny-all, rc=7/1/1/143 after. This contradicts the note in
  `infrastructure/coreos_ign_template_vms.yaml` that drove the VMs cluster to
  Calico — that note is about Flannel-the-plugin and misses K3s's separate
  kube-router controller.
- **ARC's dind sidecar renders with `resources` completely unset** and hard-coded
  `privileged: true`, and the chart silently drops any user-supplied container
  named `dind`. A namespace LimitRange is the only way to bound it — without it
  the container doing most of the work on node4 is unaccounted.
- **`/dev/kvm` on node4 is gid 36, mode 0666** — more permissive than assumed, so
  `supplementalGroups` is belt-and-braces. The `KVM_GET_API_VERSION` ioctl probe
  returns `12` on working hardware.
- **The `runs-on` expression is not a fork-PR defence.** GitHub runs the workflow
  file *from the PR*, so a stranger can delete it in their own PR. The real
  controls are the fork-approval setting and the image-side hook.
- **A `fleet.yaml` with `helm.chart` set silently ignores sibling raw YAML.**
  That's why ARC is four bundles. It's also a latent bug elsewhere in HomeLab —
  `traefik/01-eso.yaml` and `nats/nats_namespace.yaml` are dead files today.

---

## 7. Open decisions

- **PR-first vs straight-to-main** for the runner image (step 2 above).
- **Six credential-bearing jobs** — `lint` ×2, `license-inventory`,
  `resolve-version`, `deploy-experimental`, `release-chart` — are currently
  slated to stay hosted. Each holds `contents: write` and does `git push`/`git
  tag`, and `actions/checkout` persists that token into `.git/config`. You asked
  for "everything"; I flagged these as the one place worth an exception. One-line
  flip either way, and the routing guard accepts both.
- **Check 8 got weaker** when the image moved out. It now verifies the runner
  contract is *documented* here, not that it's *enforced* — enforcement moved to
  the image build, where 13 cases gate `docker push`. Net better, but it is a
  change from the approved plan.
