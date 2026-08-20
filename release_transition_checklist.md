# OSS Release — Transition Checklist

Working scaffolding for moving this repo into the `superhuman-remote-worker` GitHub org,
splitting out the non-product pieces, and cleaning the history before the public release.

**This file is transition scaffolding, not product documentation.** It gets deleted (or moved
into `srw-cloud`) in Phase 6 before the announcement — see the last item.

Drafted 2026-08-13. Current as of **2026-08-17**.

| Phase | State |
|---|---|
| 0 — Rotate credentials | **not started.** Independent of everything else; nothing blocks it |
| 1 — Transfer repo to org | not started. Blocks 2 |
| 2 — Migrate GHCR packages | not started. Blocked by 1 |
| 3 — Deploy config → HomeLab | not started, and **larger than drafted** — see below |
| 4 — `srw-cloud` + sales page | **DONE 2026-08-15.** Live and verified. One item deferred into 5 |
| 5 — Cleanup commits | **6 of 15 done 2026-08-17.** Root and gitignore are clean. Remaining: two dead directories to delete, the `helm/values.yaml` home-network defaults, a triage, and two decisions |
| 6 — History rewrite | not started. Gated on everything above |
| 7 — Pre-announce hardening | not started |
| 8 — Announce | not started |

**Done on 2026-08-17** (all uncommitted at time of writing, one working tree):

- Swept the whole tree for anything else that should leave. Findings folded into the phases
  below; the negative results are in *Audited and clean* near the end, so they are not re-audited.
- Deleted `deployment/deploy.sh` and `design/asset-pack/` (28 files) — both dead or duplicate.
- Cleared the root directory: 6.0 MB of Playwright dumps, a fake PDF, two bundled mockups and a
  scratch script. Root is now five markdown files, all of which belong there.
- Filed three root files that were **not** detritus into `researches/` and `knowledge-base/knowledge/features/`,
  with all six citations repointed.
- `.kateproject` untracked and gitignored, kept on disk.
- Resolved Phase 5's open directory triage: three of the five candidates were documents wearing
  a root-folder costume and moved into `docs/` (`knowledge-base/knowledge/research/skills/`,
  `knowledge-base/knowledge/research/ai_memory/`, `knowledge-base/knowledge/design/cockpit/`); `eval/` and `bench/` stay because they are
  code. **The repo root now holds 17 directories, down from 20.**

**The one thing to read before scheduling anything: Phase 3 is bigger than it looks.** It was
drafted as a file move. It is actually a re-plumbing of the dev deploy loop, because CI commits
image tags back into the very paths being relocated.

**Two decisions are still owed and block nothing else, so they can be made any time:** the `docs/`
split (Phase 5) and whether `config/`'s prompt library publishes as-is (Phase 5).

---

## Decisions already made

Recorded so they don't get relitigated mid-transition.

**Repo landscape — three repos, only one of which is new.**

| Repo | Visibility | Holds |
|---|---|---|
| `superhuman-remote-worker/<renamed>` | public, FSL-1.1-ALv2 | the product: `src/`, `orchestrator/`, `cockpit/`, `helm/`, `config/`, `docker/`, `tests/`, `values-local.yaml.example`, `values-tilt.yaml`. **The installer is NOT here** — see Phase 4; it moved to `srw-cloud` with the sales page. |
| `HomeLab` (exists, private, self-hosted Gitea) | private | GitOps/deploy: `values-experimental.yaml`, `fleet.yaml`, alongside the existing `deployments_managed/srw-sales-page/` |
| `srw-cloud` | private | SaaS layer: payments, multi-tenancy, tenant provisioning, entitlements — plus the sales page **and the self-host installer** under `www/` |

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
- [ ] Update every reference in one commit. ~20 distinct `ghcr.io/knaeckebrothero/*` paths —
      **70 occurrences across 32 files** (recount 2026-08-17, excluding `docs/`), concentrated in:
      - `helm/values.yaml` (8 refs) and `helm-vm-cluster/values.yaml` (3)
      - `docker-compose.yaml` (10)
      - `.github/workflows/develop.yml` (3)
      - `orchestrator/services/agent_provisioner.py` (2)
      - the `CHART` constant in `www/generator.mjs` — **now in the `srw-cloud` repo**, so this
        one is a cross-repo edit and will not show up in a grep of this tree
      - `deployment-vms/srw-vm-controller/fleet.yaml`, `helm-vm-cluster/templates/_helpers.tpl`,
        `docker/agent-vm-base/Dockerfile.containerDisk`, `vm/controller/controller.py`
      - tests asserting image refs: `test_vm_provisioner.py`, `test_vm_controller.py`,
        `test_persistent_provisioner.py`, `test_infrastructure_metering_vm_cluster_helm.py`,
        `test_vm_template_description_escaping.py`
- [ ] **The Go module path is coupled to the repo name** — added 2026-08-17, missed in the
      original draft. `vm/sudo-daemon/go.mod` declares
      `module github.com/knaeckebrothero/superhuman-remote-worker/sudo-gated`, with three internal
      imports resolving off it (`cmd/sudo-gated/main.go` ×2, `internal/gate/handler.go`). This is
      Phase 1's rename, not Phase 2's registry move, and it breaks the build rather than a pull:
      `go build` fails the moment the module path and the repo disagree. `go.mod` and all three
      imports change together, in one commit.
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
- [ ] **Move `deployment-vms/` (4 files) as well** — see below. Same domain, same move.
- [ ] Confirm Fleet reconciles from the new location against the **old** chart namespace.
- [ ] Only then flip the chart namespace on both sides simultaneously.
- [ ] Keep in the public repo: `values-local.yaml.example`, `values-tilt.yaml`. These describe
      the product's dev loop, not an instance of it.
- [ ] Consider `headscale-bootstrap.sh` for HomeLab too — it is instance infrastructure.
- [x] `deployment/deploy.sh` — **deleted 2026-08-17**, not moved. It rewrote image tags in
      `MANIFEST_DIR="$SCRIPT_DIR"`, i.e. the `deployment/legacy/` manifests that Phase 5 deletes;
      it had no purpose once the chart took over. Its `ignorePaths` entry in `fleet.yaml` went
      with it.

Rationale for what leaves: `values-experimental.yaml` is not secrets (those are in Vault) but a
map of the home network — LAN IPs such as `10.0.51.11`, MikroTik split-horizon DNS notes, Vault
paths, and the `srw.works` / `superhuman-remote-worker.com` domain split.

**`deployment-vms/` — added 2026-08-17, missed in the original draft.** Four files, and
`srw-vm-controller/fleet.yaml` is the most instance-specific file left in the repo — more so than
`values-experimental.yaml`, which is the one this phase was written around. It carries Vault
paths (`homelab/agent-vms/headscale-api-key`, `homelab/superhuman-remote-worker/srw-secrets`),
`headscale.h4ll.app`, the NATS hub at `10.0.51.14`, a `hostAliases` entry pinning
`api.srw.works` to `10.0.51.11`, the SSH **public** key authorised into every agent VM
(`srw-agent-vm-access`), Rancher cluster display-name selectors, and a closing comment that
already points at its sibling `HomeLab/rancher_cluster/fleet-gitrepo-srw-vms.yaml`. It is also a
Fleet bundle reconciled continuously by a cluster controller — the exact "different security and
cadence domain" that justifies `HomeLab` existing at all.

- `kubevirt/fleet.yaml` + `overcommit.yaml` — Rancher cluster selectors, KubeVirt node overcommit.
- `cdi/install.sh` — generic upstream CDI installer, but it is cluster bootstrap; travels with them.
- Sequencing note: this bundle pins `oci://ghcr.io/knaeckebrothero/charts/srw-dev-vm-cluster` at
  its own version, **independent of** `deployment/fleet.yaml`'s `srw-dev` pin. Phase 2's namespace
  flip has to move both.

**This phase is not a file move — it is re-plumbing the dev deploy loop.** The single biggest
thing the original draft missed. `develop.yml` ends with a deploy job that runs six `yq -i` steps
stamping chart versions and per-component image tags into exactly the three paths this phase
relocates — `deployment/fleet.yaml`, `deployment/values-experimental.yaml`,
`deployment-vms/srw-vm-controller/fleet.yaml` — then does `git add deployment/fleet.yaml
deployment/values-experimental.yaml deployment-vms/ && git commit && git push` as
`github-actions[bot]`. Continuous deployment on dev *is* CI committing back into this repo.

Move the files and that loop breaks silently: the `yq` steps keep succeeding against paths
nothing reads, the git-diff gate sees no delta, and dev quietly stops picking up new images. So
decide the mechanism before moving anything:

- [ ] Pick one and note it here before executing:
      (a) CI pushes to `HomeLab` — needs a Gitea credential as an Actions secret, since
          `GITHUB_TOKEN` has no reach there. Straightforward, but it gives a **public** repo's CI
          write access to the private GitOps repo, which is a real supply-chain surface once the
          repo is public and anyone can open a PR against the workflow file;
      (b) invert the direction — let Fleet resolve tags itself (image scan / digest tracking) so
          nothing has to write a file at all. More work now, no cross-repo credential ever;
      (c) keep a thin `deployment/` in the public repo purely as CI's write target, and have
          HomeLab read from it. Cheapest, and concedes the point of the phase.
- [ ] Whichever is chosen, the workflow path edits land in the **same commit** as the move.
- [ ] Re-check `develop.yml`'s deploy-job guards afterwards. Today the trigger is `pull_request`
      (**not** `pull_request_target`), so fork PRs get a read-only token and cannot reach the push
      step. Any move to (a) must preserve that property — a cross-repo credential exposed to a
      fork-PR-triggered job is the whole attack.

---

## Phase 4 — Create `srw-cloud` and move the sales page — **DONE 2026-08-15**

Executed, with one decision overruled: the installer moved too. The recorded plan below said
"keep the installer public" and rename `website/` → `installer/`. That was rejected in favour of
moving the whole marketing+installer surface, because both pages ship in ONE nginx image at one
origin — splitting them across repos would have meant two images and a Traefik path rule to keep
`/configure` under the apex domain.

- [x] Create the private `srw-cloud` repo in the org.
- [x] Move sales page AND installer into `srw-cloud/www/` — `index.html`, `og-image.png`,
      `configure.html`, `generator.mjs`, `test/*`. Extracted with `git filter-repo`
      (`--path-rename website/:www/`), 23 commits of history preserved.
- [x] `docker/Dockerfile.website` moved whole to `srw-cloud/docker/Dockerfile.www`. No split
      needed once both pages travelled together.
- [x] Image build moved to srw-cloud's own `ci.yml`. New image
      `ghcr.io/superhuman-remote-worker/srw-www` (a partial Phase 2 — an org repo's
      `GITHUB_TOKEN` cannot push to the `knaeckebrothero` namespace).
- [x] `HomeLab/deployments_managed/srw-sales-page/10-deployment.yaml` repointed; live and
      verified (`/`, `/configure`, `/generator.mjs`, `/og-image.png` all 200).
- [ ] Move `knowledge-base/knowledge/strategy/` into `srw-cloud`. **Deferred** — folded into the wider `docs/`
      decision in Phase 5, which is being handled separately.

**The package is PRIVATE, and that cost more than expected.** A GitHub App with `packages:read`
does not work: GHCR rejects App installation tokens (GitHub staff confirmation, community
discussion #171423, still open July 2026); fine-grained PATs fail the same way. Only a classic
PAT with `read:packages` works — `repo` scope is not needed. It is stored at
`secret/homelab/srw-sales-page/ghcr-pull` and templated into a `dockerconfigjson` by
`01-eso.yaml`. **That PAT expires and nothing warns you**; the symptom is `ImagePullBackOff` on a
pod restart.

**The chart↔installer contract survived the split**, in two halves:

- *This repo:* `helm/ci/installer-{evaluation,production,production-vms}-values.yaml` — the
  generator's verbatim output, rendered by the existing `chart-test` matrix in both workflows.
  A chart change that breaks the installer now fails HERE, in the repo that caused it.
- *srw-cloud:* the drift gate still renders generated values against the real chart, fetched
  from this repo's `develop` by `scripts/fetch-chart.sh`, plus a weekly schedule.

Neither half is sufficient alone: the fixtures cannot see a change to the generator, and the
drift gate cannot see a chart change until it runs. Regenerate the fixtures from srw-cloud's
`www/test/generator.drift.test.mjs` CASES whenever the generator changes.

Note for Phase 6: the chart the installer targets is **stale**. Newest published is `0.0.23`
(2026-06-08), which predates `helm/values.schema.json`; `main` is 2000+ commits behind `develop`.
Until a fresh release is cut, the only chart a customer can install has no schema validation.

---

## Phase 5 — Cleanup commits (make the tree final)

All ordinary, reviewable, revertible commits. The public repo's root directory is its front page.

- [x] **`.gitignore` `HomeLab/` — done.** It is a separate private repo checked out inside this
      working tree and was untracked *but not ignored*, so one `git add -A` would have published
      it. `srw-cloud/` arrived later with the same problem and is ignored too (`.gitignore` 239,
      242). Re-verified 2026-08-17: seven foreign repos/dirs now sit inside this tree
      (`HomeLab`, `srw-cloud`, `knowledge-base`, `KurortEngine*`, `BetterResavio-KB`) and **all**
      are ignored — `git status --porcelain` reports no untracked, unignored path anywhere.
- [ ] Swap the `HomeLab` remote for a credential helper — its `origin` URL currently carries a
      Gitea token inline in plaintext (local config only, but the habit is what leaks).
- [ ] Delete rather than move — dead instance config: `deployment/legacy/` (**27** files),
      `deprecated_deployment-local/` (**12** files). (Recounted 2026-08-17; the draft's 30/14 were
      high.) These are the last two directories on the delete list.
- [x] **Root-level detritus deleted 2026-08-17** — 6.0 MB. What each actually was, since the
      original draft listed them by filename without checking:
      - `cmdpalette.md`, `palette2.md`, `palette3.md` (37 KB) — Playwright **accessibility-tree
        dumps** of a code-server/VS Code window (`[ref=e31]`, "Toggle Primary Side Bar (Ctrl+B)").
        Raw tool output pasted to disk.
      - `rubin_costs.pdf` — **not a PDF.** A Cloudflare "Just a moment…" bot-challenge page saved
        with a `.pdf` extension: 0 PDF objects, 0 pages, a `cRay` token inside. A failed fetch of
        `philarchive.org/archive/RUBTCO-14`, committed 2026-06-25 with the project-onboarding
        research. Whatever it was gathered to cite never arrived.
      - `runreal.js` — one-off Playwright benchmark against `http://127.0.0.1:8972/real-css.html`,
        a file present nowhere in the repo.
      - `Variant B - Token Box (standalone).html` (1.8 MB) — bundled UI mockup.
      - `Delegate-A-Compact-List-Rows.html`, `Delegate-D-Hierarchical-Tree.html` (2.1 MB each) —
        delegation-UI mockups, **untracked with 0 commits**, so they never entered history and
        need nothing from Phase 6.
- [x] `.kateproject` — **untracked 2026-08-17** (`git rm --cached`) and added to `.gitignore`,
      rather than deleted. It is a working local editor file; it just should not be published.
      Still needs the Phase 6 pass to leave history.
- [x] **Correction to the original draft: two entries on that detritus list were not detritus.**
      Both were substantive research reports, orphaned by location rather than obsolete. **Filed
      2026-08-17 rather than deleted**, with all citations repointed:
      - `Verify Before Done Skill Research.md` → `researches/verify-before-done.report.md`. Every
        brief in `researches/` has an `X.md` + `X.report.md` pair, and `verify-before-done` was
        the **only** one missing its report — because the report was in the repo root. It is also
        the cited evidence base for a shipped skill. The folder now pairs up completely.
      - `Subagent Delegation Interface Design.md` → `researches/subagent-delegation.report.md`.
        No prompt file was ever filed for this one, so it is the folder's lone unpaired report;
        its header now says so. Its header also carries forward the caveat already on record in
        `knowledge-history/done/loop_subagent_forensics.md` — the report contains **known-synthetic** figures
        (DeepSeek "128 parallel calls", Kimi "300 subagents"), so its direction is usable and its
        numbers are not. The real decision record is the reconciliation section in
        `knowledge-base/knowledge/issues/delegation_light_mode_missing.md`.
- [x] `Officers.md` → `knowledge-base/knowledge/features/officers.md`, **done 2026-08-17.** Not root detritus either:
      `knowledge-base/knowledge/features/centurion.md` cites it in Sources as the consolidated officer notes. It was an
      Obsidian note with frontmatter sitting outside the vault; it now rides along with whatever
      `docs/` decision gets made below. Both citations in `centurion.md` repointed.
- [x] **`design/asset-pack/` deleted 2026-08-17** (28 files, 228 KB). It was a second copy of the
      PWA assets, self-described as "the originals" and requiring every icon regeneration to be
      applied twice. Verified file-by-file before deleting: 24 of 27 byte-identical to
      `cockpit/public/` or `cockpit/src/assets/`, and the three that differed were all the *stale*
      side — `manifest.webmanifest` and `head-snippet.html` still carried the pre-Travertine
      `#9c1f2e` brand red, and `microcopy.json` was fully superseded by the `pwa` namespace in
      `cockpit/src/assets/i18n/`. `design/README.md` updated to point at the shipped locations and
      to record why the mirror is not coming back.
- [x] **Triage resolved 2026-08-17 — three of the five were documents, not product, and moved
      into `docs/`.** The original framing ("ship, move to `srw-cloud`, or go") had a false
      premise: that being cited somewhere was a reason to leave a folder at the repo root. It is
      a reason to update the citation. The real test is product vs. working knowledge, and the
      file types answer it — the three that moved contain **zero code** between them:
      - `researches/` (14 `.md`) → **`knowledge-base/knowledge/research/skills/`**. Skill-authoring evidence base.
      - `ai-memory-research/` (11 `.md` + 5 `.json`) → **`knowledge-base/knowledge/research/ai_memory/`**.
      - `design/` (2 `.md` + 3 `.reference.*` files that nothing in the cockpit build imports) →
        **`knowledge-base/knowledge/design/cockpit/`**.

      Two of these were merging into folders that **already existed** — `knowledge-base/knowledge/research/` and
      `knowledge-base/knowledge/design/` — so the root copies were a navigational trap independent of this release.
      All inbound citations rewritten across 8 files, plus the relative links inside the moved
      trees (their depth changed by two levels); every link verified to resolve.

      **Consequence, stated so it is not a surprise:** this research now inherits whatever the
      `docs/` decision below turns out to be. If `docs/` goes private, so does it. That is the
      intent — none of it is something a self-hoster needs — but it is now one decision instead
      of four.
- [x] **`eval/` and `bench/` stay at the repo root, and they publish. Decided 2026-08-20.** `eval/` is a
      Python package importing `src.services.memory`, `src.core.loader`, `src.database.postgres_db`
      and `orchestrator.database.migrate` in 20+ places; it cannot leave `src/`'s side. `bench/`
      has no product imports (it talks HTTP to `SRW_API_URL`) so it *could* live anywhere, but it
      is a live instrument and there is no `tools/` folder worth creating for one thing.

      The open half — *whether* they publish — was raised again as "move `eval/` into `srw-cloud` so
      competitors cannot use it." The answer is no, for five reasons:

      - **Dependency direction forbids it.** `tests/test_memory_eval_harness.py`,
        `tests/test_app_guide_eval_harness.py` and `tests/test_app_guide_capability_eval_harness.py`
        all import `eval.*`. Moving `eval/` leaves the *public* repo importing from the *private* one —
        the exact inversion of the load-bearing rule above — or forces those three files (2,172 lines)
        out of the public suite, deleting the public repo's coverage of its own memory and app-guide
        seams. `eval/` also imports `src/` and `orchestrator/` in 20+ places, so the result is a
        two-way coupling across a repo boundary.
      - **The moat is not in the harness.** `eval/memory/README.md` is explicit that it drives the
        production seam — `MemoryManager.assemble()` / `capture()` — directly. The system it measures,
        `src/services/memory/`, publishes under FSL either way; the instrument adds nothing a competitor
        holding that code would need. LongMemEval is a public benchmark (arXiv:2410.10813), and
        `eval/memory/data/` + `eval/memory/runs/` are already gitignored, so datasets, judge labels and
        results are not public regardless.
      - **It contradicts the model already chosen.** Per the Sentry prior art in Reference below: the
        licence does the protecting, not feature removal. Carving the eval out is a first step toward
        the GitLab `ee/` shape this transition explicitly set out to avoid.
      - **Cross-repo drift, on the highest-churn surface.** The chart drift gate already needed a weekly
        scheduled CI run purely because the generator and the chart live in different repos. Memory
        changes far more often than the chart; splitting the harness from its subject rebuilds that
        problem somewhere it costs more.
      - **The published eval is a credibility asset.** It is what turns a memory claim into something a
        self-hoster, a technical buyer, or a thesis reader can check.

      **The line to hold as this grows: harness public, customer-derived data private.** What would
      justify `srw-cloud` is a proprietary *task distribution* — pilot-customer traces, eval sets built
      from usage paid for. None exists yet. When it does, that data goes to `srw-cloud` and the harness
      that reads it stays here.
- [ ] **Sub-question left open: does `eval/app_guide/`'s corpus stay public?** `cases.yaml` (576 lines)
      and `capability_cases.yaml` (230) carry prompts *and* scoring expectations. Their "held-out" fence
      is stated in the file header as *outside `config/skills/app-guide/`* — held out from the runtime
      skill's own retrieval surface, so the guide cannot read its own answer key at eval time. That fence
      is intact and unaffected by GitHub visibility. The residual risk is only long-run training-data
      contamination, and it is already partly moot: 16 real case ids and their passing phrasings are
      hard-coded in the public `tests/test_app_guide_eval_harness.py`, so privatising the YAML alone
      buys partial containment. Full containment means a synthetic public fixture plus rewriting ~28
      corpus-coupled tests against it. Decide before the Phase 6 announcement.
- [ ] **Pre-existing collision the `docs/` decision has to settle:** `docs/` holds **both**
      `research/` (30 files, `stateless_agents/` + the two trees added above) and `researches/`
      (15 PDFs). Same species, two names. Not created by this transition, but merging them is
      cheapest while the tree is being reorganised anyway.
- [ ] Sanity-check `.env.example` (47 KB) renders as a clean first-run experience. Only 1 line
      matched infra-shaped patterns, so it is in good shape.

**`docs/` — decision recorded, consequence flagged.** The call is that `docs/` moves out. The
tail: **676 tracked files outside `docs/` reference `docs/` paths**, 385 of them in `src/`,
`orchestrator/`, `config/`, `helm/`, `scripts/`, `cockpit/src/`, plus `README.md` (4),
`CLAUDE.md` (4), `AGENTS.md` (6). Moving the tree wholesale leaves all of those dangling.

- [ ] Pick one and note it here before executing:
      (a) move all of `docs/` and accept/fix 676 dangling references;
      (b) move only what should not be public (`knowledge-base/knowledge/strategy/`, session logs in `knowledge-base/knowledge/issues/`)
          and keep what code cites — `knowledge-base/knowledge/db_migration.md`, `knowledge-base/knowledge/api_key_resolution.md`,
          `knowledge-base/knowledge/features/*` referenced from source comments;
      (c) move all, then add a stub `knowledge-base/knowledge/README.md` pointing at the private location.
- [ ] Whichever is chosen, update `CLAUDE.md` and `README.md` so the public repo's own
      instructions do not point at files that are not there.
- [ ] **`docs/` still describes a `website/` that left in Phase 4.** Found 2026-08-17 while
      sweeping for dangling references; left alone because `docs/` is being handled separately,
      but these are not archives — they are wrong right now:
      - `knowledge-base/knowledge/website.md` §"lives in the SRW repo under `website/`" (lines ~173-180) states the
        source paths, the image `ghcr.io/knaeckebrothero/…`, the `build-website` CI job and the
        publish procedure. All four are false since 2026-08-15.
      - `knowledge-history/drafts/sales_page_improvement_instructions.md` — a runnable instruction set aimed
        at `website/index.html`, `website/test/*` and `docker/Dockerfile.website`. Handed to an
        agent today it fails on the first path.
      - `knowledge-base/knowledge/superpowers/specs/2026-08-13-waitlist-design.md` — a *pending* design whose page
        edits, byte budget and `COPY`-line changes all target the other repo now.
      The dated plan/spec pair from 2026-06-18/19 is correct **as history** — leave those.

**Fix in place — not a move.** `helm/values.yaml` ships the home network as the **default** for a
chart strangers are meant to install: `10.0.50.0/24` and `10.0.51.0/24` as network-policy
allow-CIDRs at three sites (lines ~467, ~1661, ~1672), and
`kbGitAllowedHosts: "git.srw.works,srw-gitea:3000"` (~163). RFC1918, so not a leak — but it is
the same home-network map this transition takes out of `values-experimental.yaml`, and it makes
the default install wrong for everyone who is not us. Five lines.

- [ ] Replace the CIDR defaults with empty/documented placeholders and move the real values into
      `values-experimental.yaml`, where the rest of the instance config already lives.
- [ ] Default `kbGitAllowedHosts` to empty. Note the existing behaviour first: empty currently
      blocks in-cluster Gitea, which is a known dev-side trap — check that is still the semantics
      before flipping it.

**One strategic call, deliberately flagged rather than defaulted.** `config/` publishes **75
experts, 64 prompts, 32 skills and 11 guardrails** verbatim. FSL stops someone shipping a
competing product; it does not stop them lifting the prompt library into something that is not
competing. That is plausibly the right trade — it is also the most copyable asset in the tree,
and the only item in this checklist that ships by default without anyone having decided it.

- [ ] Decide explicitly: publish `config/` as-is, or hold some subset back. Record the answer
      here either way, so it reads as a choice rather than an oversight.

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
- [x] `git-filter-repo` is installed (`~/.local/bin/git-filter-repo`) — it was used for the
      Phase 4 extraction. `gitleaks` and `trufflehog` are still **not** on this machine; Phase 7
      wants `gitleaks`.
- [ ] Single pass removing all dead paths at once. Enumerated 2026-08-17 — the draft said "the
      root detritus", which is no longer specific enough to execute from:

      *Credential-bearing (the original reason for this phase):*
      `deployment/values-local.yaml`, `.local-ssh/`

      *Instance config, moved to HomeLab in Phase 3:*
      `deployment/values-experimental.yaml`, `deployment/fleet.yaml`, `deployment-vms/`,
      and `headscale-bootstrap.sh` if Phase 3 takes it

      *Deleted in Phase 5, still in history:*
      `deployment/legacy/`, `deprecated_deployment-local/`, `deployment/deploy.sh`,
      `design/asset-pack/`, `cmdpalette.md`, `palette2.md`, `palette3.md`, `rubin_costs.pdf`,
      `runreal.js`, `Variant B - Token Box (standalone).html`, `.kateproject`

      *Whatever `docs/` decision was made in Phase 5.*

- [ ] **Decide separately whether `website/` leaves history.** Phase 4 moved it to `srw-cloud`
      (with its 23 commits preserved there), so it is dead weight in this tree — but it is
      marketing copy, not credentials, and removing it rewrites a large slice of history for
      tidiness alone. Cheap to include in the pass that is happening anyway; not a reason to run
      one. Same question for `docker/Dockerfile.website`.
- [ ] **Do not bother with the two `Delegate-*.html` mockups** (2.1 MB each). They were untracked
      with zero commits and were deleted from disk on 2026-08-17 — they were never in history and
      nothing needs to remove them.

**Scale check, measured 2026-08-17.** The history is 3,640 commits. 597 of them touch
`deployment/fleet.yaml`, `values-experimental.yaml` or `deployment-vms/` — and **498 of those are
CI's `deploy: update image tags to sha-…` commits**, i.e. the write-back loop described in Phase 3.
Two consequences:

- Removing those paths rewrites roughly a sixth of the history, and `filter-repo` prunes commits
  that become empty, so those 498 deploy commits **disappear entirely**. That is a large, visible
  change to what the log looks like at launch — mostly an improvement, but decide it deliberately
  rather than discovering it after the force-push.
- It is another argument for settling Phase 3's mechanism first. If the loop moves to HomeLab or
  goes away, the noise stops accumulating; if it does not, this history grows by roughly one
  deploy commit per push to `develop` forever.
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
- [ ] Re-read `README.md` as a stranger. The install path is now the **hosted** wizard at
      `https://superhuman-remote-worker.com/configure` → `helm install` from
      `oci://ghcr.io/superhuman-remote-worker/charts/...`. There is no in-repo installer to point
      at any more, and `README.md` currently links to neither — it must, and the published chart
      must actually be installable (today's newest is the stale `0.0.23`).
- [ ] Verify anonymous `helm pull` of the published chart succeeds with no credentials.

---

## Phase 8 — Announce

- [ ] Sales page live on the new image ref.
- [ ] Announcement copy says "source-available", never "open source".
- [ ] The FSL Competing-Use terms and the two-year Apache-2.0 conversion are stated plainly —
      it is the strongest part of the story and gets misread if left implicit.

---

## Audited and clean — do not re-audit

Swept 2026-08-17, hunting for anything else that should leave the repo before it goes public.
Everything below was checked and found fine. Recorded so the next pass does not spend the effort
again — and so a "that looks alarming" reaction has an answer.

- **No SaaS code has leaked into the product tree.** No Stripe, Paddle, LemonSqueezy, checkout
  session or subscription handling anywhere in `src/`, `orchestrator/`, `cockpit/src/`. The
  metering and pricing machinery is the hook-point embryo already accounted for above. The
  dependency-direction rule is holding on its own so far.
- **`usage_rate_cards` is not commercially sensitive**, despite the name. Checked because "rate
  card" reads like margins: it is AWS / Azure / STACKIT **public list prices** used to reprice
  already-metered CPU and RAM for comparison, and migration `0082` says so in a table comment —
  "planning estimates, never provider billing". No cost basis, no markup, no SRW pricing.
- **CI is safe to publish.** Everything runs on `ubuntu-latest`, and `ci-policy.yml` exists
  specifically to hard-gate any job routing to a self-hosted label — the homelab runners are
  never reachable from a public PR. Trigger is `pull_request`, not `pull_request_target`.
- **`docker/keycloak/realm-export.json` bakes in no secrets** — every client secret is a
  `${ENV_VAR}` placeholder.
- **`.env.example`** — 86 assignments, none matched an infra-shaped pattern. Confirms the
  original draft's "1 line" note; it is in good shape.
- **No pilot or customer names anywhere outside `docs/`.**
- **Trademarks need no separate policy.** The FSL text already carries both a Patents and a
  Trademarks clause, so the brand marks that necessarily ship inside `cockpit/` are covered by
  the licence rather than by holding assets back.

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
