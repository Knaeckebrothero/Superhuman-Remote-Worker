# Agents cannot render their own HTML — two stacked blockers (VM lacks `browser-exec`; `file://` blocked platform-wide)

> **Status**: Diagnosed **and fixed** 2026-07-17. All claims below are now **measured on the
> live VM**, not inferred (§6). Fix built + verified end-to-end; **uncommitted**.
> **Found via**: job `4eba7f2f-3e24-4b52-82c3-1929ce8c6771` — "Design the UI theme and
> complete mockup suite for Hotel Rheinland ERP", main dev cluster. A 40+-screen HTML
> design job that has visually verified **zero** of its mockups.
> **Severity**: High. Blocker 1 is VM-only (the RSI loop runs on VMs); **blocker 2 affects every
> backend**, container included.
> **Note**: the filename under-describes this — blocker 2 has nothing to do with VMs.
> **Related**: `docs/features/browser_workspace_executor.md`,
> `docs/issues/remove_local_browser_fallback.md`, `project_vm_golden_image`.

## TL;DR

Two independent bugs were stacked, and the second was hidden behind the first. Fixing only the
one we could see would have looked like the fix did nothing.

**Blocker 1 — the VM has no `browser-exec` (VM-only).** The `browser-exec` migration wired the
**container** image only. The VM golden image installs Playwright Chromium and the
`agent-chromium` symlink — but never `browser-use` or the `browser-exec` script. Since the
in-pod fallback was removed (2026-06-11), `browser-exec` is the **only** browser path, so
VM-backed jobs have no renderer at all, and fail silently.

It fails worse than a missing dependency: the agent cannot distinguish *"the daemon isn't
installed"* from *"no browser exists here"*. It probes conventional binary names (`chromium`,
`google-chrome`, `firefox`, `wkhtmltoimage`) — **none of which match the platform's deliberately
non-standard `agent-chromium`** — concludes **"No renderer available"**, and the observer
promotes that to a **0.92-confidence memory recalled 57 times**. A backend-specific infra gap
calcified into a permanent capability belief that steered every verification step into
source-only `grep` checks.

Chromium was sitting unused at `/usr/local/bin/agent-chromium` the entire time — **confirmed
working**: `Google Chrome for Testing 147.0.7727.15`, symlink intact, on that exact VM.

**Blocker 2 — `file://` is blocked on every backend (platform-wide, newly found).** browser-use's
security watchdog rejects any URL without a hostname *before* it consults `allowed_domains`, so
`file:///…/mockup.html` is refused unconditionally and no setting turns it off. Every mockup an
agent writes is a local file, and no tool serves them over HTTP — so **no SRW job on any backend
could ever look at its own work.** Reproduced identically on a live container workspace. This was
never VM-specific and would have survived the blocker-1 fix, merely changing the error from
"no renderer" to "blocked by security policy".

---

## 1. The failure chain

1. Job `4eba7f2f` is provisioned onto a **VM** workspace (shell prompt observed:
   `agent-host@agent-vm-4eba7f2f-3e24-4b52-82c3-1929ce8c6771:~/workspace$`).
2. Agent calls `browser_navigate` → `ToolContext.browser_exec()` →
   `backend.exec_command("browser-exec <action> --json …")` (`src/tools/context.py:922`).
3. `browser-exec` **does not exist on the VM** → `command not found` → empty stdout.
4. `src/tools/context.py:931` returns the flat error `{"error": "browser-exec returned no
   output"}`. This string appears verbatim in the agent's memory (§2.4).
5. Agent falls back to improvised `run_command` probing for conventional browser binaries:
   `chromium`, `chromium-browser`, `google-chrome`, `google-chrome-stable`, `firefox`,
   `wkhtmltoimage` — **all not-found by design**. The only browser binary on the box is the
   non-standard symlink `/usr/local/bin/agent-chromium`, a name that appears **nowhere in
   `src/` or `config/`** (grep-confirmed). The agent has no way to know it exists.
6. Agent concludes **"No renderer available"** and adopts a static-only verification posture.
7. The observer extracts this as an **`ERROR SOLUTION` memory, confidence 0.92**, created at
   Phase 4, **57 accesses**, last used 16/07/2026 18:30.
8. Every subsequent phase recalls it. Every verification todo now ends with *"retain the
   rendered-browser limitation"*. The belief is self-reinforcing and never re-tested.

## 2. Evidence

### 2.1 The image drift — exact delta

The two workspace backends' browser stanzas are near-identical twins. `Dockerfile.workspace`'s
own comment even cross-references the VM script. They drifted in exactly **two** places:

| Step | Container (`docker/Dockerfile.workspace`) | VM (`docker/agent-vm-base/scripts/provision-stage1.sh`) |
|---|---|---|
| `playwright==1.59.0` | ✅ `:192` | ✅ `:228` |
| **`browser-use>=0.12.9,<0.13.0`** | ✅ `:192` | ❌ **missing** |
| `fonts-noto-core` | ✅ `:193` | ✅ `:230` |
| `playwright install --with-deps chromium` | ✅ `:194` | ✅ `:231` |
| Chromium find + `ln -sf … /usr/local/bin/agent-chromium` | ✅ `:197–199` | ✅ `:235–237` |
| `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright` | ✅ `:201` (ENV) | ✅ `:238` (`/etc/environment`) |
| **`COPY docker/browser-exec → /usr/local/bin/browser-exec` + `chmod +x`** | ✅ `:208–209` | ❌ **missing** |

`grep -rniE 'browser-exec|browser_exec|BROWSER_EXEC' docker/agent-vm-base/` returns **zero
hits** across the entire tree (`stage1.pkr.hcl`, `stage2.pkr.hcl`, `scripts/`, `files/`).

So the VM has the *engine* (Chromium) but not the *driver* (`browser-exec`) or its runtime
dependency (`browser-use`).

### 2.2 The error path is indistinguishable from "no browser"

```python
# src/tools/context.py:922
cmd = f"browser-exec {shlex.quote(action)} --json {shlex.quote(payload)}"
...
# src/tools/context.py:931
return {"error": "browser-exec returned no output"}
```

`command not found` writes to stderr and leaves stdout empty, so a **missing daemon** and a
**broken daemon** and a **browserless host** all collapse into the same opaque string. Nothing
in that error tells the agent the daemon is absent, that Chromium exists anyway, or where it is.

### 2.3 The agent is blind to its own platform's naming

- `docker/browser-exec:62` — `CHROMIUM_PATH = os.environ.get("BROWSER_EXEC_CHROMIUM",
  "/usr/local/bin/agent-chromium")`. The `agent-chromium` name is a deliberate pin so
  browser-use can't auto-install its own Chromium.
- `grep -rniE 'agent-chromium|BROWSER_EXEC_CHROMIUM' src/ config/` → **zero hits.**

The name exists only in the Docker/daemon layer. The agent's reasoning layer has never heard of
it. So when the first-class tool fails, the agent's fallback probing is guaranteed to miss the
one browser that is actually installed.

### 2.4 The memory that calcified it

Observed in the memory UI (`ERROR SOLUTION`, `observer`, confidence **0.92**, Phase 4,
156 tokens, **57 accesses**, last used 16/07/2026 18:30):

> **No renderer available; disclose static-only verification limits**
>
> Rendered-browser inspection is unavailable in the current workspace: `chromium`,
> `chromium-browser`, `google-chrome`, `google-chrome-stable`, `firefox`, and `wkhtmltoimage`
> are all not-found, `browser_navigate` returned `browser-exec returned no output`, and only
> Node v22.23.1 is available. For mock[…]

Every factual claim in that memory is *locally* true and *globally* misleading. The agent
diagnosed honestly and behaved correctly given what it could observe — it disclosed the limit
rather than faking verification, which is the graceful-degradation behavior working as
designed. The bug is that its observation was structurally incapable of finding the truth.

### 2.5 Blocker 2 — `file://` is refused before `allowed_domains` is consulted

Found while verifying the blocker-1 fix: with `browser-exec` installed and Chromium working,
navigation to a real mockup **still failed**:

```
{"error": "navigate failed: Navigation to
           file:///home/agent-host/workspace/mockups/theme_operational_clarity.html
           blocked by security policy"}
```

The block is not in `browser-exec` (`grep` finds no URL policy there). It is in browser-use:

```python
# browser_use/browser/watchdogs/security_watchdog.py  (0.12.9)
if parsed.scheme in ['data', 'blob']:
    return True
host = parsed.hostname
if not host:
    return False          # ← file:///... lands here
# If no allowed_domains specified, allow all URLs   ← never reached
```

`file:///path` parses to `hostname=None`, so it is rejected **before** the "no `allowed_domains`
→ allow everything" branch. Only `data:` and `blob:` are exempt from the hostname requirement.
This is therefore **not configurable**: `browser-exec` sets neither `allowed_domains` nor
`block_ip_addresses` (`docker/browser-exec:115–121`), and it would not matter if it did.

Measured consequences (live, 2026-07-17):

| Probe | VM | Container |
|---|---|---|
| `file:///…/mockup.html` | blocked | **blocked** |
| `http://127.0.0.1:<port>/…` | renders (275 KB screenshot + full DOM) | renders (real pixels) |

The renderer was never the problem — only the scheme. And `grep` confirms **no local
HTTP-serving convention exists anywhere in the tools** (`src/tools/research/browser_direct.py`,
`src/tools/context.py`), so agents had no sanctioned way to reach their own files.

**Why this matters more than blocker 1**: it is platform-wide and silent. Any design/frontend
job on *any* backend could complete having never seen its own output.

## 3. How the drift happened

Timeline from the design docs:

- **2026-05-25** — `docs/features/browser_workspace_executor.md` created. Root cause: Chrome 147
  binds CDP to `127.0.0.1` only, so cross-pod CDP was refused. Fix: move the browser controller
  onto the workspace, drive it over SSH.
- **2026-05-27** — validated end-to-end **on a workspace pod**. The doc's "Implemented in this
  pass" list names `docker/Dockerfile.workspace` and nothing else. `grep -niE 'vm|agent-vm|qemu'`
  over that doc returns **no VM references at all**. The VM backend was never in scope.
- **2026-06-11** — `docs/issues/remove_local_browser_fallback.md` implemented. The in-pod
  fallback is deleted for good security reasons (agent pod holds LLM/DB credentials and has no
  NetworkPolicy; a JS engine rendering hostile content does not belong there). **`browser-exec`
  becomes the only browser path.**
- **Net effect**: the fallback removal correctly closed a real security hole on the container
  path — and simultaneously took VM-backed jobs from "maybe worked via the fallback" to
  "provably cannot render", with no error that says so.

Nothing caught it because there is no test that could: the design doc itself notes
*"End-to-end: requires the cluster (no browser in `FilesystemTestBackend`)"*. The regression
guard added in the fallback removal guards against the **local path returning** — not against a
**backend lacking the remote path**.

## 4. Impact

- **Every VM-backed job silently has zero browser capability.** Per `project_loop_vm_override`,
  the RSI loop runs on VMs — so the jobs most likely to need visual verification are exactly the
  ones that cannot do it.
- **Job `4eba7f2f` specifically**: ~11 hand-authored 46–106 KB HTML mockups produced so far
  (32 primary + 5 coverage screens planned), **none visually verified**. "Verification" is
  `grep` for token compliance + an HTTP-200 serving check. For a *design mockup* deliverable,
  layout breakage, overflow, contrast, and focus behavior go entirely unchecked. The job is
  otherwise healthy (clean strategic↔tactical alternation, now at Phase 10) — this is a
  verification-quality gap, not a liveness problem.
- **The failure is invisible from the outside**: the agent reports honestly, the job completes,
  and the handoff carries a politely-worded limitation instead of a bug report.

## 5. Fix — as built (2026-07-17)

### Layer 1a — VM image: install the missing driver + dep. **DONE.**

| File | Change |
|---|---|
| `docker/agent-vm-base/scripts/provision-stage1.sh` | `browser-use>=0.12.9,<0.13.0` alongside the playwright pin, cap rationale carried verbatim; `--ignore-installed` (see below); self-assert `import browser_use` + `agent-chromium --version` in the layer that owns them |
| `docker/agent-vm-base/stage2.pkr.hcl` | `file` provisioner uploads `../browser-exec` and `../assert-browser-stack.sh` |
| `docker/agent-vm-base/scripts/provision-stage2.sh` | installs `browser-exec` → `/usr/local/bin` (0755), then runs the shared gate **as `agent-host`** |

Two decisions worth keeping:

- **`browser-exec` rides stage2, not stage1.** The earlier draft of this doc argued for keeping
  the script with its pip dep in stage1 to avoid drift. That was wrong for the wrong reason: the
  drift is prevented by the shared gate (Layer 1c), not by co-location. `browser-exec` is
  *per-commit source* — the same category as `management-daemon.py`, which stage2 already
  installs — and baking it into stage1 would force the slow Chromium rebuild on every edit to it.
  The heavy stable dep (`browser-use`) stays in stage1 where it belongs.
- **Sourced from `../browser-exec`, not copied into `files/`.** A copy would recreate the
  hand-maintained duplication that caused this bug. Both images now install the byte-identical
  file.

**`--ignore-installed` is required on the VM and not in the container**, and the asymmetry is the
base image. The container starts from a minimal `ubuntu:24.04`; the VM starts from the Ubuntu
*cloud image*, which preinstalls apt-managed Python packages for cloud-init
(`python3-typing-extensions`, `python3-jwt`, …). Those ship no `RECORD` file, so browser-use's
resolver aborts:

```
ERROR: Cannot uninstall typing_extensions 4.10.0, RECORD file not found.
       Hint: The package was installed by debian.
```

Purging them is unsafe (cloud-init depends on them). `--ignore-installed` lets pip install its
own copies under `/usr/local/lib/python3.12/dist-packages`, which precedes
`/usr/lib/python3/dist-packages` on `sys.path` — pip's versions win at import, apt's stay intact
for cloud-init. **The playwright pin must stay named in the same command**, because
`--ignore-installed` reinstalls the whole tree and browser-use depends on playwright; unnamed, it
would silently resolve away from `.playwright-version`. Verified on the live cloud image: yields
`browser-use 0.12.9` + `playwright 1.59.0`.

Chromium, the `agent-chromium` symlink, `fonts-noto-core`, and `PLAYWRIGHT_BROWSERS_PATH` were
**already correct on the VM** — measured, not assumed (§6). No change needed.

### Layer 1b — `file://` rendering via loopback HTTP. **DONE.**

`docker/browser-exec` gains `LocalFileServer`: `navigate` translates a `file://` URL into
`http://127.0.0.1:<ephemeral>/…` served by a threaded static listener, and passes every other
URL through untouched.

Design decisions:

- **Serve over HTTP rather than patch the watchdog.** Monkeypatching a vendored library's
  security control is fragile across upgrades (browser-use is pinned but not frozen). Navigating
  to a real `http://` URL is the supported path and needs no patching.
- **The root is bounded to the workspace, deliberately.** A page served from
  `http://127.0.0.1:<port>` shares an origin with everything else under that port, so the served
  root is exactly what a rendered page could fetch and exfiltrate. Rooting at `/` would let any
  rendered HTML read the whole disk — **strictly worse than `file://`**, which Chrome gives an
  opaque origin per document. The root is therefore `$HOME/workspace` (the agent's own sandbox,
  which by policy holds no internal credentials); files outside it fall back to their own
  directory. Verified: `/tmp/outside.html` is served on a *separate* port rooted at `/tmp`, not `/`.
- **Listeners bind 127.0.0.1 only**, staying inside the same loopback boundary browser-exec
  exists to enforce. Verified with `ss -ltnp`.
- `$HOME` resolution assumes browser-exec runs as the workspace user. It does — the agent spawns
  it over its own SSH session and the orchestrator connects as `agent-host` on every backend.
  Verified `/home/agent-host/workspace` on both the VM and container images.

### Layer 1c — Shared conformance gate (stops the whole class). **DONE.**

`docker/assert-browser-stack.sh`, run by **both** images: `Dockerfile.workspace` (§2d) and
`provision-stage2.sh` (§8). Checks `browser-exec` on PATH, `browser_use` + `playwright`
importable, `agent-chromium` executable (`test -x` also catches a dangling symlink), and that
Chromium actually runs.

**It is one shared file on purpose.** A per-image assertion would not have prevented this bug:
adding a capability to one image and its own private check leaves the twin silently short — which
is exactly what happened. A shared gate fails the build of whichever image lacks the capability.
Adding to the browser stack means editing one file and expecting both builds to break until both
install it. That failure is the feature.

Validated in **both directions on the live VM**: `EXIT=1` naming exactly the two missing pieces
before the fix, `EXIT=0` after. Also installed permanently as `assert-browser-stack`, so "can this
workspace render?" is answerable in five seconds instead of five weeks.

### Layer 2 — Error legibility

`"browser-exec returned no output"` is a dead end for the agent. Make the failure
self-diagnosing:

- Distinguish `command not found` / non-zero exit / empty stdout / non-JSON stdout into
  distinct, actionable errors ("browser-exec is not installed on this workspace — this is an
  infra gap, not a missing capability; Chromium is at `/usr/local/bin/agent-chromium`").
- Consider a `browser-exec health` action and a capability probe at workspace attach, so the
  gap surfaces **once, loudly, to the operator** instead of silently to the agent's reasoning.
- Teach the agent layer the `agent-chromium` name so a fallback probe can't miss the one browser
  that is installed by design.

### Layer 3 — Memory guardrail (the RSI lesson)

This is the deepest finding and the most generalizable:

> A single failed capability probe became a 0.92-confidence, long-lived memory, recalled 57×,
> which suppressed an entire class of actions for the rest of a multi-day job — and was never
> re-tested.

Capability-**absence** beliefs are categorically different from other memories: they are
cheap to re-test, expensive to be wrong about, and self-reinforcing (once you believe you can't
render, you never try again, so you never learn otherwise). Options to consider:

- Lower confidence ceiling and/or short TTL for "capability X is unavailable" memories.
- A re-verify gate before an absence-memory is allowed to suppress an action class (cheap: one
  `command -v`).
- Don't let `ERROR SOLUTION` encode *environmental capability absence* from a single call —
  environment facts should come from a capability probe with a known refresh, not from an
  observer generalizing one tool failure.

Transient infra failures must not calcify into permanent capability beliefs. This is a
first-class RSI-loop failure mode: bad diagnoses compound silently.

## 6. Verification — measured, not inferred

Both formerly-open assertions were run against the live VM
(`agent-vm-4eba7f2f-3e24-4b52-82c3-1929ce8c6771`, via its agent pod's Tailscale sidecar →
`agent-host@100.64.25.224`) **before** any rebuild. Every prediction held:

| Claim | Status |
|---|---|
| VM tree has zero `browser-exec` references; container `COPY`s it | **Confirmed** (grep) |
| `agent-chromium` absent from `src/`+`config/` | **Confirmed** (grep) |
| browser-exec migration was container-only, no VM in scope | **Confirmed** (design doc) |
| Memory content / 0.92 / 57 accesses | **Confirmed** (memory UI) |
| Agent hit `browser-exec: command not found` specifically | **CONFIRMED (measured)** — `browser-exec: MISSING`, `browser_use: MISSING` |
| Chromium **is** present at `/usr/local/bin/agent-chromium`, i.e. "no renderer" is a false negative | **CONFIRMED (measured)** — symlink intact → `/opt/playwright/chromium-1217/chrome-linux64/chrome`, runs: `Google Chrome for Testing 147.0.7727.15` |
| Every conventional name the agent probed is genuinely absent | **CONFIRMED (measured)** — `chromium`, `chromium-browser`, `google-chrome`, `firefox`, `wkhtmltoimage` all absent |
| `playwright`, `fonts-noto-core`, `PLAYWRIGHT_BROWSERS_PATH` already correct on the VM | **CONFIRMED (measured)** — pkg present, font installed, `/etc/environment` set |
| `file://` blocked on the **container** too (blocker 2 is platform-wide) | **CONFIRMED (measured)** on a live workspace pod |
| The fix actually renders | **CONFIRMED (measured)** — the exact previously-failing `file://` URL now returns a 275 KB screenshot + real DOM (*"Hotel Rheinland · Empfang · Bad Orb …"*) |

The headline measurement: **the VM was one pip package and one script away from working, with a
functional Chrome installed the whole time.** Every expensive, slow, fragile part of the image
build had already succeeded.

Also verified on the live VM after the fix: file outside the workspace is rooted at its own
directory (not `/`); a missing file yields `navigate failed: local file not found: …` instead of
an opaque error; `https://example.com` still passes through untouched; all listeners bound to
`127.0.0.1`.

### Not yet verified

- **Neither image has actually been rebuilt.** The stage1/stage2/Dockerfile changes are
  syntax-checked (`bash -n`, `packer fmt`) and every *step* was executed by hand on the live VM,
  but no golden-image build has run end-to-end. The `--ignore-installed` fix in particular was
  validated against the live cloud image, not a fresh Packer build.
- The container image gate (`Dockerfile.workspace` §2d) has not been exercised by a build.
- `browser-exec`'s new `LocalFileServer` has no unit test — the script is not importable as a
  module and the design doc notes end-to-end needs a cluster. Verified live instead.

## 7. Secondary findings from the same job audit

Catalogued here so they aren't lost; each is independent of the browser issue and may graduate
to its own issue doc.

| # | Finding | Evidence / fix location | Severity |
|---|---|---|---|
| 1 | **Scratch litter committed into the deliverable repo.** ~25 `run_command` output dumps (`out.txt`, `result1-4.txt`, `scope2.txt`, `wc.txt`, `grep.txt`, `p24_post.txt`) + abandoned drafts (`critic_iter11_plan.md`, `plan_pre_adapt.md`, `iter-8-handoff-DRAFT.md`) sit at repo root at HEAD, polluting the client handoff and re-inflating every directory listing the agent reads back. | Systemic, not agent-improvised: `git_manager.py:205` (and `:165`) run `git add -A` on **every** commit. Only guard is `DEFAULT_IGNORE_PATTERNS` (`:103–109`) = `*.db, *.log, __pycache__/, .DS_Store, *.pyc` — covers none of it. Fix: stage known deliverable paths, or extend ignores, or route scratch to a gitignored `tmp/`. | Medium |
| 2 | **42.5 KB `plan.md` fully regenerated every strategic phase.** Commits show `todo_2: ADAPT plan.md` at Phases 2, 4, 6, 8, 10. ~85% of the file is static (42-file mockup inventory, reuse map, component families, interaction model, responsive strategy, verification checklist); only the progress table and current batch change. Will repeat ~10 more times through Batches D→M. | Split stable spec from a small mutable status ledger, or edit-in-place instead of regenerate. (Churn ratio inferred from file structure — a measured diff was attempted but the ref pair wouldn't resolve.) | Medium (token) |
| 3 | **Heavy strategic ceremony per batch.** Full REVIEW → ADAPT → PLAN-OR-COMPLETE (3 todos + retrospective + plan rewrite) brackets every ~4-screen tactical batch. Once the plan stabilized at Phase 4, remaining work is largely mechanical screen production. | Allow longer tactical runs / lighter checkpoints when the retrospective isn't finding drift. | Medium |
| 4 | **~19 h low-yield opening.** Created 07-15 08:52; first clean committed deliverable 07-16 03:53. `iter6`–`iter11` / `phase24`/`phase25` scratch artifacts show an early execution with a numbering scheme that didn't match orchestrator phases, reconciled at Strategic Phase 4 (the plan itself admits: *"The earlier plan used design-batch numbers that did not match orchestrator phase numbers"*). Self-correction was genuine and good — but came 19 h in. | Investigate what lets an agent thrash that long before the first deliverable lands. | Medium |
| 5 | **Directory sprawl with near-duplicates.** `output/` vs `outputs/`, `spec/` vs `design_spec/`, `notes/` vs `knowledge/` vs `retros/` vs `archive/` vs `knowledge_iter6_check/`. Correctness edge: `plan.md` requires the final index at `output/ui/README.md`, but `output/ui/` doesn't exist while both `output/` and `outputs/` do. | Canonical dirs per artifact type; final-audit todo must assert exact paths. | Low–Medium |
| 6 | **`kb_search` keyword-stuffing.** Queries like *"Hotel Rheinland UI mockup failed approaches blockers browser inspection phase 7 reservation search operational alert partial REW…"* cram 15+ terms into one hybrid query, diluting the dense embedding and firing sparse matches on incidental terms. | Prompt for focused single-intent queries, or split into 2–3. | Low |
| 7 | **Mega-todos make "done" a weak signal.** Each todo is a 150–250-word spec cramming ~10 requirements; `todo_complete` is a coarse pass/fail with no per-requirement check. Compounds the browser issue: no visual verification **and** no granular verification = "complete" is thinly evidenced. | Per-todo acceptance checklists with explicit per-item verification. | Medium |
| 8 | **No budget/scope gate on a runaway-shaped job.** ~28 h and 4187+ audit entries bought ~20% of the checklist; stop-condition is all-or-nothing at 42 mockups + 7 spec docs. Nothing caps it. | Budget ceiling, or staged completion so a vertical-slice-complete handoff exists at ~15 screens instead of nothing until 42. | Medium |
| 9 | **Possible git exit-128 on a phase commit.** Shell tail showed `__DONE_…__ 128` after the Phase 10 REVIEW commit, though a commit hash (`bcb77293`) printed. Ambiguous — may be benign. | Low confidence; worth a glance only. | Low |
| 10 | **Sudo gate disabled in favour of a VM gate that isn't installed.** `orchestrator/main.py:2128–2129` sets `config_override["shell"]["sudo_action"] = "allow"` for VM jobs, commented *"VM has its own sudo gate — allow sudo through"*. On the live VM the gate is **absent**: `systemctl is-active sudo-gated.socket` → `inactive`, `/etc/sudo-gate/config.yaml` → absent, and `sudo -n true` succeeds unprompted. `provision-stage2.sh:171` installs the gate only `if [ -s /tmp/sudo_gate.so ] && [ -s /tmp/sudo-gated ]` — CI ships empty placeholders when the Go/C binaries aren't built, so the section silently skips. Net: agent-side gating is turned **off** deferring to a VM-side gate that does not exist → unrestricted passwordless sudo with no approval path. | Found incidentally while probing whether sudo would hang on the live VM. Deserves its own issue. Fix candidates: fail the build (or the VM dispatch) when the gate is expected but absent; or have the orchestrator condition `sudo_action=allow` on a verified gate rather than on `backend == "vm"`. | **High (security)** |
| 11 | **`git add -A` is explicitly forbidden in this repo** (`MEMORY.md`, DB roadmap note) yet `git_manager.py:165,205` does exactly that on every agent commit — the mechanism behind finding #1. Worth reconciling the guardrail with the code. | `src/managers/git_manager.py:165,205` | Medium |

## 8. Open questions

**Answered by this pass:**

- ~~Are there other VM/container capability drifts of the same shape?~~ → Partially addressed.
  `assert-browser-stack.sh` closes the class **for the browser stack**. The general question
  stands: the images are twins across *many* axes (node, git, datasource clients, rclone) with no
  conformance assertion beyond the browser. The gate is a pattern to extend, not a finished job.
- ~~Does the container backend still render correctly today?~~ → **Yes** — measured: `http://`
  renders real pixels on a live workspace pod, `chromium_sandbox=False` is shipped. But it hits
  blocker 2 identically, so it could not render *local files* either.

**Still open:**

- **Neither image has been rebuilt.** Highest-priority follow-up: a real stage1 build is the one
  thing that would validate `--ignore-installed` against a fresh cloud image rather than a
  5-week-old running VM. Watch for the known-fragile cold-import (`waiting_golden` park).
- **This fix does not reach job `4eba7f2f` by itself.** A golden-image rebuild affects only new
  VMs, and the live VM has now been hand-patched — but the **0.92-confidence / 57-access memory
  will keep suppressing re-probing regardless**. Rescuing the remaining ~33 mockups needs the
  memory invalidated *and* an explicit steer, not just working infra. Infra fix ≠ job fix.
- **The payoff is model-dependent.** `gpt-5.6-sol` (this job) resolves to family `gpt-5.6`,
  `multimodal: true` — so screenshots reach the model and the fix delivers genuine visual
  verification. But `minimax` (M2.7) is `multimodal: false` while `minimax-m3` is `true`; VM-backed
  RSI-loop jobs on M2.7 would get DOM inspection only. Worth confirming which the loop pins
  before calling this "fixed for the loop".
- **Image-token cost.** At `gpt-5.6`'s `openai_patches` budget (~3000 tok/image), screenshotting
  33 remaining mockups × several states each is real spend on a job already at 1066 requests.
  Argues for screenshotting key states, not every state.
- Should visual verification be a **hard gate** for design-type jobs? Today a design job can
  complete having never rendered its own output, disclosing the limitation politely. Arguably
  the expert config should refuse to mark such a job `goal_achieved`. Now actually implementable —
  before this fix the gate would have been unsatisfiable on every backend.
- How many other high-confidence `ERROR SOLUTION` memories encode environment absences that were
  transient? Worth an audit of the memory store for `capability unavailable`-shaped entries.
- Should the agent layer learn the `agent-chromium` name (Layer 2), so a fallback probe can't miss
  the one browser installed by design? Cheap, and it would have shortened this investigation from
  weeks to minutes.
