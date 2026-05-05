# CI Pipeline Optimization — design doc

**Status:** **shipped 2026-05-05** (Workstreams A + B + C + D2). Workstream D1 deferred — see §5.1.
**Author:** session continuation; precipitated by repeated `build-agent-vm-base` 75-min timeouts on `develop`
**Scope:** reduce wall-clock time for the GitHub Actions pipelines in `.github/workflows/develop.yml` and `main.yml`, without migrating off GitHub-hosted runners (self-hosted/Gitea is tracked separately and explicitly out of scope here).

---

## TL;DR

The pipeline has two distinct problems that have been conflated in conversation:

1. **Container builds (agent, orchestrator, cockpit, mcp, workspace, vm-controller) DO have layer caching configured**, but it's set up inconsistently — half the jobs use `mode=max`, half `mode=min`, all of them share the single 10 GB GHA cache pool with `ignore-error=true` masking failures. On warm cache they're fine; on a cold cache or after eviction they take 15–40 min.
2. **The `build-agent-vm-base` Packer job has zero caching**, and runs the full provisioning script inside QEMU's SLIRP user-mode network on every single CI run. That's the 35-min → 75-min job that just timed out. Mirror choice (Azure vs archive.ubuntu.com) is **not** the dominant factor — SLIRP single-threaded userspace TCP is.

This doc proposes five workstreams, three of which are surgical patches that ship in a few hours each, and two of which are larger restructurings (a Packer stage1/stage2 split, and a switch to GHCR registry caching). The combined effect on a cold-cache run should be roughly:

| Job | Today | After this design |
|---|---|---|
| `build-agent-vm-base` | 35–75+ min | ~10–15 min (warm), ~25 min (stage1 rebuild) |
| `build-workspace` | 20–40 min | ~6–10 min (warm), ~18 min (cold) |
| `build-agent` | ~25 min | ~8–12 min (warm) |
| `build-orchestrator/cockpit/mcp/vm-controller` | 5–20 min | 3–8 min (warm) |

Numbers are estimates from the research summarized in §6.

---

## 0. What shipped (2026-05-05)

In one sitting, in roughly this order:

| Workstream | Status | Notes |
|---|---|---|
| A1 — Cache Ubuntu cloud image | ✅ shipped | `actions/cache@v4` keyed on `ubuntu-noble-cloudimg-$(date -u +%Y-%m)`, applied in `develop.yml`, `main.yml`, and `stage1-rebuild.yml` |
| A2 — Standardize cache mode + scopes | ✅ shipped, **then superseded by C** | Per-image `scope=` keys went in first; the whole config was then rewritten to `type=registry` |
| A3 — `eatmydata` + global `APT::Install-Recommends "false"` | ✅ shipped | Applied in `provision-stage1.sh`, plus `Dockerfile.workspace` |
| A4 — Section profiling in `provision.sh` | ✅ shipped | `_section`/`_section_end` helpers emit `>>> [PROFILE] '<name>' took Ns` lines; live in both `provision-stage1.sh` and `provision-stage2.sh` |
| A5 — Bump `build-agent-vm-base` timeout 75→120 | ✅ shipped, **then revisited by B** | Stage2 now has a 30-min timeout; stage1 has 90 |
| B — Packer stage1/stage2 split | ✅ shipped | Six new files (see §2 below); old `agent-vm-base.pkr.hcl` and `provision.sh` deleted |
| C — Switch to GHCR registry cache | ✅ shipped | All 12 cache configs now `type=registry`; `REPO_LOWER` env hardcoded for case correctness |
| D1 — Drop `cypher-shell` + JRE | ❌ **deferred** | Still the agent's documented Neo4j interface (`src/agent.py:2152-2155`, `src/core/datasource_setup.py:478`, `orchestrator/main.py:14825-14829`). Needs separate redesign. |
| D2 — Trim global npm packages | ✅ shipped | Dropped `ts-node`, `@angular/cli`, `eslint`, `yarn`. Kept `typescript + prettier`. Added `corepack enable`. |
| D3 — Workspace Dockerfile parallel cleanup | ✅ shipped | BuildKit cache mounts (`/var/cache/apt`, `/var/lib/apt`, `/root/.npm`, `/root/.cache/pip`), `eatmydata`, three datasource RUNs collapsed into one, `--no-install-recommends` made global, `rm /etc/apt/apt.conf.d/docker-clean` to keep cache mount alive |
| Packer file provisioner consolidation | ✅ shipped | 8 of 9 single-file `provisioner "file"` blocks collapsed into one with `sources = [...]` |

### Pre-merge bootstrap (one-time, then never again)

Stage2 builds depend on a pre-existing `:latest` tag on `ghcr.io/.../-agent-vm-base-stage1`. Before this design merges, run **`stage1-rebuild.yml`** once via `workflow_dispatch` to seed the registry. Without that, the first stage2 build fails with `manifest unknown` from `docker pull`.

### Known gaps to validate on first real run

- `docker create + docker cp` against a `FROM scratch` image without a CMD. Should work (containers don't have to start to be `cp`-able), but the workflow has a defensive `|| docker create $IMAGE` fallback.
- ~~`replace(github.ref_name, '/', '-')` for OCI tag sanitization~~ — turned out GitHub Actions has no `replace()` expression function (the workflow validator rejected it on first commit). Replaced with static per-workflow cache keys: `:buildcache-develop` in `develop.yml`, `:buildcache-main` in `main.yml`. Per-branch isolation lost, but PRs can't write anyway (no `docker/login-action` on PRs, `ignore-error: true` swallows the silent failure), so all builds just read/write the workflow's shared cache. `concurrency: cancel-in-progress: false` already serializes pushes; with `mode=max,ignore-error=true` a race in concurrent writes lets the last writer win — no correctness issue.
- PR builds keep the existing conditional `docker/login-action` (no GHCR auth on PRs), so PR cache reads/writes fail silently via `ignore-error: true`. PR validation runs are slightly slower as a result; push builds are fully cached.

---

## 1. Current state (pre-implementation, kept for reference)

### 1.1 Workflows

Both `develop.yml` and `main.yml` build the same set of images. `develop.yml` is change-based (build only triggers when relevant paths changed, gated by `changes` job); `main.yml` is full-matrix on every push to main and PR. Both share the same Dockerfiles and the same Packer template.

| Job | Timeout | Cache config | Where |
|---|---|---|---|
| `build-agent` | 30 min | `type=gha,mode=max,ignore-error=true` | `develop.yml:412–463`, `main.yml:250–303` |
| `build-orchestrator` | 20 min | `type=gha,mode=min,ignore-error=true` | `develop.yml:465–509`, `main.yml:305–351` |
| `build-cockpit` | 20 min | `type=gha,mode=min,ignore-error=true` | `develop.yml:511–555`, `main.yml:353–399` |
| `build-mcp` | 15 min | `type=gha,mode=min,ignore-error=true` | `develop.yml:557–601`, `main.yml:401–447` |
| `build-workspace` | 40 min | `type=gha,mode=max,ignore-error=true` | `develop.yml:603–652`, `main.yml:449–500` |
| `build-vm-controller` | 15 min | `type=gha,mode=min,ignore-error=true` | `develop.yml:808–852`, `main.yml:659–705` |
| `build-agent-vm-base` | **75 min** | **none** (Packer + plain `docker build`) | `develop.yml:694–806`, `main.yml:543–657` |
| `build-sudo-gate` | 15 min | none (Go + C compile, ~30 s) | `develop.yml:654–692`, `main.yml:502–541` |

### 1.2 The Packer job, in detail

`docker/agent-vm-base/agent-vm-base.pkr.hcl:80–110` boots a QEMU VM with `accelerator = "kvm"` (works on GHA — KVM device is present, see workflow's `Enable KVM` step at `develop.yml:747–752`) and `net_device = "virtio-net"` over SLIRP user-mode networking. It then runs `scripts/provision.sh` as a single shell provisioner.

Inside the VM, `provision.sh` does:

| Lines | Step | Approx. time |
|---|---|---|
| `provision.sh:25–87` | `apt-get update` + 18 base packages (build-essential, vim, python3-dev, etc.) | 4–6 min |
| `provision.sh:89–113` | Datasource clients: `postgresql-client`, `mongodb-mongosh` (custom APT repo), `openjdk-17-jre-headless cypher-shell` (Neo4j APT repo) | 5–8 min |
| `provision.sh:119–121` | Tailscale install via curl pipe-to-shell | 30 s |
| `provision.sh:127–133` | `sudo python3 -m pip install nats-py psutil` | 30 s |
| `provision.sh:143–157` | **Playwright Chromium install with `--with-deps`** | 8–12 min |
| `provision.sh:163–174` | Node.js 22 from NodeSource + 6 global npm packages (typescript, ts-node, @angular/cli, eslint, prettier, yarn) | 4–6 min |
| `provision.sh:178–344` | User setup, SSH/tmux/git config, daemon install, sudo-gate wiring | 1–2 min |
| Workflow steps after Packer | `wget` Ubuntu cloud image (~600 MB), `docker build` containerDisk wrapper, push | 3–5 min |

Total realistic floor on a cold run: ~30–40 min when network behaves; the failed run hit 75 min because SLIRP throughput collapsed mid-build (the user observed the apt download log streaming slowly past the 60-min mark).

### 1.3 Why caching has felt unreliable

Three confounders explain the "I thought we had caching" feeling:

- **GHA cache 10 GB-per-repo limit** (raised to optional pay-as-you-go in Nov 2025, but not enabled here). Six images at `mode=max` plus the uv cache plus the npm cache easily exceeds 10 GB. LRU eviction kicks in; older caches drop; next build is a cold rebuild.
- **`ignore-error=true`** on every cache-from/cache-to (e.g. `develop.yml:460–461`). Cache export failures (rate-limit, OOM during commit, eviction races) appear as warnings, not job failures. So a build that's silently rebuilding from scratch looks identical in the UI to a healthy cache hit.
- **Inconsistent `mode`** — `mode=min` only exports final-stage layers, so multi-stage Dockerfiles (orchestrator, cockpit) lose builder-stage caches between runs.

### 1.4 Doc convention reference

This file follows the structure of `docs/design/guardrails_matrix.md`: title with `— design doc` suffix, Status/Author/Scope frontmatter prose, TL;DR, numbered sections, prose-with-tables-and-fenced-blocks tone.

---

## 2. Workstream A — Surgical patches (✅ shipped)

Cheap, high-confidence fixes that don't change the architecture.

### A1. Cache the Ubuntu cloud image download

The workflow `wget`s `noble-server-cloudimg-amd64.img` (~600 MB) on every `build-agent-vm-base` run (`develop.yml:756–762`, `main.yml:607–613`). Trivially cacheable.

```yaml
- name: Cache Ubuntu cloud image
  id: cloud-image
  uses: actions/cache@v4
  with:
    path: docker/agent-vm-base/input/noble-server-cloudimg-amd64.img
    key: ubuntu-noble-cloudimg-${{ env.NOBLE_DATE_KEY }}
- name: Download Ubuntu cloud image
  if: steps.cloud-image.outputs.cache-hit != 'true'
  working-directory: docker/agent-vm-base
  run: |
    mkdir -p input
    wget -q https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img \
      -O input/noble-server-cloudimg-amd64.img
```

`NOBLE_DATE_KEY` should be set at workflow level to e.g. `2026-05` (rotate monthly so we don't drift more than ~30 days behind upstream patches, which our `apt-get upgrade` inside the VM would pull in anyway).

**Effort:** 30 min. **Saved per run:** ~30–60 s on cold cache (the wget itself), but more importantly saves bandwidth and removes a transient failure mode.

### A2. Standardize cache `mode` and add explicit scopes

For every container build, change `cache-to: type=gha,mode=min` → `mode=max` and add `scope=<image>-${{ github.ref_name }}`. Per the Docker cache backend docs (cited §6), `mode=max` is essential for multi-stage builders, and per-image scopes prevent the six images from overwriting each other's cache state.

```yaml
cache-from: |
  type=gha,scope=orchestrator-${{ github.ref_name }}
  type=gha,scope=orchestrator-main
cache-to: type=gha,scope=orchestrator-${{ github.ref_name }},mode=max
```

Two scopes on `cache-from` so PR builds reuse `main`'s cache. Keep `ignore-error=true` for now (pulling a cache miss should not fail a build).

**Effort:** 1 h (six jobs × two workflows + matching `db-migrations.yml` if it builds). **Saved per run:** uneven — biggest impact on multi-stage Dockerfiles (orchestrator, cockpit, mcp, vm-controller currently in `mode=min`).

### A3. Wrap apt operations with `eatmydata` in `provision.sh`

`eatmydata` library-preloads a no-op `fsync()`. Reported gains: ~3× on dpkg-heavy installs on standard disks; ~33% on Azure-class runners. Ubuntu's official Docker images already do this via `force-unsafe-io` in `/etc/dpkg/dpkg.cfg.d/docker-apt-speedup`, but the cloud image we boot does not.

```bash
# scripts/provision.sh, after line 44 apt config block:
sudo apt-get update -y
sudo apt-get install -y eatmydata

# Replace every "sudo apt-get install -y ..." with:
sudo eatmydata apt-get install -y --no-install-recommends ...
```

Also add `--no-install-recommends` globally (currently only used at `provision.sh:148`) and set it as default in `99-build-tuning`:

```
APT::Install-Recommends "false";
```

**Effort:** 1 h. **Saved per run:** ~3–5 min on the `provision.sh` apt sections.

### A4. Add `time` instrumentation to `provision.sh`

Wrap each labelled section in `time bash -c '...'` or surround with `SECONDS=0 ; ... ; echo "Section X: $SECONDS s"`. This isn't an optimization — it's data collection so future tuning is grounded in numbers.

**Effort:** 30 min. **Saved per run:** zero. Future-saving: high.

### A5. Bump `build-agent-vm-base` timeout from 75 → 120 min

While we ship A1–A4, future cold-cache runs may still occasionally exceed 75 min. Bumping prevents the timeout-cancel-burns-an-hour failure mode the user just hit. This is a Band-Aid; **remove it after Workstream B lands** since the stage1/stage2 split should keep us comfortably under 30 min.

**Effort:** 1 line. **Saved per run:** none directly; prevents wasted-run failures.

---

## 3. Workstream B — Packer stage1/stage2 split (✅ shipped)

**The single biggest structural fix.** Pre-implementation: the VM image was rebuilt from the Ubuntu cloud image upward on every change to anything that triggered `build-agent-vm-base`. Wasteful: ~95% of `provision.sh` (the apt installs, JRE, Playwright, Node) was identical across hundreds of commits. Now split into two layers.

### 3.1 Approach

Split `provision.sh` into two phases:

**Stage1 — heavy, stable, published:**
- All apt packages (`provision.sh:46–86, 97, 105, 113`)
- Tailscale (`:120`)
- Python pip system deps (`:133`)
- Playwright Chromium with deps (`:146–149`) **— this alone is 8–12 min**
- Node.js 22 + global npm packages (`:163–174`) **— 4–6 min**

Inputs: a checksum of `provision.sh` lines 25–174, the `.playwright-version` file, and a manual rebuild trigger. Output: a `qcow2` wrapped as a containerDisk image, pushed to `ghcr.io/.../agent-vm-base-stage1:<hash>` and `:latest`.

**Stage2 — light, volatile, runs every CI build:**
- User setup, SSH/tmux/git config (`provision.sh:178–214`)
- Management daemon install + config (`:233–249`)
- Sudo gate binary install + config (`:266–318`)

Inputs: `vm/sudo-daemon/` and `vm/sudo-plugin/` (these change), `provision.sh:178–344` itself (rare), `cloud-init/*` (occasional), the daemon files. Stage2 runs on top of the stage1 qcow2 (`source.qemu.agent-vm-base-stage2.iso_url = "input/agent-vm-base-stage1.qcow2"`).

### 3.2 Implementation (as shipped)

```
docker/agent-vm-base/
├── stage1.pkr.hcl                  # boots Ubuntu cloud → stage1 qcow2
├── stage2.pkr.hcl                  # boots stage1 qcow2 → final qcow2
├── Dockerfile.containerDisk        # unchanged, wraps stage2's output/agent-vm-base.qcow2
├── Dockerfile.containerDisk-stage1 # FROM scratch wrapper for stage1's output-stage1/agent-vm-base-stage1.qcow2
├── scripts/
│   ├── provision-stage1.sh         # heavy: apt + datasource clients + Tailscale + Playwright + Node
│   ├── provision-stage2.sh         # light: user setup + SSH + management daemon + sudo gate + tmux/git
│   ├── cleanup-stage1.sh           # trims caches, preserves packer user + cloud-init state
│   └── cleanup.sh                  # full cleanup at end of stage2 (unchanged from before)
```

The old `agent-vm-base.pkr.hcl` and `scripts/provision.sh` were deleted. Stage2 boots the stage1 qcow2, so cloud-init's `instance-id: packer-build` matches between stages — cloud-init detects "already ran" and skips, packer user persists for SSH.

Workflow changes (`develop.yml`, `main.yml`):

```yaml
# Two new jobs replace today's build-agent-vm-base
build-agent-vm-base-stage1:
  if: needs.changes.outputs.vm-base-stage1 == 'true' || workflow_dispatch
  # ... runs only when stage1 inputs change
  steps:
    - run: packer build stage1.pkr.hcl
    - run: |
        IMAGE=ghcr.io/.../agent-vm-base-stage1
        docker build -t $IMAGE:$STAGE1_HASH -t $IMAGE:latest -f Dockerfile.containerDisk .
        docker push $IMAGE:$STAGE1_HASH && docker push $IMAGE:latest

build-agent-vm-base:
  needs: [build-agent-vm-base-stage1, build-sudo-gate, ...]
  steps:
    - run: |
        # Pull stage1 image, extract qcow2, feed to stage2
        docker pull ghcr.io/.../agent-vm-base-stage1:latest
        docker create --name s1 ghcr.io/.../agent-vm-base-stage1:latest
        docker cp s1:/disk/disk.img input/stage1.qcow2
    - run: packer build stage2.pkr.hcl
    - # ... existing containerDisk wrap and push
```

The `changes` job in `develop.yml` gets a new output `vm-base-stage1` that fires on changes to `provision-stage1.sh`, `.playwright-version`, or stage1's Packer config — keeping stage1 rebuilds rare (estimated: weekly to monthly).

### 3.3 Versioning and freshness (as shipped)

Stage1 is tagged with `:sha-<short>` and `:latest` on every push that triggers a rebuild. The change-detection in `develop.yml`'s `changes` job watches `provision-stage1.sh`, `cleanup-stage1.sh`, `stage1.pkr.hcl`, `Dockerfile.containerDisk-stage1`, `cloud-init/`, and `.playwright-version` — only when those change does stage1 run on a develop push.

`.github/workflows/stage1-rebuild.yml` runs `workflow_dispatch` + weekly cron (`0 6 * * 1` — Mondays 06:00 UTC) and rebuilds stage1 unconditionally so the baseline doesn't drift more than ~7 days behind upstream apt updates. Adds a `:cron-<YYYYMMDD>` tag for traceability.

In `main.yml`, stage1 only runs on push events to main (not on PRs to main). PRs to main reuse the existing `:latest` and only rebuild stage2 — keeping PR validation fast.

### 3.4 Risks (current state)

- **Bootstrap dependency:** stage2 needs stage1 `:latest` to exist. Run `stage1-rebuild.yml` once via `workflow_dispatch` before merging this design.
- **stage1 corruption blocks all builds.** Mitigation: rollback by re-tagging an older `:sha-<short>` as `:latest`. The rebuild cron will overwrite within a week regardless.
- **`docker create + docker cp` against `FROM scratch`:** workflow uses `docker create $IMAGE /noop 2>/dev/null || docker create $IMAGE` to handle the missing-CMD edge case across Docker versions.

**Saved per run on the common case (stage1 cached):** 25–35 min. **Cost on the rare stage1-rebuild path:** same ~30–40 min as today, but only when stage1 inputs change or the weekly cron fires.

---

## 4. Workstream C — Switch container caches to GHCR registry (✅ shipped)

After A2 made `mode=max` consistent, GHA-cache pressure went up — every build exporting a max-mode cache stresses the 10 GB limit. We migrated all 12 cache configs to **registry cache on GHCR**, scoped per image.

```yaml
cache-from: |
  type=registry,ref=ghcr.io/${{ github.repository }}/agent:buildcache-${{ github.ref_name }}
  type=registry,ref=ghcr.io/${{ github.repository }}/agent:buildcache-main
cache-to: type=registry,ref=ghcr.io/${{ github.repository }}/agent:buildcache-${{ github.ref_name }},mode=max,image-manifest=true,oci-mediatypes=true,compression=zstd
```

Benefits per `docs.docker.com/build/cache/backends/registry/`:
- No 10 GB shared limit (GHCR free for public, per-org quota for private)
- Cache survives across PR branches, forks, and longer than GHA's 7-day LRU
- `compression=zstd` reduces bandwidth on cold pulls
- Inspectable as regular OCI artifacts — `docker buildx imagetools inspect` to debug

Auth is already there — `docker/login-action@v3` runs on every build job. Just need to add `permissions: packages: write` (already present on push builds; may need adding to PR builds depending on whether we cache there — see §4.1).

### 4.1 PR builds and registry cache (as shipped)

PR builds keep the existing `if: github.event_name != 'pull_request'` on `docker/login-action` for the container build jobs — no GHCR auth on PRs. Both `cache-from` (read) and `cache-to` (write) fail silently due to `ignore-error: true`, so PR validation runs cold-cache. Push builds (with login) get fully cached.

This is option (a) from the original design — "cache-from only on PRs, cache-to disabled" — implemented via the `ignore-error: true` flag rather than a conditional cache-to expression. Simpler YAML, same outcome.

The stage2 build job (`build-agent-vm-base`) is the **exception**: it has unconditional `docker/login-action` because PR validation needs to *pull* the stage1 image from GHCR. Login happens on PRs; the container build's `push: ${{ github.event_name != 'pull_request' }}` still gates the final image push.

### 4.2 `ignore-error: true` left in place

The original plan was to drop `ignore-error: true` on one canary build to surface silent cache failures. We kept it on for the initial migration so a flaky cache push doesn't fail CI during the transition. Revisit once we have a few weeks of stable registry-cache builds — pick `build-orchestrator` (medium-sized, fast feedback) as the canary then.

---

## 5. Workstream D — Trim baked dependencies

### 5.1 Drop `openjdk-17-jre-headless` + `cypher-shell` from VM base — ❌ deferred

`provision-stage1.sh:121` installs ~280 MB of JRE + cypher-shell. The original plan was to drop both in favor of the Neo4j Python driver. **Audit found this is non-trivial:** cypher-shell is the agent's documented primary interface for Neo4j datasource access:

- `src/agent.py:2152-2155` — agent prompt instructs the LLM to "Use `run_command` with `cypher-shell`" with literal example syntax
- `src/core/datasource_setup.py:478` — datasource help text builds `cypher-shell --address ...` command strings
- `orchestrator/main.py:14825-14829` — the orchestrator's datasource help reference includes cypher-shell examples

Refactoring would mean: replace prompt examples with Python driver code, add a `neo4j_query` agent tool through the existing `src/tools/registry.py` pattern, update the orchestrator's datasource help, and audit any tests that exercise the path. Scope is bigger than the original 2–4 h estimate — a separate workstream.

Until then: stage1 still installs JRE + cypher-shell. ~280 MB image + ~60 s install time accepted as a known cost.

### 5.2 Trim global npm packages — ✅ shipped

Pre-implementation `provision.sh:168–174` installed `typescript ts-node @angular/cli eslint prettier yarn` globally — combined ~140 MB and ~45 s of npm install in the VM.

Audit found zero usage of `ts-node`, `@angular/cli`, `yarn` in `src/`, `orchestrator/`, or `config/`. `eslint` only mentioned in developer-expert prompts as a possible linter to detect, never invoked. `typescript` and `prettier` are commonly used, kept baked.

`provision-stage1.sh:174-178` and `Dockerfile.workspace:122-129` now install:
```
typescript
prettier
```
plus `corepack enable` so `yarn`/`pnpm` are still available on demand via shims (Node 22 ships corepack — zero install cost).

**Saved per run:** ~110 MB image, ~30–45 s in `provision-stage1.sh`.

### 5.3 Workspace container Dockerfile cleanup — ✅ shipped

`Dockerfile.workspace` got the same treatment as `provision-stage1.sh`, plus extra container-specific wins via BuildKit cache mounts:

- Added `APT::Install-Recommends "false"` to apt-tuning RUN; added `rm /etc/apt/apt.conf.d/docker-clean` and a `Binary::apt::APT::Keep-Downloaded-Packages "true"` config so `--mount=type=cache,target=/var/cache/apt` actually persists the downloaded `.debs`
- All apt-related RUNs use `--mount=type=cache,target=/var/cache/apt,sharing=locked` plus `/var/lib/apt`. eatmydata wraps every install
- Three datasource RUNs (postgresql, mongodb, neo4j) consolidated into one — single `apt-get update`, one install for all four packages
- Node.js RUN gets npm cache mount at `/root/.npm`. Playwright RUN gets pip cache mount at `/root/.cache/pip`
- npm globals trimmed identically to §5.2

The `cypher-shell + JRE` install in the workspace container also remains, for the same reasons as §5.1.

---

## 6. Out of scope (tracked separately)

- **Self-hosted/Gitea Actions runner.** Per user decision, deferred. The fundamental KVM-on-GHA limitation (per `docs.github.com/en/actions/reference/runners/larger-runners` — KVM/nested-virt not supported on any GitHub-hosted SKU as of this writing, even on 96-vCPU larger runners; KVM works on GHA only because GHA exposes `/dev/kvm` to the runner, which is a special-case nested setup that doesn't extend to most workloads) means VM image builds will always be ~3× slower on GitHub-hosted than on owned bare metal. When we move to a Gitea runner with KVM passthrough, expect `build-agent-vm-base` to drop into the 8–12 min range with no further code changes.
- **Third-party drop-in runners** (Depot.dev, Namespace.so, Ubicloud). All viable; all cost money. Out of scope for this design but worth re-evaluating if Workstream B doesn't deliver expected wins.
- **`actuated.com`** is the named provider for KVM-in-CI. Worth a spike if self-hosted maintenance becomes a burden.
- **BuildJet — shut down January 2026.** Do not adopt.

---

## 7. What's next

Workstreams A, B, C, D2, D3 all shipped in one session. The original Phase 1 → 4 sequencing was collapsed since the user opted to land everything together. Remaining work:

1. **Bootstrap stage1.** Run `stage1-rebuild.yml` via `workflow_dispatch` once before merging, so `:latest` exists on `ghcr.io/.../-agent-vm-base-stage1`. Without that the first stage2 build fails on `docker pull`.
2. **First develop run after merge.** Watch for: stage2 successfully pulling stage1, registry cache writes succeeding (no silent `manifest unknown` errors), and the GHCR registry growing the right `:buildcache-develop` tags.
3. **Drop `ignore-error: true` on a canary.** After a few weeks of stable registry-cache builds, drop it on `build-orchestrator` to surface silent failures. Roll out to others if green for a week.
4. **Workstream D1 (cypher-shell removal)** — separate redesign needed. Sketch:
   - Add a `neo4j_query` tool in `src/tools/registry.py` that uses the existing Neo4j Python driver (`src/database/neo4j_db.py`)
   - Update `src/agent.py:2152-2155` prompt to reference the new tool instead of `cypher-shell`
   - Update `src/core/datasource_setup.py:478` and `orchestrator/main.py:14825-14829` help strings
   - Drop `openjdk-17-jre-headless cypher-shell` from `provision-stage1.sh:121` and `Dockerfile.workspace`
   - Estimated 1–2 days; saves ~280 MB on both stage1 image and workspace container
5. **Self-hosted runner option** (Gitea / actuated.com) — still deferred per user. With B in place, stage2 builds should run in ~5–10 min on GHA, which makes the move less urgent. Re-evaluate if stage1 cron rebuilds become annoying or if the budget for GHA-minutes climbs.

---

## 8. References

External:
- [Docker — GitHub Actions cache backend (`type=gha`)](https://docs.docker.com/build/cache/backends/gha/)
- [Docker — Registry cache backend (`type=registry`)](https://docs.docker.com/build/cache/backends/registry/)
- [Docker — Optimize cache usage in builds](https://docs.docker.com/build/cache/optimize/)
- [GitHub blog — GHA cache size can now exceed 10 GB (Nov 2025)](https://github.blog/changelog/2025-11-20-github-actions-cache-size-can-now-exceed-10-gb-per-repository/)
- [QEMU networking docs — SLIRP/user-mode performance](https://www.qemu.org/docs/master/system/devices/net.html)
- [Speeding up Docker builds with eatmydata](https://wildwolf.name/speeding-up-docker-builds-with-eatmydata/)
- [BuildKit cache mounts in Dockerfile (vsupalov.com)](https://vsupalov.com/buildkit-cache-mount-dockerfile/)
- [Playwright CI / Docker docs](https://playwright.dev/docs/ci)
- [Packer manifest post-processor](https://developer.hashicorp.com/packer/docs/post-processors/manifest)
- [Two-stage QEMU builds with Packer (puppeteers.net)](https://www.puppeteers.net/blog/two-stage-qemu-builds-with-packer/)
- [GitHub Actions runner pricing (incl. larger runners, KVM availability)](https://docs.github.com/en/billing/reference/actions-runner-pricing)

Internal (post-implementation):
- `.github/workflows/develop.yml` — pipeline for develop (change-based; stage1 + stage2 jobs)
- `.github/workflows/main.yml` — pipeline for main (full-matrix; stage1 only on push)
- `.github/workflows/stage1-rebuild.yml` — weekly cron + manual `workflow_dispatch` to refresh stage1 against upstream apt updates
- `docker/agent-vm-base/stage1.pkr.hcl` — Packer template for the heavy base image (apt + datasource clients + Playwright + Node)
- `docker/agent-vm-base/stage2.pkr.hcl` — Packer template for per-commit overlay (boots stage1 qcow2, layers user/daemon/sudo-gate)
- `docker/agent-vm-base/scripts/provision-stage1.sh` — heavy provisioning (with `_section`/`_section_end` profiler)
- `docker/agent-vm-base/scripts/provision-stage2.sh` — light provisioning (same profiler)
- `docker/agent-vm-base/scripts/cleanup-stage1.sh` — light cleanup (preserves packer user + cloud-init state)
- `docker/agent-vm-base/scripts/cleanup.sh` — full cleanup at end of stage2 (unchanged)
- `docker/agent-vm-base/Dockerfile.containerDisk-stage1` — `FROM scratch` wrapper for stage1 qcow2
- `docker/agent-vm-base/Dockerfile.containerDisk` — `FROM scratch` wrapper for final qcow2
- `docker/Dockerfile.workspace` — workspace container with BuildKit cache mounts + eatmydata + consolidated datasource RUN
- `docs/design/guardrails_matrix.md` — formatting precedent for this doc
