# Systematic Debugging Skill — Research Report

Evidence base for the `systematic-debugging` skill (Tier-1 — **scoped to code work**,
not universal). `deep-research` run (105 agents · 23 sources fetched · 108 claims →
25 verified → **24 confirmed / 1 killed**), 2026-06-24. Prompt:
[systematic-debugging.md](./systematic-debugging.md). Skill:
`config/skills/systematic-debugging/SKILL.md`.

## Executive synthesis / the crux

A genuinely well-evidenced systematic-debugging procedure exists and is convergent
across primary sources — but it is overwhelmingly **CODE-shaped**, and the transfer
question does NOT support shipping one universal skill. The canonical loop: reproduce
(small repeatable case) → hypothesize ("X because Y") → instrument/gather evidence
BEFORE changing → isolate the root (trace bad value to source) → fix root not symptom,
one change at a time → re-run the check to verify.

**Transfer verdict (objective 3):** every battle-tested debugging procedure is
code/test-centric (triggers on "test failures," terminates on "run the test suite"),
and the candidate universal bridge — formal RCA (5 Whys / Ishikawa) — fails its own
evidence test (Peerally et al., BMJ Quality & Safety 2017: RCA "has consistently failed
to deliver benefits," promotes a "flawed reductionist" single-root view; a systematic
review found only ~9% of studies showed improvement). → **Ship scoped to code work;
defer the knowledge-work variant** — no real example of agent non-code debugging was
found (open-Q #2).

## Findings (14 after synthesis)

1. **Canonical loop is settled & convergent** — HIGH. reproduce→hypothesize→instrument→isolate→fix-root→verify. (MIT 6.031, Zeller, obra/superpowers, doraemonkeys, ChrisWiles, Cursor Debug Mode.)
2. **Reproduce-first is non-negotiable** — HIGH. Small repeatable case; you re-run it many times; can't confirm a fix without it. If not reproducible, gather data, don't guess.
3. **Hypothesis-first, evidence-before-fix** — HIGH. State "X because Y" before touching code; instrument to test it. (Tension: MIT lists MULTIPLE hypotheses; obra/ChrisWiles test ONE at a time; Cursor reconciles — generate several, test singly.)
4. **Instrument at component boundaries; tag logs with the hypothesis number** — HIGH. `[DEBUG H1]` log tags → a diagnosis table (Hn confirmed/ruled-out + evidence). Strongest reusable scaffold.
5. **Fix the root, not the symptom** — HIGH. Trace the bad value to its origin, fix at source; symptom-patching "masks the bug without removing it."
6. **One change at a time / minimal fix** — HIGH. One variable per test; on failure form a NEW hypothesis, don't stack fixes. (Delta-debugging = the formal one-variable analogue.)
7. **3-strikes circuit breaker** — HIGH. After 3 failed fixes, STOP — wrong-architecture signal, not a failed hypothesis. **SRW adaptation:** record the doubt + set `goal_achieved=false` (orchestrator decides), not "ask your human."
8. **Verify by re-running the check** — HIGH. Confirm the bug is gone AND nothing new broke; remove instrumentation after. **Hands off to `verify-before-done`** (obra names the same handoff to verification-before-completion).
9. **Delta Debugging** — HIGH. Formal minimize-repro / isolate-difference (896 lines→1; 95 actions→3). CAVEAT: minimizes the failing INPUT, not the faulty STATEMENT → too code-input-specific for L2; push to L3 (developer scope).
10. **TRANSFER QUESTION (the crux)** — HIGH. Code-specific; RCA bridge unproven → scope to code work; a lighter variant for knowledge work only if honestly labeled as resting on general RCA, not agent practice.
11. **Enforcement: model-invoked in tactical phases** — MEDIUM. Proactive complement to SRW's reactive fingerprint loop-detection; NOT a hard gate (false triggers on healthy edits). (doraemonkeys is human-gated / never-auto — don't copy that.)
12. **Trigger description** — MEDIUM. Fire on failure / unexpected behaviour, not only "test failures." Avoid ChrisWiles' ALL-CAPS "NO FIXES" (violates our rubric); prefer obra's "before proposing fixes."
13. **Model-variance: one body robust** — MEDIUM. Reasoning discipline, not provider syntax; the same text works cross-harness.
14. **Diagnosis scaffold** — HIGH. Fill-in hypothesis/evidence log (Expected · Observed · Hypotheses · Evidence · Verdict · Root cause · Fix · Verification). Written to `notes/` → also resets loop-detection.

## Refuted (do NOT use)
- "Evidence-driven debugging produces a small 2–3 line fix vs hundreds of speculative lines" (Cursor) — **1-2**.

## Caveats
- Procedural core is excellent (MIT 6.031, Zeller, four real SKILL.md files). **Weakest leg = the transfer/RCA evidence** (healthcare-safety lit generalized to agent debugging; two claims carried 2-1 votes with overreach). "RCA is not a proven universal bridge" is well-supported; "RCA is useless for diagnosis" would overstate.
- **Human-in-the-loop mismatch:** every in-the-wild skill assumes a human reproduces / confirms / receives the escalation. Three SRW adaptations are load-bearing + unsourced: the agent runs the repro itself; 3-strikes → record + `goal_achieved=false`; verify hands off to `verify-before-done`.
- **Scope honesty:** the knowledge-work variant is a RECOMMENDATION grounded in general RCA + scientific-method reasoning, NOT demonstrated agent practice — no such artifact was found (the key objective-3 finding).

## Open questions
1. Does hypothesis-first measurably reduce thrashing in autonomous agents? (MAST frames WHY; no source measured the procedure's effect on agent failure rates; a dubious "95% vs 40%" stat was flagged and excluded.)
2. **Any real example of an agent debugging a NON-code failure? None found** — decides whether the knowledge-work variant is worth building or should fold into `research-guide` / `verify-before-done`.
3. Model-invoked only, or also soft phase-injected when the last `run_command` exited non-zero? (needs a "last tool failed" signal.)
4. How does the 3-strikes counter interact with SRW's fingerprint loop-detection (does a rewind reset the fix-attempt counter, letting it thrash past 3)?

## Sources (primary)
obra/superpowers systematic-debugging SKILL.md · ChrisWiles systematic-debugging SKILL.md · doraemonkeys claude-code-debug-mode · Cursor Debug Mode · MIT 6.031 (scientific-method debugging) · Zeller & Hildebrandt delta-debugging (IEEE TSE 2002 + 2025 retrospective; Columbia PDF) · Peerally et al., BMJ Quality & Safety 2017 (RCA critique) · PMC systematic review of RCA · MAST (arXiv 2503.13657) · Agans "9 Indispensable Rules" · Allspaw blameless postmortems.
