# Self-hosted CI runners

SRW's CI can run on self-hosted GitHub Actions runners in the homelab k3s
cluster, pinned to **node4** (16-core Zen 5, 128 GiB, real KVM). Routing is
controlled by two repository variables, so turning runners on or off never
requires a commit.

The cluster half lives in the HomeLab repo under
`deployments_managed/arc-{base,controller,runners-general,runners-kvm}/`; its
README carries the deploy runbook, the GitHub App bootstrap, and the
troubleshooting table.

## Why this needs more than a `runs-on` expression

**This repository is public.** For a `pull_request` event GitHub executes the
workflow file taken *from the pull request's merge ref* — the fork's copy. So
this line:

```yaml
runs-on: ${{ github.event_name == 'pull_request' && 'ubuntu-latest' || vars.CI_RUNNER_LABEL || 'ubuntu-latest' }}
```

does **not** stop a fork PR. A stranger edits it in their own PR and opens it.
What the expression actually protects against is *our* typo landing on
`develop`/`main`. That is worth having, but it is the weakest of four layers.

Mitigating the stakes: there are **zero repo/org secrets** in this repository —
every `secrets.` reference is `GITHUB_TOKEN`, which GitHub issues read-only for
fork PRs. The exposure is arbitrary code execution inside the cluster network,
not credential theft.

### The four layers

| # | Control | Where it lives | Can a fork PR disable it? |
|---|---|---|---|
| 0 | Fork-approval repo setting | Repo settings | No |
| 1 | `job-started-guard.sh` | Runner **image** | **No** — not in this repo |
| 2 | `runs-on` expression | Workflow files | Yes — irrelevant, layer 1 catches it |
| 3 | `check_ci_runners.py` + required check | `scripts/` + `ci-policy.yml` | Yes, but the merge is blocked |

Layers 0 and 1 make the failure mode structurally impossible. Layers 2 and 3
make it impossible to introduce *by accident*.

**Layer 0** — Settings → Actions → General → *Fork pull request workflows from
outside collaborators* → **Require approval for all external contributors**. The
public-repo default only requires approval for *first-time* contributors: one
merged trivial PR and a contributor is auto-approved forever.

**Layer 1** — `job-started-guard.sh`, wired up through
`ACTIONS_RUNNER_HOOK_JOB_STARTED`. The runner executes it before the job's first
step — before `actions/checkout`, so nothing from the triggering ref has been
fetched when it decides. Four independent checks: repository allowlist, an event
**allowlist** (`push`/`schedule`/`workflow_dispatch`), a ref cross-check so a
laundered event name cannot pass, and a best-effort payload check.

**It does not live in this repository, and that is the point.** It ships inside
the runner image, built from `devops/github-actions-runner/` in the
**Scripts-and-Notebooks** repo and published as
`ghcr.io/knaeckebrothero/github-actions-runner` (plus `-kvm`). A control that a
pull request could edit is not a control; keeping it outside every repo it
protects is what makes it one. That placement also avoids a bootstrap cycle — a
broken image push must not leave you without a working runner to build the fix.

It is tested, not reviewed: the image workflow runs **13 accept/refuse cases
against the built image and gates `docker push` on them**, so a regressed guard
cannot reach the registry. Pull requests there run the same proof without
pushing.

The repository allowlist is `ARC_ALLOWED_REPOSITORY`, supplied by the runner
**pod spec** (the ARC scale-set values in the HomeLab repo) rather than baked in
— equally out of a pull request's reach, but it keeps one image usable across
repos. Exact match, comma-separated, no globs; **unset or empty refuses
everything**, so a misconfigured scale set serves nothing rather than everything.

## The three legal `runs-on` lines

```yaml
runs-on: ubuntu-latest
runs-on: ${{ github.event_name == 'pull_request' && 'ubuntu-latest' || vars.CI_RUNNER_LABEL || 'ubuntu-latest' }}
runs-on: ${{ github.event_name == 'pull_request' && 'ubuntu-latest' || vars.CI_RUNNER_LABEL_KVM || 'ubuntu-latest' }}
```

`scripts/check_ci_runners.py` compares every `runs-on` line against these three
**byte for byte** (whitespace-normalised), from the raw source rather than parsed
YAML. A typo produces a mismatch — and so does any cleverly-equivalent rewrite.
That is deliberate, not a limitation: reviewing a routing change must never
require evaluating an expression.

Only `github`, `needs`, `strategy`, `matrix`, `vars` and `inputs` are legal
contexts in `runs-on`. Notably **not `env`**, which is why a repository variable
is the only variable-like option.

The trailing `|| 'ubuntu-latest'` is load-bearing. Without it an unset variable
evaluates to `''` and the job gets no runner at all.

## Kill-switch

```bash
gh variable set CI_RUNNER_LABEL     --body srw-node4        # enable general
gh variable set CI_RUNNER_LABEL_KVM --body srw-node4-kvm    # enable packer

gh variable delete CI_RUNNER_LABEL                          # fall back to hosted
```

Two variables so the KVM pool — the one most likely to break on its own — can be
failed back without moving the other ~40 jobs. Deleting a variable fails safe to
`ubuntu-latest`, with no commit and no merge.

Caveats: it applies to the **next** run, so cancel in-flight jobs with
`gh run cancel`. And it does not help during a GitHub *control-plane* outage — no
runner of any kind gets work then. It helps with hosted-capacity saturation (flip
*to* self-hosted) and with cluster outages (flip *away*).

## What `ubuntu-latest` was silently providing

These are baked into the runner image (Scripts-and-Notebooks,
`devops/github-actions-runner/Dockerfile`) because jobs assumed them:

| Tool | Used by | Failure mode without it |
|---|---|---|
| `yq` | `develop.yml` `changes` job | **Fails OPEN** — every VM job rebuilds every run, silently |
| `psql` | `db-migrations.yml` `dry-run` | `psql: command not found` |
| `gcc` | `build-sudo-gate` | build failure |
| Playwright system libs | `test-cockpit` | 2-minute apt transaction every run |
| `helm` | `test_canvas_slice3_infra.py` | tests silently **skip** via `which("helm")` |
| Docker CLI | `db-migrations.yml` `artifact`, VM base jobs | no daemon access |

The `yq` case is the instructive one: a missing tool and a genuinely-absent key
were indistinguishable, and the fail-open path is "rebuild everything". The
`changes` job now hard-fails on a missing `yq` while keeping the empty-key
fail-open, which really does mean "nothing deployed yet".

## Steps guarded by `runner.environment`

`runner.environment` is `github-hosted` or `self-hosted`, and is available in
step-level `if:` (not in `runs-on` — the runner isn't chosen yet).

- **7 "Free disk space" steps** are now `if: runner.environment ==
  'github-hosted'`. They exist for hosted runners' ~14 GiB; the heavy variants
  delete `/opt/hostedtoolcache`, which is where `setup-*` install. Guarded rather
  than deleted, so the hosted fallback still works.
- **5 "Enable KVM" udev steps** likewise — there is no udevd in a container, so
  the rule file goes nowhere and `udevadm control` fails. On self-hosted, access
  comes from the pod spec (privileged + a `/dev/kvm` `CharDevice` hostPath).
- A new **unconditional** "Verify KVM is usable" step does a real
  `KVM_GET_API_VERSION` ioctl. Packer's qemu builder does not fail without
  acceleration — it falls back to TCG and burns the whole 75–90 minute timeout
  first. The probe returns `12` on a working host.

## Local verification

```bash
python scripts/check_ci_runners.py          # the routing policy
pytest tests/test_ci_runner_policy.py
ruff check scripts/ tests/
```

`ci-policy.yml` is the hard gate and should be a **required status check** on
both `develop` and `main`. It has no `paths:` filter on purpose — GitHub treats a
required check that did not run as failed, see the "status-check footgun" section
of `knowledge-base/knowledge/branch_protection_setup.md`.

### Prove the guard fails

A guard nobody has watched fail is not a guard. Both halves have negative tests.

The runtime hook is exercised on **13 cases** by the image workflow in
Scripts-and-Notebooks — including an unset and an empty `ARC_ALLOWED_REPOSITORY`
(both must refuse, not wildcard), a glob allowlist (must not be honoured), a
foreign repository, and an event name laundered to `push` while the ref still
says `refs/pull/1/merge`.

The routing policy has been verified to reject a missing `|| 'ubuntu-latest'`
fallback, a semantically-equivalent rewording, a literal label, an unregistered
workflow, `pull_request_target`, a hosted-only workflow routed to self-hosted, a
label smuggled into a non-`runs-on` value, a duplicate-key parser differential,
and a vanished contract doc.

## What stays hosted permanently

- `ci-policy.yml` — it verifies nothing else is misrouted, so it can never run on
  the machines it protects.
- `codeql` ×2 — ~1 GB bundle per run, hosted-specific tool-cache behaviour,
  already `continue-on-error`, and the only job holding `security-events: write`.

(The runner image build used to be on this list. It now lives in
Scripts-and-Notebooks, so the bootstrap cycle it guarded against no longer
exists here.)

**Recommended to stay hosted**: `lint` ×2, `license-inventory`,
`resolve-version`, `deploy-experimental`, `release-chart`. Each holds
`contents: write` and does `git push`/`git tag`, and `actions/checkout` persists
that token into `.git/config` — so self-hosting them puts a repo-write credential
on homelab hardware, for jobs that already finish in 10–60 seconds. The routing
guard accepts either choice; this is a judgement call, not a rule.
