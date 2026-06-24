---
tags:
  - feature
  - agent
  - tool-use
  - fine-tuning
  - model-config
  - research
  - strategy
  - idea-capture
aliases:
  - tool fluency packs
  - tool specialization packs
  - finetuning sets for tools
  - tool-use datasets
related:
  - "[[agent_skills]]"
  - "[[default_expert_roster]]"
  - "[[default_skill_roster]]"
  - "[[db_backed_model_catalog]]"
  - "[[models_yaml_removal]]"
  - "[[family_centered_reasoning]]"
  - "[[auxiliary]]"
  - "[[agent_open_source_split]]"
  - "[[platform_for_agents]]"
  - "[[observability_and_quotas]]"
  - "[[no_workspace_agent_mode]]"
---

# Tool Specialization Packs — eval + finetuning data shipped with the toolset

> **Idea, captured — not a committed plan.** Should SRW ship, alongside its tools,
> a per-toolset **eval + specialization pack** (a generated tool-fluency benchmark,
> a verifiable synthetic SFT corpus, and optional LoRA adapters) so that cheap /
> open / self-hosted models can be made fluent with *our specific* tools instead of
> paying frontier-model prices? The technique (synthetic tool-use data → finetune)
> is mature and well-validated; the genuinely fresh part is the **packaging**: a
> repo that defines tools also emitting the corpus + eval to specialize a model to
> them — something the MCP ecosystem deliberately does *not* do today. The load-
> bearing design principle if we pursue it: **ship a generator, not a frozen
> dataset** (tools churn; a static JSONL rots confidently).

**Status:** Concept / idea capture. Filed 2026-06-24. No implementation, no decision.
Originated in a design discussion; this doc exists so we can pick it up later.

**Method note:** the landscape/precedent claims below are from a web pass during the
discussion (Gorilla, ToolLLM, APIGen/xLAM, ToolACE, Glaive, Hermes, FireFunction,
BFCL, "Don't Fine-Tune, Decode"). They're directionally sound but not adversarially
verified — if we promote this past "idea," a `deep-research` run should harden the
survey for the thesis. SRW-specific mappings (registry as seed corpus, per-expert
adapters, cost fit) are reasoned synthesis on top.

---

## TL;DR

- **The premise is half-right.** Modern open models (Qwen 2.5/3.x, Llama 3.x, Gemma,
  Mistral) *are* post-trained for generic function calling — "the model doesn't
  natively use tools" is no longer true in general. The real gap is fluency with
  **our specific toolset**, not function calling as a skill.
- **"Cognition spent on tool use" decomposes into three problems with three different
  cheapest-first answers** — and conflating them is what makes people reach for
  finetuning when they shouldn't:
  1. **Format / syntax** → mostly *free* via constrained / structured decoding. Not a
     finetuning problem.
  2. **Tool selection among many** → finetuning helps, but **tool retrieval** (RAG-
     over-tools) often helps more cheaply and survives tool churn.
  3. **Planning / composition** (the implicit grammar of *how our tools chain*) → this
     is where tool-specific finetuning is uniquely strong and retrieval/decoding don't
     help.
- **Strong precedent for the technique, zero novelty there** (good — it de-risks):
  Gorilla, ToolLLM/ToolBench, Toolformer, Glaive, Hermes-FC, APIGen→xLAM, ToolACE,
  NexusRaven, Granite-FC, FireFunction. Fine-tuned 7B-class models matching frontier
  function-calling at **~10% of the cost** (FireFunction v2) is a repeated result.
- **The fresh contribution is packaging/distribution.** MCP standardized the tool
  *interface* and stops there; nobody ships *training/eval data with the tools*. "An
  MCP server should be able to ship a LoRA-and-eval pack next to its schema" is an
  ownable idea.
- **The trap is tool churn.** A finetune is frozen against a tool snapshot; our
  `TOOL_REGISTRY` changes constantly. **Ship the generator + verifier + eval**, treat
  the JSONL/adapter as a *build output*, not source.
- **If we ever build it, the order is: eval pack FIRST.** A BFCL-style tool-fluency
  eval generated from the registry is pure upside, rots slowly, and is the denominator
  for any finetuning claim — it tells us *which* model×toolset cells are actually weak
  before we spend a token on data generation. The finetuning/LoRA layer is worth it
  **only** for the cheap/open tail of the model matrix, **only** for stable-core tools,
  and **only** once the eval proves a real gap.

---

## 1. The idea (as raised)

> Big labs finetune their models to their specific tool sets. With open models — or
> systems not built for any specific model — the model doesn't natively use the tools
> and always spends part of its cognition on picking the right tool and using it
> correctly. To counteract this, we could collect data on our tools (or generate
> synthetic datasets for the toolset), publish them with the repo, and offer
> "finetuning sets" so people can specialize a model to our tools.

This is a good instinct, and it lands squarely on a paved research road. The rest of
this doc refines the premise, catalogs the precedent, isolates what's actually novel,
and sketches what it would mean *inside SRW specifically*.

## 2. Where the premise is half-right

The 2023 framing "models can't really use tools" is largely obsolete. What's true now:

- **Generic tool-calling is table stakes, including for open weights.** Llama 3.x,
  Qwen 2.5/3.x, Mistral, Gemma all emit structured tool calls natively; Qwen is
  considered best-in-class for agentic/tool use among open models. They understand
  *function calling as a skill*. What they lack is fluency with *our 40-odd domain
  tools, their quirks, and their phase restrictions*.
- **The "cognition cost" is three distinct problems:**

  | Sub-problem | What it is | Cheapest-first answer |
  |---|---|---|
  | **Format / syntax** | emit valid JSON in the right schema | **Constrained / structured decoding** (grammar-enforced sampling). Model-agnostic, no weights touched. There's a paper literally titled ["Don't Fine-Tune, Decode"](https://arxiv.org/pdf/2310.07075) on exactly this. |
  | **Tool selection** | pick the right tool out of many | Finetuning helps, but **tool retrieval** (Gorilla's retriever, ToolGen) shrinks the candidate set first and survives tool changes. |
  | **Planning / composition** | the implicit grammar of *how our tools chain* (e.g. `next_phase_todos` → `todo_complete`; citation tools imply…) | **This is where tool-specific finetuning uniquely shines.** Retrieval and decoding don't help here. |

  The lesson: finetuning is the right hammer for **selection + composition**, the
  wrong (overkill) hammer for **format**. The stated pain — "spend cognition on using
  the correct tool and using it correctly" — is two of those problems, each with a
  cheaper first move than a finetune.

- **We already fight the format-heterogeneity problem.** The per-family prompt variants
  (`_gpt_oss`, `_minimax`) and the codex harmony-markup leaks are evidence: tool-call
  *format* differs per model family. A finetuning set is format-specific, so it would
  *lock* us to one format per model rather than free us from the heterogeneity. That
  cuts both ways and is worth weighing — see [[family_centered_reasoning]].

## 3. Precedent — this is a well-trodden technique

Roughly chronological; the technique is one of the most active sub-fields of the last
two years, which is good news (it works, we're not pioneering risk):

- **[Gorilla](https://gorilla.cs.berkeley.edu/) (Berkeley, 2023)** — finetuned LLaMA
  on synthetically generated instruction↔API pairs, *retriever-aware* so it generalizes
  to changed docs. The closest ancestor of this idea; read first.
- **ToolLLM / ToolBench** — 16k+ real REST APIs from RapidAPI, synthetic solution paths
  (DFSDT), plus an API retriever → ToolLLaMA.
- **Toolformer (Meta)** — self-supervised: the model annotates its own corpus with where
  API calls would help.
- **[Glaive function-calling](https://huggingface.co/datasets/glaiveai/glaive-function-calling-v2)**
  — open synthetic datasets *and* a 2.7B model whose explicit thesis is "use-case-
  specialised models you only use for the given task." Directly the "finetuning set"
  concept, already shipped.
- **[NousResearch Hermes-FC](https://huggingface.co/datasets/NousResearch/hermes-function-calling-v1)**
  — open dataset + a structured tool-call format used to specialize the Hermes models.
- **[APIGen → xLAM (Salesforce)](https://huggingface.co/datasets/Salesforce/xlam-function-calling-60k)**
  — the one most aligned with the *right* way to do this: an automated pipeline that
  generates **verifiable** function-calling data (every sample checked by actually
  executing it), 60k entries over 3,673 executable APIs, fully open. The follow-up
  APIGen-MT does the multi-turn agentic version.
- **[ToolACE](https://arxiv.org/html/2409.00920v1)** — self-evolving synthesis focused
  on accuracy and API diversity.
- **NexusRaven / Granite-FC (IBM)** — emphasis on generalizing to *unseen* tools purely
  from descriptions (relevant to the churn problem).
- **[FireFunction v2 (Fireworks)](https://fireworks.ai/blog/firefunction-v2-launch-post)**
  — the market proof point: a finetuned Llama-3-70B matching GPT-4o on function calling
  at ~2.5× speed and **~10% of the cost**. For a budget-conscious, multi-model
  orchestrator, that economics is the whole argument.
- **[Berkeley Function Calling Leaderboard (BFCL)](https://gorilla.cs.berkeley.edu/leaderboard.html)**
  — the standard eval, AST-based matching that scales to thousands of functions. The
  *measurement* half we'd want to mirror.

**Takeaway:** zero novelty in the technique, and that's a feature. Fine-tuned small
models reaching much-larger-model tool performance on a fixed toolset is a robust,
repeated result.

## 4. What's actually novel in *our* framing

**Nobody ships the training data *with the tools*.** MCP standardized the tool
*interface* (schemas, descriptions, JSON-RPC) and there are hundreds of MCP servers —
but MCP deliberately stops at the interface. There is no convention for "here's the
server, *and* here's its companion fine-tune/eval pack so you can specialize a cheap
local model to it." Every dataset in §3 is a standalone artifact decoupled from any
running toolset.

Our framing — *the repo that defines the tools also emits the specialization corpus and
eval for them* — is a **distribution/packaging convention**, and that part is
underexplored. It's the "an MCP server should be able to ship a LoRA-and-eval pack next
to its schema" idea. Clean, ownable, and well-shaped for a source-available release (see
§7).

## 5. The trap, and how to dodge it

The reason this isn't already standard practice is **tool churn**. A finetune is frozen
against a *snapshot* of the tools. SRW's `src/tools/registry.py` changes constantly —
every rename, schema field, or new phase-restricted tool invalidates a static dataset,
and worse, it's *confidently* stale (the model memorized the old signature).

> **Design principle that makes the idea robust:** ship the **generator + verifier +
> eval**, not the frozen dataset. The versioned artifact is the *pipeline* that
> regenerates data from the current tool schemas, plus the eval that proves a given
> model×toolset combo is fluent. The JSONL / LoRA is a build output — like a compiled
> binary, regenerable, not source.

We're unusually well-positioned for this because **our tools are already machine-
readable**: `TOOL_REGISTRY` metadata + JSON schemas + phase restrictions are exactly the
seed an APIGen-style generator consumes. No hand-authoring.

## 6. What this would look like inside SRW (sketch, not a plan)

Order matters — and notably the first step is *not* a finetuning set:

1. **Eval pack first.** A BFCL-style, AST-based **tool-fluency eval** generated from
   `TOOL_REGISTRY`: given expert *Y*'s toolset, does model *X* select and fill the right
   calls? We need this *anyway* (it's the denominator for any finetuning claim), it rots
   far slower than training data, it's cheap, and it immediately shows *which model×
   toolset cells in the matrix are weak*. Plausible outcome: the good open models (Qwen)
   are already fine and only the small/cheap ones need help — which scopes the whole
   effort down. Slots beside the model matrix ([[db_backed_model_catalog]],
   [[models_yaml_removal]]).
2. **APIGen-style generator**, keyed off the registry: generate candidate
   (instruction → tool-call) samples and **verify by execution** against existing test
   backends (`FilesystemTestBackend`, mock datasources); keep only what executes
   correctly. Verifiability is what separated xLAM/ToolACE from the noisy early datasets
   — don't skip it. The auxiliary-LLM seam ([[auxiliary]]) is a natural home for the
   generation/verification passes.
3. **LoRA adapters per toolset/expert, not full finetunes** — literally "a finetuning
   set you can apply": swappable, cheap, composes with the per-expert config system
   ([[agent_skills]], [[default_expert_roster]]). One adapter per stable expert (scholar,
   developer, critic) is the natural unit.
4. **Target the cheap/open tail of the model matrix, and only stable-core tools.** Don't
   specialize against still-churning tools; don't bother for frontier models where it
   buys little. ROI is concentrated: cheap model + stable toolset + cost-sensitive
   deployment.

## 7. Strategic fit (why this is more than a micro-optimization)

- **Cost.** The FireFunction "~10% of cost" result maps straight onto the budget-
  conscious posture and the quota/usage work ([[observability_and_quotas]]). A
  specialized cheap model on the stable core is a direct lever on per-job LLM spend.
- **Open-source split.** If the agent goes source-available (see
  [[agent_open_source_split]]), people will run it on cheap local models — and a
  tool-pack is exactly what makes those models *usable*, driving agent adoption. The
  agent's tools (`src/tools/`) are the part going source-available, so "ship the pack
  with the toolset" aligns perfectly with "ship it with the source-available agent."
- **Platform angle.** Less direct for [[platform_for_agents]] (foreign harnesses bring
  their own tools), but a per-toolset eval is still the substrate-level way to certify
  "model *X* is fluent with capability *Y*."
- **Licensing nuance (important, easy to get wrong).** The repo is now **FSL-1.1-ALv2
  (source-available, *not* open source)** with a B2B deploy-and-consult model. But
  *data artifacts are not code.* We could license the generated corpus + eval more
  permissively than the generator (e.g. a permissive/CC data license, even while the
  *generator* stays FSL as part of the platform) — data isn't the moat, and a permissive
  data license is what would actually drive community adoption and outside contributions.
  Decision deferred; just flagging that "open-source the finetuning sets" needs to be
  restated as "source-available generator + (separately, possibly permissively) licensed
  data outputs." Never call the repo itself "open source."

## 8. Open questions to resolve before this graduates past "idea"

- **Is there even a gap?** Build the eval first and measure. If Qwen-class open models
  already clear our toolset, the finetuning layer may be unnecessary and this stays an
  eval-only effort.
- **Format lock-in.** Per-family tool-call formats mean one adapter ≠ portable. Do we
  accept per-family adapters, or does constrained decoding + good descriptions get us
  "good enough" without any weights? (Cheapest path might beat the whole idea.)
- **Maintenance contract.** Who/what regenerates packs on tool change? Is it a CI job off
  the registry? Without automation this becomes a maintenance tax — the exact thing the
  "ship a generator" principle is meant to prevent.
- **Retrieval vs. finetune for the selection problem.** Tool retrieval may dominate
  finetuning for "select among many" while being churn-proof. Worth a head-to-head before
  committing to data generation.
- **Scope of "stable core."** Which tools are stable enough to be worth specializing
  against? Needs a churn audit of `TOOL_REGISTRY`.

## 9. Possible next steps (when picked up)

- **(a)** Promote to a real design doc: add acceptance criteria, pick the eval format,
  and scope the registry→eval generator as the first concrete slice.
- **(b)** Run a `deep-research` harness to harden the literature survey (esp. retrieval-
  vs-finetune and churn-robustness) for thesis-grade grounding.
- **(c)** Spike the **eval pack only** (no finetuning) against 2–3 models in the matrix to
  answer "is there a gap?" — the cheapest experiment that de-risks everything downstream.

Recommended first move if/when revisited: **(c)**, then **(a)**.

---

## Sources

- Gorilla / BFCL — <https://gorilla.cs.berkeley.edu/> · <https://gorilla.cs.berkeley.edu/leaderboard.html>
- APIGen / xLAM (Salesforce) — <https://huggingface.co/datasets/Salesforce/xlam-function-calling-60k>
- ToolACE — <https://arxiv.org/html/2409.00920v1>
- Glaive function-calling — <https://huggingface.co/datasets/glaiveai/glaive-function-calling-v2>
- NousResearch Hermes-FC — <https://huggingface.co/datasets/NousResearch/hermes-function-calling-v1>
- "Don't Fine-Tune, Decode" (constrained decoding for tool use) — <https://arxiv.org/pdf/2310.07075>
- FireFunction v2 (Fireworks) — <https://fireworks.ai/blog/firefunction-v2-launch-post>
- MCP overview (interface, not training data) — <https://modelcontextprotocol.io/>
