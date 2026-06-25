---
tags:
  - issue
  - prompting
  - experts
  - config-resolution
  - loader
  - model-families
related:
  - "[[global_expert_management]]"
  - "[[default_expert_roster]]"
  - "[[agent_skills]]"
---

# Expert-authored prompt files are silently shadowed by framework family variants

**Filed:** 2026-06-25, discovered while scaffolding the new `assistant` default
session expert ([[default_expert_roster]]) and k3d-verifying that it loads.

**Status:** Part 1 **implemented + verified on k3d** (2026-06-25), uncommitted on
`develop`. Resolution + blob probes confirm experts' own prompts now resolve on
gemma (`bughunter`/`developer`/`scholar` persona+strategic+tactical flip to
EXPERT; `assistant` blob persona fixed; `scholar@minimax` no regression); 12 unit
tests + `ruff` green. **Gemma worker gate: PASSED** — a live k3d worker run (a
scholar subjob on the default gemma model, with the experts' base prompts now
active) made **84 `write_file` + 4 `run_command` + 10 `todo_complete` tool calls
with 0 parse failures and no parser-loop**, across clean strategic→tactical phase
transitions. The `gemma.yaml` guardrails (71 brace-form tool examples, a separate
channel the fix doesn't touch) hold the wire format, so swapping the framework
`*_gemma` prompts for the experts' base prompts did **not** regress tool-call
parsing. The developer-specific run (highest risk — its `instructions.md` has 3
Python-style examples) is expected clean for the same reason (same guardrails) and
is in-flight to confirm. **Part 2 (content/adaptation hygiene) still deferred.**
Original bug affected shipped experts (`bughunter`, `designer-interactive`), not
just the new `assistant`.

**Severity:** Silent correctness bug. No error, no warning — an expert's
authored prompt is replaced by the framework's generic one, and the agent runs
with the wrong identity/methodology. Only surfaced because we resolved a config
on-cluster and inspected the output.

## Symptom

An expert that ships its own prompt file for a segment — e.g.
`config/experts/bughunter/strategic.txt` (its adversarial methodology) or
`config/experts/assistant/persona.txt` — has that file **silently ignored** when
the agent runs on a model family for which the **base** `model_config_matrix.yaml`
defines a variant (e.g. `gemma`, the homelab default model). The agent instead
gets the generic framework file (`config/prompts/strategic_gemma.txt`,
`persona_gemma.txt`, …).

Confirmed in-pod by resolving configs through the real production path
(`services.config_resolver.resolve_config` → `src/core/loader`):

| Expert @ family | persona | systemprompt | strategic | tactical |
|---|---|---|---|---|
| `bughunter @ gemma` | framework | framework | **framework** | **framework** |
| `assistant @ gemma` | framework | framework | framework | framework |
| `developer @ gemma` | **expert** | framework | **expert** | **expert** |
| `scholar @ minimax` | **expert** | framework | **expert** | **expert** |

`bughunter`'s custom adversarial `strategic.txt`/`tactical.txt` are **dead on
gemma**. `designer-interactive`'s `persona.txt` likewise degrades to the generic
framework persona on gemma (it only "works" because it *also* ships a custom
`systemprompt_interactive.txt`, which has no gemma variant to shadow it — see
below). `developer`/`scholar`/`critic`/`designer` escape only because they carry
a per-expert workaround (see Root cause).

## Scope: this is systemic, not persona-specific

Every prompt segment routes through the same resolver, so all of them are
exposed wherever the base matrix variant-maps the running family. The base
`config/model_config_matrix.yaml` `gemma` block (lines ~384-402) variant-maps
**persona, systemprompt, strategic, tactical, summarization** → `*_gemma.txt`,
and every one of those files exists in `config/prompts/`. The only segment that
escapes on gemma is **`systemprompt_interactive`** — there is no
`systemprompt_interactive_gemma.txt`, so it falls back to the base name and the
expert-dir copy wins. (That single gap is why session experts can reliably
override the interactive system prompt but not the persona.)

## Root cause

Two independent resolution axes that don't compose correctly, on top of a prompt
model that conflates three concerns into single files.

**The two axes (`src/core/loader.py`):**

1. **Filename selection** — `MatrixResolver.resolve_filename()` (lines
   1015-1047): a 4-level chain → (1) expert matrix family-specific, (2) expert
   matrix default, (3) **base matrix family-specific**, (4) base matrix default,
   (5) hardcoded `<seg>.txt` (lines 1086-1096). For `gemma`, Level 3 returns
   `persona_gemma.txt` (etc.).
2. **Location** — `FileResolver.resolve()` (lines 848-874): for *that one
   filename*, check expert dir → framework dir.

The seam fails because Axis 1 hands Axis 2 a **framework-only filename**
(`persona_gemma.txt`), so Axis 2's "expert dir first" rule never gets the chance
to prefer the expert's `persona.txt` — that filename was already discarded. The
expert's base file is unreachable.

**The deeper conflation.** Each `<segment>_<family>.txt` bundles three concerns:

- **Role/phase content** — what *this expert* does (expert-owned).
- **Model-specific behavioral guidance** — "don't emit skeletons" (gpt_oss),
  anti-leak nudges, tool-use discipline (framework-owned, family-specific).
- **Format/markup** — `<role>` tags vs `# Headers` (framework-owned,
  family-specific).

Evidence they're tangled: `persona_gemma.txt` is a *pure markup reflow* of
`persona.txt` (identical words, `<role>` → `# Role`; 222 vs 224 words), but
`persona_gpt_oss.txt` is *rewritten content* (164 words, model-specific
anti-skeleton nudges), and `systemprompt_gemma.txt` carries **+217 words** of
model guidance over the base. So a single file is simultaneously the expert's
content *and* the framework's model-adaptation, and the resolver can only pick
one — the framework wins, destroying the expert's content.

**The composition seam already exists** (and is the basis of the fix). The
worker `config/prompts/systemprompt.txt` is a *template with slots*:

```
<identity>You are {agent_display_name}… {expert_identity}</identity>   ← persona slots here
{available_skills}
…model-specific guidance, memory model, constraints…
<phase_directive>{prompt_content}</phase_directive>                    ← strategic/tactical slot here
```

i.e. a family-specific **wrapper** (`systemprompt_<family>.txt`) already owns the
model-adaptation, and the **persona** (`{expert_identity}`) and **phase
directive** (`{prompt_content}` = strategic/tactical) slot into it as content.
The `persona_<family>` / `strategic_<family>` variants are a *second, redundant*
home for model-adaptation that ends up competing with — and clobbering — expert
content.

**Why most experts look fine (the workaround that proves the bug).**
`critic`, `designer`, `developer`, `scholar` ship their own
`config/experts/<name>/model_config_matrix.yaml` and/or matching
`<seg>_<family>.txt` files. `developer/model_config_matrix.yaml` has:

```yaml
default:
  prompts:
    persona: persona.txt
    strategic: strategic.txt
    tactical: tactical.txt
```

That `default` block (Axis-1 Level 2) beats the base matrix's family entry, so
its base files resolve. **That is the per-expert workaround, already in the
repo** — it both proves the workaround works and shows it costs O(experts ×
families) of boilerplate. `bughunter` simply forgot it, and broke silently.

## DB-backed experts: immune to this bug, but a related fidelity gap

DB-backed experts ([[global_expert_management]]) resolve prompts on a
**different path** and are **not affected by the shadowing bug** — but examining
them surfaces a related, *confirmed* gap and validates the chosen solution.

**They're immune.** A DB expert stores its persona/instructions as literal
strings in `experts.prompts` (JSONB). `resolve_config` overlays them onto the
blob *after* matrix/file resolution (`orchestrator/services/config_resolver.py:127-133`;
`build_expert_config` in `src/core/expert_resolution.py:131`), so they never go
through the family-filename lookup. Verified in-pod with a synthetic DB row on
the gemma default: the DB persona/instructions win; the framework `persona_gemma`
is overwritten. There is no filename, so there is nothing to shadow. **Part 1
does not touch DB experts** — correctly scoped; no risk, no benefit.

**But DB experts can only customize `persona` + `instructions`.** The
`experts.prompts` JSONB holds exactly those two keys. The same in-pod probe
shows `strategic` / `tactical` / `systemprompt` / `summarization` for a DB expert
stay on the framework versions — a DB expert cannot override them at all.

**Confirmed fork-fidelity gap.** Because of that, **duplicating or exporting a
bundled expert silently drops its phase prompts.** `_bundled_expert_bundle`
(`orchestrator/main.py:18498-18530`) — the source for both
`POST /api/experts/{id}/duplicate` and `GET /api/experts/{id}/export` — reads
only `persona.txt` and `instructions.md` (line 18516); `strategic.txt`,
`tactical.txt`, `systemprompt.txt`, and all `_<family>` variants are not
captured. So forking `bughunter` (whose entire value is its adversarial
`strategic` / `tactical`) yields a DB copy with its config + persona but
**generic framework phase prompts**, with no warning. Same for
`developer` / `scholar` / `designer`. This is a *separate* defect from the
shadowing bug (different code path) but shares the same root model — "an expert's
prompts = persona + instructions only" — and should be tracked (here or split
into its own issue).

**Trust boundary (must persist).** DB personas are untrusted user content:
fenced via `fence_persona` (`src/core/expert_resolution.py:148`) and subordinated
below operator policy through the `_persona_source: 'db'` marker. Bundled
personas are operator-authored and not fenced. The two paths exist for this
reason and must stay differentiated even if the content model is unified.

**Why this validates Part 2.** The DB overlay model — *persona is a content
string slotted into the framework wrapper* — is exactly Part 2's target
architecture, already working (the probe shows the DB persona slotting in while
`systemprompt_gemma` still supplies model-adaptation). Part 2 should therefore
(a) **unify the content model** so bundled (file) and DB (string) experts both
feed the same slots, with the wrapper owning model-adaptation; (b) **preserve the
trust boundary** (DB content stays fenced); and (c) **close the capability gap** —
let DB experts carry `strategic` / `tactical` content and make `duplicate` /
`export` capture all segments, fixing the fork-fidelity drop.

## Solution chosen

Separate model-adaptation from content and lean on the wrapper+slot seam the
system already has. Two parts — Part 1 is the required correctness fix; Part 2
is the content-hygiene follow-on that makes the separation real (incremental,
not required for correctness).

### Part 1 — Location-primary file resolution (the fix)

Make a file in the **expert directory always outrank a file in the framework
directory**, with family-specificity as the *secondary* axis within each
location. For any file-resolved prompt/instruction, the candidate order becomes:

```
1. expert/<seg>_<family>     ← expert opted into a family-specific version
2. expert/<seg>              ← the expert's content        ★ the fix: beats framework family
3. framework/<seg>_<family>  ← framework's family default
4. framework/<seg>           ← framework base
```

Today the effective order is `1 → 3 → 2 → 4` (framework family beats expert
base). Flip it to **location-primary** (expert dir is the outer loop,
family-specificity the inner):

```python
# in the file-resolving MatrixResolver path (PromptMatrixResolver,
# InstructionMatrixResolver) — NOT settings/guardrails, which are values not files
family_name = resolve_filename(entry_type)        # honors expert-matrix overrides (Levels 1-2)
base_name   = HARDCODED_DEFAULTS.get(entry_type, f"{entry_type}.txt")
for directory in (deployment_dir, framework_dir):
    for name in (family_name, base_name):
        if (directory / name).exists():
            return directory / name
```

Properties:

- **Fixes every segment at once** (persona, strategic, tactical, systemprompt,
  instructions) — they all share this resolver.
- **Retroactively repairs** `bughunter`, `designer-interactive`, `assistant`.
- **Removes boilerplate** — the per-expert `model_config_matrix.yaml` blocks that
  `developer`/`scholar`/`critic`/`designer` carry for this can be deleted.
- **One rule, statable in a sentence** ("your own files always win"), matching
  `FileResolver`'s own documented contract (deployment dir first).
- **Not lossy** — the family wrapper (`systemprompt_<family>`) still applies the
  family's format and model guidance *around* the slotted content. The only thing
  an expert gives up by shipping its own `persona.txt` is the model-specific
  *nudges* currently duplicated inside `persona_<family>` — which Part 2 relocates
  to where they belong.

### Part 2 — Content hygiene: model-adaptation in the wrapper (incremental)

Make the slots **content** and the wrapper own **adaptation**:

- Move model-specific behavioral guidance (gpt_oss anti-skeleton, anti-leak,
  tool-use discipline) into `systemprompt_<family>` — applied to *every* expert
  regardless of role, instead of buried inside `persona_<family>`.
- Treat `persona` / `strategic` / `tactical` as role/phase **content**, kept as
  format-neutral as practical (models read XML and markdown fine).
- Retire the **pure-reformat** variants (`persona_gemma` etc.) — they earn
  nothing once Part 1 lands.

This is a content migration, not a code change; do it opportunistically. It is
what makes the system coherent long-term: one home for "how this model likes to
be addressed," a separate home for "what this expert does," composed — instead
of an N-experts × M-families grid of files each re-encoding all three concerns.

### Rejected alternative — per-expert matrix override (Option A)

Add `config/experts/assistant/model_config_matrix.yaml` with
`default: {prompts: {persona: persona.txt, …}}` (what `developer` already does).
Rejected as the *solution*: it treats the symptom, scales as O(experts ×
families) of boilerplate, doesn't fix `bughunter`/`designer-interactive`, and is
exactly the failure mode that produced this bug (forget the file → silent
breakage). It remains a valid one-expert unblock under release pressure.

## Implementation plan

1. **Resolver change** — rework the file-resolution step in the file-resolving
   `MatrixResolver` subclasses (`PromptMatrixResolver`, `InstructionMatrixResolver`)
   to location-primary order as above. Keep `resolve_filename` for the
   family/base *name* selection (so explicit expert-matrix overrides are still
   honored); change only how the chosen name(s) are located on disk. Leave
   settings/guardrails (value resolution) untouched.
2. **Delete the workaround boilerplate** from `developer`/`scholar`/`critic`/
   `designer` per-expert matrices where it exists solely to dodge this (verify
   each still resolves to its own content afterward).
3. **Part 2 note** — file a short follow-on under `docs/features/` for the
   wrapper/content split + variant retirement.

## Validation

- **Unit tests** pinning all four ranks, across `persona` + `strategic` +
  `tactical` + `systemprompt`, for: expert-ships-base-only,
  expert-ships-family-variant, expert-ships-nothing, and
  expert-matrix-override-still-honored.
- **Re-probe in-pod** (same `PromptMatrixResolver` probe used to find this):
  `bughunter` / `designer-interactive` / `assistant` should flip to **expert**
  for the shadowed segments on gemma.
- **Regression-check** `developer` / `scholar` / `critic` / `designer` — must
  still resolve to *their own* content (never the framework generic). Chase down
  one observed quirk: `developer @ gpt_5` resolved to `persona.txt` rather than
  its shipped `persona_gpt_5.txt` (likely a family-key match issue in the
  per-expert matrix); confirm the fix doesn't regress it.
- **Eyeball the now-activated prompts** — `bughunter`'s adversarial
  `strategic`/`tactical` and `designer-interactive`'s persona have effectively
  never run on gemma; confirm they read well once live.

## Risk

Part 1 changes behavior by design — experts begin using their own prompts. The
change is confined to the currently-broken case (expert ships a base file, base
matrix has a family variant, expert lacks that variant); experts that ship
family variants or ship nothing resolve identically. Shared prompt/instruction
resolution → land behind the test sweep before trusting it.

## References

- Resolver: `src/core/loader.py` — `MatrixResolver.resolve_filename` (1015-1047),
  `FileResolver.resolve` (848-874), `HARDCODED_DEFAULTS` (1086-1096), persona
  consumption (3440-3501).
- Base matrix: `config/model_config_matrix.yaml` (gemma block ~384-402).
- Wrapper/slot template: `config/prompts/systemprompt.txt`,
  `config/prompts/systemprompt_interactive.txt`.
- Existing per-expert workaround: `config/experts/developer/model_config_matrix.yaml`.
- Discovery context: scaffolding `config/experts/assistant/` for
  [[default_expert_roster]].
