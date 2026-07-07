---
tags:
  - issue
  - loop
  - critic
  - knowledge-base
  - prompt-quality
  - llm-judge-bias
---

# Loop Critic sees which producer authored each candidate (LLM-judge identity bias)

**Filed:** 2026-07-07 — research-flagged, **not an observed incident.** Surfaced
during the 5-lane web research pass that optimized the `product-qa` expert
prompt (multi-agent / producer→critic lane). Captured here to be tackled later.

**Status:** OPEN / backlog. Not scheduled. Low urgency (see "Why this is latent,
not a fire"). Depends on nothing; best validated empirically on the first real
homelab loop run.

## Problem

In the parallel-stages loop, two producers add candidate work to the shared KB
and one Critic picks what to do next:

- **Scholar** files new-feature ideas as `plan` notes tagged `proposal`.
- **Product-QA** files issue candidates as `plan` notes tagged `qa-finding`.
- **Critic** reads both streams and selects the single next action.

The Critic currently *knows the source of every candidate* — both from the tag
(`proposal` vs `qa-finding`) and from its own kickoff, which explicitly names
"Scholar's new-feature `proposal` notes" and "Product-QA's `qa-finding` issue
notes" (`_ROLE_BLOCKS["critic"]` in `orchestrator/services/project_loops.py`).

The LLM-as-judge literature documents that a judge which can see *who authored*
a candidate tends to score on **source/identity** rather than on reasoning
quality — self-preference bias, verbosity bias, and position/order bias are all
well-attested. When two producers compete for one Critic slot, that means
selection can skew toward a stream (or toward whichever note is longer / appears
first) instead of toward the genuinely most valuable next action.

### Evidence / sources (from the research pass)

- LLM-judge bias survey — self-preference, verbosity, position bias, and the
  recommended mitigations (per-criterion atomic scoring; normalize/anonymize the
  candidate's source): https://arxiv.org/pdf/2410.02736 ·
  https://llm-judge-bias.github.io/
- Debate/identity work: judgments track *who said it* unless source is
  normalized; conformity/sycophancy exceeds obstinacy:
  https://arxiv.org/html/2510.07517 · https://arxiv.org/pdf/2509.23055

## Why this is latent, not a fire

- The **biggest lever was already pulled** on 2026-07-07: the producers were
  de-advocacy'd — Product-QA no longer "argues why it should beat a new Scholar
  feature," and both are framed as peers adding approaches to the KB (see
  `_ROLE_BLOCKS["product-qa"]` and `config/experts/product-qa/tactical.txt`
  "Prioritization Argument"). Removing the *hard sell* removes most of the
  verbosity/persuasion bias surface. Identity-blindness is the residual.
- The gain may be modest on the loop's actual model. The multi-agent research
  also found competition/structural framing yields "limited gains" on weaker
  reasoners and is "bounded by the strongest reasoner available" — on a
  MiniMax-class loop model, identity-blindness is a nudge, not a lever. Validate
  before investing.

## The catch: full identity-blindness conflicts with build-vs-fix triage

Naively "hide the tag" is **wrong here.** The Critic's job is explicitly a
build-vs-fix decision — "choosing a fix over a feature is a first-class outcome"
— and to weigh that it *needs* to know whether a candidate is a **new feature**
or a **fix to existing work**. So the category (build vs fix) is
decision-relevant and must stay visible.

What we want to strip is the **authoring identity / reputation** signal (which
agent wrote it, its self-assessed "pick me"), not the decision-relevant
category. That distinction is what makes this a design call rather than a
one-line change.

## Proposed approaches (pick during design)

1. **Normalize presentation, keep category.** Have the Critic (or a pre-pass)
   restate every candidate in one uniform schema — category (`new-feature` |
   `fix`), evidence, impact, size — with producer/author metadata stripped and
   order shuffled to blunt position bias. The Critic judges normalized cards.
2. **Independent per-candidate scoring before comparison.** Score each candidate
   against the Definition-of-Done rubric in isolation (per-criterion atomic
   scores), *then* rank — reduces holistic source-anchored judgment. Matches the
   survey's headline mitigation.
3. **Drop identity from the Critic kickoff.** Reword `_ROLE_BLOCKS["critic"]` so
   it references "new-feature candidates" and "fix/issue candidates" by category
   rather than by producer name ("Scholar's…", "Product-QA's…"). Cheapest
   partial step; keeps category, removes the named-source anchor.
4. **Do nothing / accept.** If the homelab run shows the de-advocacy pass already
   yields balanced build-vs-fix selection, the residual identity signal may not
   be worth the added Critic complexity. Record the decision either way.

## Acceptance criteria (for whoever tackles it)

- A decision is recorded on which approach (1–4) to take, with rationale.
- If we change triage: the Critic still makes an explicit build-vs-fix choice
  (category preserved) and no longer selects based on producer identity; verified
  on a real loop run where both a strong `qa-finding` and a strong `proposal` are
  present and the more valuable one wins regardless of which stream it came from.
- No regression to the KB machine contract (`qa-finding` / `proposal` tags stay;
  this is about how the Critic *reads/frames*, not the note format).

## Related

- [`../features/loop_parallel_stages.md`](../features/loop_parallel_stages.md) —
  the feature this concerns; its Open Question #4 ("should the Critic's KB read
  explicitly query both tags") is the same surface.
- `orchestrator/services/project_loops.py` — `_ROLE_BLOCKS["critic"]` (the
  kickoff that names the producers) and `_ROLE_BLOCKS["product-qa"]` (de-advocacy
  pass 2026-07-07).
- `config/experts/product-qa/` — the producer prompt, generalized +
  research-optimized 2026-07-07 (uncommitted).
