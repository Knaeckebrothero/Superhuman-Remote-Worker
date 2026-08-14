# OSS Release — Transition Checklist

Working scaffolding for moving this repo into the `superhuman-remote-worker` GitHub org,
splitting out the non-product pieces, and cleaning the history before the public release.

**This file is transition scaffolding, not product documentation.** It gets deleted (or moved
into `srw-cloud`) in Phase 6 before the announcement — see the last item.

Status: drafted 2026-08-13. Nothing executed yet.

---

## Decisions already made

Recorded so they don't get relitigated mid-transition.

**Repo landscape — three repos, only one of which is new.**

| Repo | Visibility | Holds |
|---|---|---|
| `superhuman-remote-worker/<renamed>` | public, FSL-1.1-ALv2 | the product: `src/`, `orchestrator/`, `cockpit/`, `helm/`, `config/`, `docker/`, `tests/`, the installer (`configure.html` + `generator.mjs`), `values-local.yaml.example`, `values-tilt.yaml` |
| `HomeLab` (exists, private, self-hosted Gitea) | private | GitOps/deploy: `values-experimental.yaml`, `fleet.yaml`, alongside the existing `deployments_managed/srw-sales-page/` |
| `srw-cloud` | private | SaaS layer: payments, multi-tenancy, tenant provisioning, entitlements — plus the sales page under `www/` |

**Why not one monorepo.** A GitOps repo is read continuously by a cluster controller and
reconciled on every commit; that is a different security and cadence domain from commercial
source, and folding them together makes `ignorePaths` in `fleet.yaml` load-bearing for
correctness. `HomeLab` also already exists and already deploys the sales page.

**Why not a separate repo per concern.** The sales page is two files. Each repo carries a fixed
cost (CI, secrets, dependabot, branch protection) plus the daily "which repo does this change go
in" tax. The sales page will acquire real coupling to the SaaS layer as soon as signup and
pricing tiers exist — pricing table and plan definitions want one source of truth — so it starts
where it is heading.

**The load-bearing rule: dependency direction.** `srw-cloud` depends on the public repo. The
public repo never imports from `srw-cloud`. This is what keeps self-hosted installs working and
stops the product drifting into something a customer cannot deploy.

Mechanically: the private image builds `FROM ghcr.io/<org>/superhuman-remote-worker-orchestrator:<tag>`
and layers on top, with a chart values switch for the image repo. This is the Sentry shape
(`getsentry` imports `sentry` and re-exports it) expressed in containers rather than Python
packaging — correct here because `orchestrator/` is an 11.5k-line application, not a library.

Where public code must call private code, that is a **hook point** declared in the public repo
with a no-op default. Embryos already present: infrastructure-metering collectors,
`helm-vm-cluster/templates/license-gate.yaml`, DB-backed model registry. Expect three to cover
the first year — a metering sink, an entitlement/quota resolver, a tenant-provisioning callback.
Define each when needed, not up front.

**FSL wording.** FSL-1.1-ALv2 is *source-available*, not open source (not OSI-approved). Scrub
"open source" from `README.md`, the sales page, and the announcement copy — otherwise that is
what the launch-day thread argues about instead of the product.

**Copyright.** `LICENSE` Notice stays `Copyright 2026 Niklas Hall`. A GitHub org is not a legal
entity; this changes only when the UG exists and IP is formally assigned.

---

## Phase 0 — Rotate credentials (do first, independent of everything else)

The repo has been public since 2025-11-03. Fork count is 0, but that is not the exposure
measure: **2,128 clones from 457 unique cloners in the last 14 days**, against 1 star, 0 watchers
and 3 unique page-viewers. That ratio is automated traffic — secret scanners, mirrors, crawlers.
Anything credential-shaped that was ever committed must be treated as harvested. History rewriting
does not undo this; it only stops the material being re-served going forward.

Full scan of all 20,267 objects in history. Two real findings, both already out of `HEAD`:

- [ ] **Rotate the Tavily API keys.** Two live-shaped keys were committed in
      `deployment/values-local.yaml`:
      - `tvly-U2Q3vC1s4SP…` — public roughly 2026-06-07 → 2026-07-03
      - `tvly-dev-3pR9Qg…` — in the final tracked revision, 2026-07-05
      Tavily bills per search, which is exactly what harvesters monetise. Rotate at tavily.com.
- [ ] **Treat the leaked SSH key as burned.** `.local-ssh/id_ed25519`, comment
      `srw-local-workspace`, fingerprint `SHA256:fMulhc4ZAkzvV30t17/RpS6sZwpbl3yeyWg4L86UGG0`,
      unencrypted (cipher `none`). Added `b50943c4` (2026-04-03), removed `70232734`
      (2026-04-05). `.local-ssh/` is gitignored now. Its public half appears in no current
      `authorized_keys` in the chart, `HomeLab`, or anywhere else — nothing trusts it today, so
      this is disclosure without an access path. Confirm once more before closing.

Not findings, recorded so they don't get re-flagged:

- `APP_ENCRYPTION_KEY: B9THPyad3JG…` is the documented fixed dev key, still deliberately present
  in the tracked `values-local.yaml.example`. Local DB only. Not a leak.
- Everything else in the committed `values-local.yaml` was placeholders (`dev_pg_password`,
  `minioadmin`, empty strings for the LLM provider keys).
- The 389 `BEGIN OPENSSH PRIVATE KEY` hits across history are overwhelmingly test fixtures in
  `tests/test_ssh_key_utils.py`, `tests/test_kb_git_source.py`, and docs examples.
- `.env` was **never** committed (0 commits touching it). `values-local.yaml` was tracked across
  36 commits until `0a29ec7b` (2026-07-05) removed and gitignored it.

---

## Phase 1 — Transfer the public repo to the org

Reversible-ish and instant. Do it before the history rewrite so there is a known-good green
pipeline in the final location before the one irreversible step.

- [ ] Decide the repo name. Transferring as-is yields
      `superhuman-remote-worker/superhuman-remote-worker`. Rename now while URLs are breaking
      anyway — `core`, `platform`, or `srw`. Reads better in the org listing and in image names.
- [ ] Transfer the repo into the `superhuman-remote-worker` org.
- [ ] Re-add Actions secrets — **these do not transfer.** Set them at org level if `srw-cloud`
      will need the same ones.
- [ ] Re-apply branch protection / rulesets (also not carried over).
- [ ] Verify the old-URL redirect works for clone and push. Note it breaks permanently if
      anything is ever created at the old path — do not recreate a repo there.
- [ ] Confirm `develop.yml` and `main.yml` still run green in the new location before proceeding.

---

## Phase 2 — Migrate GHCR packages

The largest single chunk of transition work, and the one most likely to break things quietly.

GHCR packages are owned by the **user** `knaeckebrothero`. Transferring the repo does **not**
move them. Once the repo lives in the org, CI's `GITHUB_TOKEN` is scoped to an org repo and will
`403` pushing to packages in the personal namespace.

- [ ] Republish images and charts under `ghcr.io/superhuman-remote-worker/*`.
- [ ] Update every reference in one commit. ~20 distinct `ghcr.io/knaeckebrothero/*` paths,
      concentrated in:
      - `helm/values.yaml` (8 refs) and `helm-vm-cluster/values.yaml` (3)
      - `docker-compose.yaml` (10)
      - `.github/workflows/develop.yml` (3)
      - `orchestrator/services/agent_provisioner.py` (2)
      - `website/generator.mjs` — the `CHART` constant (customer-facing install command)
      - `deployment-vms/srw-vm-controller/fleet.yaml`, `helm-vm-cluster/templates/_helpers.tpl`,
        `docker/agent-vm-base/Dockerfile.containerDisk`, `vm/controller/controller.py`
      - tests asserting image refs: `test_vm_provisioner.py`, `test_vm_controller.py`,
        `test_persistent_provisioner.py`, `test_infrastructure_metering_vm_cluster_helm.py`,
        `test_vm_template_description_escaping.py`
- [ ] Leave the old packages published (do not delete) until Fleet is confirmed on the new
      namespace — rollback path.
- [ ] Check package visibility is public on the new namespace, or anonymous `helm pull` of the
      install command fails for the first OSS user who tries it.

---

## Phase 3 — Move deploy config into HomeLab

Sequencing matters: `deployment/fleet.yaml` pins `oci://ghcr.io/knaeckebrothero/charts/srw-dev`
at `0.0.0-dev.sha-83da298`. Chart namespace and Fleet pin must move together or the dev cluster
stops reconciling. Do not combine this with the Phase 2 namespace flip.

- [ ] Move `deployment/values-experimental.yaml` and `deployment/fleet.yaml` into `HomeLab`,
      next to the existing `deployments_managed/srw-sales-page/`.
- [ ] Confirm Fleet reconciles from the new location against the **old** chart namespace.
- [ ] Only then flip the chart namespace on both sides simultaneously.
- [ ] Keep in the public repo: `values-local.yaml.example`, `values-tilt.yaml`. These describe
      the product's dev loop, not an instance of it.
- [ ] Consider `headscale-bootstrap.sh` for HomeLab too — it is instance infrastructure.

Rationale for what leaves: `values-experimental.yaml` is not secrets (those are in Vault) but a
map of the home network — LAN IPs such as `10.0.51.11`, MikroTik split-horizon DNS notes, Vault
paths, and the `srw.works` / `superhuman-remote-worker.com` domain split.

---

## Phase 4 — Create `srw-cloud` and move the sales page

- [ ] Create the private `srw-cloud` repo in the org.
- [ ] Move the sales page into `srw-cloud/www/`: `website/index.html`, `website/og-image.png`.
- [ ] Split `docker/Dockerfile.website` — the sales-page build moves, anything serving the
      installer stays.
- [ ] Move the sales-page image build job out of `develop.yml` into `srw-cloud` CI; it needs its
      own GHCR push credentials.
- [ ] Update `HomeLab/deployments_managed/srw-sales-page/10-deployment.yaml` to the new image ref.
- [ ] Move `[removed]/` (4 files: …
      strategy) into `srw-cloud`.

**Keep public:** `website/configure.html`, `website/generator.mjs`, `website/test/*`. This is the
customer install wizard, not marketing — `generator.drift.test.mjs` is a CI hard-gate that renders
generated values against the real `helm/` tree with kubeconform. It is source-coupled to the chart
and is the best onboarding asset the public repo has.

- [ ] Rename the surviving directory `website/` → `installer/` so it reads as product.
- [ ] Update the drift-gate path references in `develop.yml` (lines ~814, ~854, ~878) and
      `main.yml` (~154, ~182) after the rename.

---

## Phase 5 — Cleanup commits (make the tree final)

All ordinary, reviewable, revertible commits. The public repo's root directory is its front page.

- [ ] **`.gitignore` `HomeLab/`.** It is a separate private repo checked out inside this working
      tree and is currently untracked *but not ignored* — one `git add -A` publishes it. It is the
      only such hazard; everything else untracked is already ignored.
- [ ] Swap the `HomeLab` remote for a credential helper — its `origin` URL currently carries a
      Gitea token inline in plaintext (local config only, but the habit is what leaks).
- [ ] Delete rather than move — dead instance config: `deployment/legacy/` (30 files),
      `deprecated_deployment-local/` (14 files).
- [ ] Delete root-level detritus: `cmdpalette.md`, `palette2.md`, `palette3.md`, `rubin_costs.pdf`,
      `runreal.js`, `Subagent Delegation Interface Design.md`,
      `Variant B - Token Box (standalone).html` (1.8 MB), `Verify Before Done Skill Research.md`,
      `.kateproject`.
- [ ] Triage, don't reflexively delete: `ai-memory-research/` (16), `bench/` (5), `design/` (32),
      `eval/` (26), `researches/` (12). Several are genuine engineering assets — decide per
      directory whether they ship, move to `srw-cloud`, or go.
- [ ] Decide `Officers.md` — feature documentation, so either keep or fold into whatever survives
      of the docs tree.
- [ ] Sanity-check `.env.example` (47 KB) renders as a clean first-run experience. Only 1 line
      matched infra-shaped patterns, so it is in good shape.

**`docs/` — decision recorded, consequence flagged.** The call is that `docs/` moves out. The
tail: **676 tracked files outside `docs/` reference `docs/` paths**, 385 of them in `src/`,
`orchestrator/`, `config/`, `helm/`, `scripts/`, `cockpit/src/`, plus `README.md` (4),
`CLAUDE.md` (4), `AGENTS.md` (6). Moving the tree wholesale leaves all of those dangling.

- [ ] Pick one and note it here before executing:
      (a) move all of `docs/` and accept/fix 676 dangling references;
      (b) move only what should not be public (`[removed]/`, session logs in `docs/issues/`)
          and keep what code cites — `docs/db_migration.md`, `docs/api_key_resolution.md`,
          `docs/features/*` referenced from source comments;
      (c) move all, then add a stub `docs/README.md` pointing at the private location.
- [ ] Whichever is chosen, update `CLAUDE.md` and `README.md` so the public repo's own
      instructions do not point at files that are not there.

---

## Phase 6 — History rewrite (irreversible; everything above must be done first)

With 0 forks and `network_count: 0` this is genuinely clean — no fork network to strand objects
in, no downstream clones to break, no third-party PRs referencing old SHAs.

Reframe the goal: **not** "unpublish the secrets" — that sailed nine months and 457 unique cloners
ago, which is why Phase 0 is rotation. This is about the history being part of the artifact at
launch, and right now it contains a private key file, a credentials overlay, and a home-network map.

- [ ] **Take a `git clone --mirror` to a private archive first.** `filter-repo` rewrites every
      descendant commit, so every SHA after the first touched commit changes. `docs/` is dense
      with commit references (`22b2511e`, `871bdf45`, `83da2983`, `7d72b964`, …) and so is the
      working memory index — all of them dangle afterward. The mirror keeps archaeology working.
- [ ] Install `git-filter-repo` (not currently on this machine; neither is `gitleaks` or
      `trufflehog`).
- [ ] Single pass removing all dead paths at once:
      `deployment/values-local.yaml`, `.local-ssh/`, `deployment/values-experimental.yaml`,
      `deployment/fleet.yaml`, `deployment/legacy/`, `deprecated_deployment-local/`, the root
      detritus, and whatever `docs/` decision was made in Phase 5.
- [ ] Force-push all branches and tags.
- [ ] Ask GitHub Support to garbage-collect unreachable objects. Without this, old commits stay
      reachable forever by direct SHA URL. With 0 forks this actually works cleanly.
- [ ] Re-run the full-history scan afterwards to confirm the tree is clean:
      `git cat-file --batch-all-objects --batch --buffer | grep -aE '<pattern set>'`
- [ ] Verify CI is green post-rewrite and Fleet still reconciles.
- [ ] **Delete this file, or move it into `srw-cloud`.** It is transition scaffolding, and it
      describes where credentials used to live.

---

## Phase 7 — Pre-announce hardening

Right now the only thing reading the repo is bots. After the announcement that changes, and the
next `values-local.yaml` will not be full of `dev_` placeholders.

- [ ] Enable GitHub secret scanning **and push protection** (free on public repos).
- [ ] Add `gitleaks` as a pre-commit hook.
- [ ] Confirm `SECURITY.md` has a working disclosure address.
- [ ] Confirm `THIRD_PARTY_LICENSES.md` is current against `requirements.txt`.
- [ ] Re-read `README.md` as a stranger: install path is the `installer/` wizard → `helm install`
      from `oci://ghcr.io/superhuman-remote-worker/charts/...`, and it must actually work from a
      cold clone.
- [ ] Verify anonymous `helm pull` of the published chart succeeds with no credentials.

---

## Phase 8 — Announce

- [ ] Sales page live on the new image ref.
- [ ] Announcement copy says "source-available", never "open source".
- [ ] The FSL Competing-Use terms and the two-year Apache-2.0 conversion are stated plainly —
      it is the strongest part of the story and gets misread if left implicit.

---

## Reference

**Exposure snapshot, 2026-08-13.** Public since 2025-11-03. 0 forks, `network_count` 0, 1 star,
0 watchers. 14-day traffic: 2,128 clones / 457 unique cloners; 340 views / 3 unique viewers.
20,267 objects scanned in history.

**Full-history secret scan** (the command used, for re-running in Phase 6):

```bash
PAT='sk-ant-api03-[A-Za-z0-9_-]{40,}|sk-proj-[A-Za-z0-9_-]{40,}|sk-or-v1-[a-f0-9]{48,}|tvly-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{40,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|xox[baprs]-[0-9A-Za-z-]{20,}|hvs\.[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}|sk_live_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
git cat-file --batch-all-objects --batch --buffer 2>/dev/null | grep -aEo "$PAT" | sort -u
```

Note the pattern set initially missed `tvly-`; extend it rather than trusting a prior clean run.

**Prior art for the split.** Sentry are the FSL authors and are in the same position:
`getsentry/sentry` is public and feature-complete under FSL; the private `getsentry/getsentry`
imports it and adds billing, quotas and plan management, hooking in via Django signals, swappable
backends (`sentry.quotas`, `sentry.nodestore`) and a feature-flag handler; `getsentry/self-hosted`
is a third public repo holding the packaged install. Nothing is held back from the public repo —
the licence does the protecting, not feature removal. GitLab is the counter-example worth avoiding
here: one repo with a proprietary `ee/` directory behind a runtime licence key, chosen to escape
cherry-pick pain, at the cost of a decade explaining what their public repo actually is.
