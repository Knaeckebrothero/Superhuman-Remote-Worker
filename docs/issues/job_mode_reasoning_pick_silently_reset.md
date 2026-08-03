---
tags:
  - issue
  - cockpit
  - jobs
  - llm
related:
  - "[[tool_configuration_defects_and_fix_roadmap]]"
  - "[[session_create_tool_toggles_cannot_enable_a_group]]"
  - "[[tool_configuration_deferred_findings]]"
---

# Job mode has the session reasoning-reset defect too — an involuntary expert prefill wipes a Strategic/Tactical pick with no feedback

**Status:** OPEN, filed 2026-08-03. The **session-mode** instance of this defect
is fixed (`bb3e061e`, `369390df`, on `develop`, unpushed) and written up as
defect 4 of [[tool_configuration_defects_and_fix_roadmap]] / Part 3 of
[[session_create_tool_toggles_cannot_enable_a_group]]. This is a **second
instance of the same defect in the job-create surface**, reasoned from the code
that the session fix was derived from. It is not a hypothetical, and it is not
yet reproduced in a browser.
**Severity:** low-medium — no data loss, but the user believes a reasoning level
(and a temperature, and a multimodal flag) is set that the job will not run with.
Same shape as the session case, wider fields.
**Component:** `cockpit/src/app/views/agent-settings/advanced-accordion.component.ts`
(`prefillFromConfig`, ~1256-1267);
`cockpit/src/app/views/create/job-create.component.ts` (the `artifacts.config()`
effect at ~1128 and `fetchExpertDetail` at ~1297);
`cockpit/src/app/views/agent-settings/agent-settings.component.ts:414-417` (the
cascade).

## Why this is a probable second instance, not a guess

The session defect turned out **not** to be "the user changed model or expert".
The trigger is **involuntary**: a deferred expert resolution lands after the user
has already picked a level, calls `prefillFromConfig()`, and the sink wipes the
pick unconditionally because the incoming shape carries no
`llm.reasoning_level`. No model change and no expert click is required. That is
the finding the session fix was built on, reproduced end to end in a test against
a real `ModelGroupComponent`.

Job mode has the same three parts:

**1. The same unconditional sink.** `advanced-accordion.component.ts`:

```ts
    this.strategicReasoning.set((strat?.['reasoning_level'] as string) ?? (llm?.['reasoning_level'] as string) ?? null);   // :1261
    this.strategicTemperature.set(…);                                                                                      // :1262
    this.strategicMultimodal.set(…);                                                                                       // :1263
    this.tacticalReasoning.set((tact?.['reasoning_level'] as string) ?? (llm?.['reasoning_level'] as string) ?? null);      // :1264
    this.tacticalTemperature.set(…);                                                                                       // :1265
    this.tacticalMultimodal.set(…);                                                                                         // :1266
```

Every line ends in `?? null`. A `{llm: {}}` shape — which is what an expert with
no reasoning declaration delivers — therefore collapses to `set(null)` on all
six signals. Note the blast radius is **wider than session mode**: session mode
wipes one field, job mode wipes six across two phases.

**2. The same cascade reaching it involuntarily.**
`AgentSettingsComponent.prefillFromConfig` fans out to all three child groups
(`modelGroup`, `toolsGroup`, `advancedAccordion`) at `:415-417`, and
`job-create.component.ts` calls it from two places that are not user gestures on
the reasoning control:

- `:1128` — an `effect()` on `this.artifacts.config()`, which fires whenever the
  artifact form-state signal changes.
- `:1297` — `fetchExpertDetail()`'s subscription, i.e. an asynchronous HTTP
  response.

Either can land after the user has picked a level.

**3. No feedback.** The session fix added a `reasoningResetNotice` signal to
`model-group.component.ts`, raised whenever a non-null pick is cleared and
dismissed by the next deliberate reasoning interaction. `advanced-accordion` has
no equivalent. The select simply snaps back to the family default, which is
visually indistinguishable from never having been touched.

## What must not be changed

**Do not make the reset conditional on the new family supporting the level.**
This is settled and the reasoning is in the roadmap: reasoning vocabularies are
per-family and do not translate (`gemma` is a binary toggle, `gpt-5.6` is an
effort enum), carrying a stale sampler across families is what produced hard
400s, and enum→enum is already handled server-side by `_clamp_reasoning_level`
walking the effort ladder. The reset is correct and load-bearing. **The whole
defect is the absence of feedback.**

## Verification owed before fixing

The session instance was diagnosed by reproducing the race in a test, and the
first hypothesis was wrong. Do the same here rather than assuming:

1. **Confirm a real trigger.** Pick a Strategic reasoning level on the job-create
   form, then cause one of the two call sites to fire — selecting a project whose
   scoped default expert differs is the path that mattered in session mode
   (minutes later, not a page-load race) — and observe whether the pick
   disappears. If neither call site fires after a pick in practice, this is
   latent rather than live, and the ticket should say so.
2. **Check whether the job payload echoes the loss.** Job create submits
   `agentSettings.getOverrides()`; if a wiped signal is simply absent from the
   payload, the loss is invisible until the job runs with the family default.
3. **Then port the fix, not the diff.** The session fix lives in
   `model-group.component.ts` and covers one signal on one surface. The job
   version needs a notice that can describe *which* of two phases lost a value,
   and it needs a rendered (DOM) test — the session fix's review found that all
   six of its first tests were signal assertions that could not see whether the
   notice or its transloco key rendered at all.
