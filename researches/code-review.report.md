# Skill Research Report — `code-review` (giving)

> Evidence base for authoring `config/skills/code-review/SKILL.md`. Synthesized
> 2026-06-25 from three scoped research passes (vendor/competitor
> implementations · the empirical effectiveness canon · the transfer question +
> LLM-reviewer failure modes). Prompt: [`code-review.md`](./code-review.md).
> `[FACT]` = sourced; `[REC]` = synthesis. Most code-review evidence is
> code-specific — domain-coverage caveats are flagged inline.

## Key numbers (load-bearing)

- **Review size/rate ceiling:** defect-finding "is almost zero after 400" LOC; review **<200–400 LOC** at **<300–500 LOC/hour**, **≤60–90 min/sitting**; within those limits a review yields **70–90%** of present defects. `[FACT]` SmartBear/Cisco (2500 reviews, 3.2M LOC) — *single-vendor, non-peer-reviewed; cite as "widely-repeated vendor study."*
- **Real-world change is small:** Google median change = **24 LOC / 1 file**; <25% of changes have >1 reviewer; median review latency <4h. `[FACT]` Sadowski et al. ICSE-SEIP 2018.
- **Review is oversold as a bug-catcher:** only **~14–15%** of review comments are about defects (and those skew shallow); the realized value is *understanding / knowledge transfer / maintainability*. "Relying on code review … for quality assurance may be fraught." `[FACT]` Bacchelli & Bird ICSE 2013; Czerwonka et al. "Code Reviews Do Not Find Bugs" ICSE-SEIP 2015.
- **Understanding the change is THE bottleneck:** 91% say unfamiliar files take longer; only familiar-file reviewers give deep, subtle-defect feedback. `[FACT]` Bacchelli & Bird.
- **Perspective/role-based reading > generic checklist:** scenario/perspective reading improved fault detection ~**35%** over ad-hoc, and **checklist reading was no better than ad-hoc**. `[FACT, direction solid; exact % is secondary]` Porter, Votta & Basili, IEEE TSE 21(6) 1995; Basili et al. PBR.
- **MAST verification cluster = 23.5%** of multi-agent failures: FM-3.1 Premature Termination **6.2%**, FM-3.2 No/Incomplete Verification **8.2%**, **FM-3.3 Incorrect Verification 9.1%** (the single largest sub-mode = a confidently-wrong verdict, false-approve OR false-reject). `[FACT]` arXiv 2503.13657, 1,642 traces, κ=0.88.
- **LLM-judge biases (numbers):** position bias (Claude-v1 favored first 75%); verbosity attack fooled GPT-3.5/Claude-v1 **91.3%**; self-preference +10% (GPT-4) / +25% (Claude-v1); sycophancy consistent across 5 SOTA models and **amplified by RLHF**. `[FACT]` Zheng et al. 2306.05685; Panickssery NeurIPS 2024; Sharma et al. 2310.13548.

## A. Executive synthesis

The strongest review procedure for an autonomous agent is **small-scope, understanding-first, perspective-driven, executed, and evidence-gated**, ending in a defensible verdict. The empirical canon is blunt about two things: review only finds bugs reliably when the change is *small and slow* (defect-finding collapses past ~400 LOC and ~60 min), and the binding constraint on quality is the reviewer's *understanding* of the change, not effort or checklist length `[FACT]`. So step one of any review is bounding it and comprehending it — not hunting for nits.

What measurably improves detection is **perspective/role-based reading** (read as the attacker, the caller, the maintainer), which beat ad-hoc by ~35% in controlled experiments — while a generic checklist did *no better than ad-hoc* `[FACT]`. This is the single most important authoring nuance: frame the rubric as *perspectives to adopt*, not boxes to tick. The convergent rubric across every serious AI-reviewer product (CodeRabbit, Greptile, Copilot, Codacy) and the superpowers skill is the same five axes — **Correctness · Security · Performance · Tests · Maintainability** (+ Architecture) `[FACT]`.

For an agent whose verdict can *gate a job*, the dominant risks are **sycophancy / over-approval** (a false "approve" that ships the defect) and **confidently-wrong verdicts** (MAST FM-3.3, the largest verification sub-mode at 9.1%), compounded by **hallucinated false-positive findings** that erode trust `[FACT]`. The two mitigations with the hardest evidence both convert a vibe into a check: **"run it, don't just read it"** (execute the tests / reproduce the claim — also how false positives get filtered) and **require explicit evidence per finding against a fixed rubric** `[FACT]`. Severity triage is universal across products (Critical/Major/Minor/Nit ≈ P0–P2 ≈ superpowers' three tiers), and the finding unit everyone converges on is **location + what's wrong + why it matters + how to fix** `[FACT]`.

On transfer: critique *is* a domain-general discipline as a *frame* — academic peer review evaluates the same axes (validity/method, significance, clarity), and Toulmin (claim → evidence → warrant) plus steelman-first apply to any argument `[FACT]`. But the hard, defensible part of a *code* verdict is code-specific (run the tests, reproduce, read the diff), and peer review's own reliability is weakly validated — so universality justifies shipping *one body* available to all experts, **not** dropping the execution steps `[REC]`.

## B. Recommended SKILL.md body procedure

Six steps (mirrors superpowers `code-reviewer.md`, hardened with the empirical + LLM-judge findings):

1. **Scope it** — get exactly what changed and what was asked (`git_diff`/`git_show` vs. base for code; the submitted draft/findings otherwise); read the task/plan it must satisfy. Review the *submitted change*, not the whole repo or a redesign. Slice a big diff (defect-finding falls off past ~400 LOC).
2. **Understand before you judge** — read the change in context (callers, surrounding code, intent). Understanding is the bottleneck; steelman the author's intent first.
3. **Read against the rubric — by perspective, not a checklist** — Correctness (as the caller) · Security (as an attacker) · Performance (as load) · Tests (as a future maintainer) · Maintainability. Map the same axes to non-code artifacts (claims-vs-evidence, unstated assumptions, structure, are-conclusions-tested).
4. **Exercise it — run, don't just read** — check out the change, run the tests, reproduce a suspected bug before flagging it; mark each finding *verified* (ran it) vs *suspected* (read-only). The top defense against hallucinated findings + false approve; shared spine with `verify-before-done`.
5. **File findings: severity · location · evidence · fix** — one row per finding, triaged; lead with blocking, don't bury them under nits; each names where/what/evidence/fix.
6. **Render a verdict — with evidence** — approve / approve-with-nits / request-changes, backed by findings + what you ran. SRW phase split: gather evidence **tactically**, render the verdict **strategically** (verdict tools are strategic-only). Don't soften a real blocker or manufacture one; when genuinely unsure, say so and fail safe.

## C. Embedded scaffold

A findings table — `Severity · Location · Issue · Evidence · Suggested fix` — plus the verdict line. Severity vocabulary: **Blocking · Major · Minor · Nit** (maps to P0–P2 / Critical–Info / superpowers Critical-Important-Minor). A long per-domain rubric (full security checklist, language-specific smells) belongs in an **L3 reference file**, not the body. `[REC]`

## D. Quality bar

**Good review:** scoped to the change, demonstrably understood, read by perspective, **exercised** (tests run / claim reproduced), findings carry location + evidence + fix + severity, verdict tied to that evidence. **Rubber-stamp:** "LGTM" with nothing checked or run. **Nit-flood:** a wall of style nits, no severity, the real bug missed. The discriminator is *evidence per finding* and *something was actually run*.

## E. Anti-patterns (each with the fix)

- Rubber-stamp ("LGTM" unread) → name what you checked and ran.
- Sycophancy / cave when challenged → hold the finding while its evidence holds; change it only when the evidence changes.
- Nit-flood / bikeshed → triage by severity; lead with blocking.
- Hallucinated finding → reproduce it or mark *suspected*; don't assert a bug you can't show.
- No evidence → cite the line + the failing case.
- Scope creep / redesign → review what was submitted; raise bigger ideas separately, non-blocking.
- Missing the high-severity issue → spend the budget on correctness/security before trivia.

## F. Enforcement & scope recommendation

**Scope:** ship as **one universal body** — the critique frame transfers and the body degrades gracefully to non-code artifacts in prose `[REC, transfer evidence]`.

**Enforcement — recommend MODEL-INVOKED (catalog), with an OPTIONAL `critic` `phase:tactical` binding.**
- Model-invoked is the right default: consistent with `brainstorming`/`systematic-debugging`, zero binding risk, available to any expert reviewing any artifact, and review is *episodic* (you review when there's something to review) — exactly the model-invoked profile, unlike `verify-before-done` which fires at every completion.
- The `critic` is the natural home (reviewing *is* its job; it already ships `git_diff`/`git_show`/`run_command` + `output/reviews/`), so a `phase:tactical` binding on the critic is well-motivated as a *follow-up*. **Cost to weigh:** the critic has no `instruction_files` block today (it inherits all four defaults bindings); binding code-review to it requires an `instruction_files` block that **replaces** the inherited list (deep_merge replaces arrays), so all four defaults entries must be re-listed and will then **drift** from defaults. Minor but real — hence "optional follow-up," not part of the first build.
- **Not** a hard gate: gating *reading* a skill (tier B) doesn't make the review good; the behavior that matters (running the tests) is what `verify-before-done`/execution covers.

## G. Trigger-description draft

**Primary:** *"Use when reviewing someone else's work — a code diff or PR, a research finding, a draft, an analysis — to evaluate it against explicit criteria (correctness, security, performance, tests, maintainability) and return structured, evidence-backed findings with severity and a clear verdict. For reviewing submitted work, not checking your own before completion (that's verify-before-done)."*

Alternates: (a) "Use to review a change or artifact you've been handed and produce located, severity-tagged, evidence-backed findings plus an approve / request-changes verdict." (b) "Use when acting as a reviewer or quality gate on someone else's diff, proposal, or draft — read it in context, run/reproduce it, and file actionable findings by severity." The description must *not* misfire on the author's own self-check (verify-before-done) — the "someone else's work" framing is load-bearing.

## H. Model-variance note

One body is robust `[REC]`. No evidence that a review *procedure* needs per-family wording. The relevant model variance is *behavioral* (sycophancy/position/verbosity/self-preference biases vary by model — Zheng et al.), which the body counters structurally for every model (evidence-per-finding, run-it, fail-safe verdict) rather than via per-family text. SRW already varies the *persona/strategic* prompt per family; the skill rides on top unchanged.

## I. Real examples from the wild

- **superpowers `code-reviewer.md`** (the best adaptable artifact): DO "Categorize by actual severity, Be specific (file:line), Explain WHY each issue matters"; DON'T "Say 'looks good' without checking, Mark nitpicks as Critical, Give feedback on code you didn't actually read." Severity: Critical (Must Fix) / Important (Should Fix) / Minor (Nice to Have). Output: Strengths → Issues (per severity: File:line, What's wrong, Why it matters, How to fix) → Assessment ("Ready to merge? Yes|No|With fixes" + 1–2 line reasoning). <https://github.com/obra/superpowers/blob/main/skills/requesting-code-review/code-reviewer.md>
- **CodeRabbit** categories + 5-level severity (🔴 Critical / 🟠 Major / 🟡 Minor / 🔵 Trivial / ⚪ Info): Security · Stability · Data Integrity · Functional Correctness · Performance · Maintainability. <https://docs.coderabbit.ai/guides/code-review-overview>
- **Greptile** P0/P1/P2 with a per-comment confidence score + 0–5 PR rating — the "confidence" idea maps to our verified-vs-suspected distinction. <https://www.greptile.com/docs/code-review/first-pr-review>
- **Conventional Comments**: `<label> [decoration]: <subject>` with `(blocking)`/`(non-blocking)`/`(if-minor)` — e.g. "suggestion (security): …". <https://conventionalcomments.org/>; Google "Nit:" non-blocking convention <https://google.github.io/eng-practices/review/reviewer/looking-for.html>.

## J. Open questions / weak spots

- **Transfer is frame-only-proven.** No source measures an agent doing *non-code* review with this procedure; the universal claim rests on peer-review/editorial/critical-thinking frameworks (prescriptive, and peer review's own reliability is contested). The body should carry the universal frame but keep the code-execution steps; validate non-code review on a real SRW job before claiming it. `[flagged]`
- **SmartBear LOC/rate numbers** are the most-quoted but weakest-sourced (single vendor, non-peer-reviewed); peer-reviewed work corroborates the *direction* (small diffs review better) but not the exact figures.
- **Checklist vs. perspective:** SmartBear touts checklists; the peer-reviewed result says generic checklists ≈ ad-hoc and *perspective* reading is what helps. The skill follows the peer-reviewed result.
- **Verdict calibration** (when is "unsure" → request-changes vs approve-with-nits) is a judgment the evidence can't fully settle; the body's "fail safe + state the reason" is a `[REC]`.

## K. Sources

Implementations: obra/superpowers `requesting-code-review` + `code-reviewer.md` + `receiving-code-review` (PRIMARY, the modeling target); CodeRabbit / Greptile / GitHub Copilot / Codacy / Graphite docs (vendor rubrics + severity scales); OpenAI Codex GitHub review docs; Cline docs; Conventional Comments; Google eng-practices; yegor256 "Testing in Code Review."
Empirical: SmartBear "11 Best Practices"/Cisco study (vendor, size/rate numbers); Bacchelli & Bird ICSE 2013 (peer-reviewed, "understanding is the bottleneck", 14% defects); Sadowski et al. ICSE-SEIP 2018 Google (median 24 LOC, single-reviewer); Czerwonka et al. ICSE-SEIP 2015 "Code Reviews Do Not Find Bugs"; Porter/Votta/Basili IEEE TSE 1995 + Basili PBR (perspective > checklist ≈ ad-hoc); Fagan IBM Sys J 1976 (~82% inspection efficiency).
Transfer + LLM-judge: peer-review rubric studies (Li 2025; PMC11000557); Toulmin (Writing Commons); steelman/charity (Reason, LessWrong — contested); Zheng et al. 2306.05685 (LLM-as-judge biases); Panickssery NeurIPS 2024 (self-preference); Sharma et al. 2310.13548 (sycophancy, RLHF-amplified); LLM code-review FP studies (2505.16339, 2603.00539); reference-guided / jury mitigations (2408.09235, 2404.18796); MAST arXiv 2503.13657 (verification cluster numbers, confirmed).
