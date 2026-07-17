# VM workspaces ship Chromium but no `browser-exec` — agents conclude "no renderer" and memory calcifies it

> **Status**: Diagnosed 2026-07-17, **UNFIXED**. Root cause confirmed by code inspection
> (§2); two runtime assertions still owed (§6).
> **Found via**: job `4eba7f2f-3e24-4b52-82c3-1929ce8c6771` — "Design the UI theme and
> complete mockup suite for Hotel Rheinland ERP", main dev cluster. A 40+-screen HTML
> design job that has visually verified **zero** of its mockups.
> **Severity**: High for VM-backed visual/frontend work. The RSI loop runs on VMs.
> **Related**: `docs/features/browser_workspace_executor.md`,
> `docs/issues/remove_local_browser_fallback.md`, `project_vm_golden_image`.

## TL;DR

The `browser-exec` migration wired the **container** workspace image only. The **VM** golden
image installs Playwright Chromium and the `agent-chromium` symlink — but never `browser-use`
or the `browser-exec` script itself. Since the in-pod fallback was removed (2026-06-11),
`browser-exec` is the **only** browser path. Therefore **VM-backed jobs have no renderer at
all**, and fail silently.

It gets worse than a missing dependency. The agent cannot distinguish *"the daemon isn't
installed"* from *"no browser exists on this machine"*. It falls back to probing conventional
binary names (`chromium`, `google-chrome`, `firefox`, `wkhtmltoimage`) — **none of which match
the platform's deliberately non-standard `agent-chromium`** — concludes **"No renderer
available"**, and the memory observer promotes that conclusion to a **0.92-confidence memory
that has been recalled 57 times**. A transient, backend-specific infra gap calcified into a
permanent capability belief that now steers every verification step of the job into
source-only `grep` checks.

Chromium is almost certainly sitting unused at `/usr/local/bin/agent-chromium` on that VM the
entire time.

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

## 5. Fix — three layers

### Layer 1 — Infra (unblocks rendering). The actual bug.

Mirror the container's §2c stanza into the VM golden image. Exactly two additions:

1. Add `"browser-use>=0.12.9,<0.13.0"` to the pip install at
   `provision-stage1.sh:228`. **Carry the version cap and its comment verbatim** — per
   `Dockerfile.workspace:184–191`, browser-use 0.13 (2026-06-08) rewrote the browser layer to
   pure CDP, dropped Playwright, no longer finds Chromium under `PLAYWRIGHT_BROWSERS_PATH`, and
   falls back to `uvx playwright install` which fails on the image. Loosening this cap breaks
   every `browser_*` tool. Do not let the VM copy drift on the cap.
2. Install `docker/browser-exec` → `/usr/local/bin/browser-exec` + `chmod +x` (Packer `file`
   provisioner; the script already lives at `docker/browser-exec`).

Chromium, the `agent-chromium` symlink, `fonts-noto-core`, and `PLAYWRIGHT_BROWSERS_PATH` are
**already correct on the VM** — no change needed.

Implementation notes:
- Both additions are stage1 inputs → triggers the slow stage1 rebuild (Chromium is "the slowest
  single step"). The `browser-exec` script alone is small enough to ride stage2, but splitting
  the daemon from its own pip dep invites exactly the drift this issue documents. Prefer keeping
  them together in stage1.
- The VM comment at `provision-stage1.sh:221–222` notes the symlink is **build-time only** —
  runtime upgrades dangle it. The fix belongs in the image, not in cloud-init.
- **Add a build-time assertion** to both images so this drift cannot recur silently, e.g.
  `command -v browser-exec && command -v agent-chromium && python3 -c 'import browser_use'`.

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

## 6. Verification owed

Confidence markers for the claims above:

| Claim | Status |
|---|---|
| VM tree has zero `browser-exec` references; container `COPY`s it | **Confirmed** (grep) |
| VM installs Chromium + `agent-chromium`; lacks `browser-use` | **Confirmed** (`provision-stage1.sh:228–237`) |
| `agent-chromium` absent from `src/`+`config/` | **Confirmed** (grep) |
| browser-exec migration was container-only, no VM in scope | **Confirmed** (design doc) |
| Job runs on a VM workspace | **Confirmed** (shell prompt) |
| Memory content / 0.92 / 57 accesses | **Confirmed** (memory UI) |
| Agent hit `browser-exec: command not found` specifically | **Inferred (high)** — assert with `which browser-exec` on the VM |
| Chromium **is** present at `/usr/local/bin/agent-chromium` on that VM, i.e. "no renderer" is a false negative | **Inferred (high)** — assert with `which agent-chromium` on the VM |

Both open assertions are one SSH away on the live VM
(`agent-vm-4eba7f2f-3e24-4b52-82c3-1929ce8c6771`). Run them **before** the golden-image rebuild —
if `agent-chromium` is somehow absent too, the fix spec in §5 changes.

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

## 8. Open questions

- Are there other VM/container capability drifts of the same shape? The two images are
  maintained as parallel twins with no assertion that they agree. A conformance test ("both
  backends satisfy the same capability probe") would catch the whole class.
- Does the **container** backend still render correctly today? The migration was validated
  2026-05-27; `browser_workspace_executor.md` notes a sandbox fix that "needs a workspace image
  rebuild to ship" (`chromium_sandbox=False` is present in `docker/browser-exec:119`, so it
  appears shipped — worth one live assertion).
- Should visual verification be a **hard gate** for design-type jobs? Today a design job can
  complete having never rendered its own output, disclosing the limitation politely. Arguably
  the expert config should refuse to mark such a job `goal_achieved`.
- How many other high-confidence `ERROR SOLUTION` memories encode environment absences that were
  transient? Worth an audit of the memory store for `capability unavailable`-shaped entries.
