# DB-backed session experts can't customize the interactive system prompt

**Date:** 2026-07-02
**Status:** Deferred (intentionally). Low urgency — filed to capture the
architectural finding so it isn't re-derived. Not a regression; this gap
predates and is orthogonal to Part 1/Part 2 of the expert-prompt work.
**Component:** `src/core/loader.py` (`get_phase_system_prompt`, interactive
branch), `orchestrator/services/config_resolver.py` (overlay keys),
`config/prompts/systemprompt_interactive.txt` (the wrapper),
`cockpit/.../expert-editor.component.ts`.
**Related:** `docs/issues/expert_prompts_shadowed_by_family_variants.md`
(Part 1 location-primary resolution + Part 2 DB-expert phase-prompt parity —
this is the session analog of Part 2, deliberately not done there).

## Summary

After Part 2, **worker** DB/forked experts can carry their own
`strategic`/`tactical`/`summarization` prompt content. **Session** DB/forked
experts still can't customize their interactive behavior beyond `persona` (+ a
workspace `instructions.md`). The interactive system prompt content — the
`<interactive_mode>` / `<constraints>` / `<memory_model>` behavior contract — is
framework-owned and not reachable from the `experts.prompts` overlay.

This is a genuine capability gap, but a **narrow, low-value** one, and — unlike
Part 2 — it is **not** a small overlay-widen. The naive version is a trust
regression. This doc records why, and the safe design if we ever pick it up.

## Why it is NOT "the same shape as Part 2"

Part 2 was small because the worker prompt is a **wrapper with a subordinate
content slot**. The interactive prompt is a wrapper with **no such slot** — the
workflow is baked into the wrapper itself.

| | Worker (`systemprompt.txt`) | Session (`systemprompt_interactive.txt`) |
|---|---|---|
| Slots | `{agent_display_name}`, `{expert_identity}` (persona), `{available_skills}`, **`{prompt_content}`** | `{agent_display_name}`, `{expert_identity}` (persona), `{available_skills}` |
| Customizable workflow lives in | the `{prompt_content}` slot = `strategic`/`tactical` (a clean, subordinate, fenceable region) | **inline in the wrapper** — `<interactive_mode>`, `<constraints>`, `<memory_model>`, `<instruction_hierarchy>` |
| How a bundled expert customizes it | ships `strategic.txt` / `tactical.txt` (content only) | ships a **whole replacement** `systemprompt_interactive.txt` (see `config/experts/designer-interactive/`) |

Part 2 overlays a fenced workflow **string** into the framework-owned wrapper
(`get_phase_system_prompt`, worker branch, `src/core/loader.py:3656-3669`,
fenced via `fence_phase_directive`). The interactive branch
(`loader.py:3588-3635`) has no content slot to overlay into: it fills
`{expert_identity}` (persona, fenced when DB) and `{available_skills}` and
nothing else. The `_OVERLAY_PROMPT_KEYS` set in
`orchestrator/services/config_resolver.py`
(`persona, instructions, strategic, tactical, summarization`) deliberately
excludes `systemprompt_interactive`; `serialize_resolved_config`
(`loader.py:4439`) resolves it from **files only**, so a DB session expert with
no expert directory always gets the framework default.

## Why the naive version is a trust regression

"Let DB session experts override `systemprompt_interactive`" means handing
**untrusted user content the entire system-altitude prompt**:

- **Slot contract**: the wrapper is consumed by `template.format(...)`
  (`loader.py:3621`). A user-authored wrapper that drops `{expert_identity}` or
  `{available_skills}` crashes render (or silently loses persona-fencing /
  skills injection).
- **Safety scaffolding**: `<instruction_hierarchy>`, the no-fabrication
  `<constraints>`, and the memory model live in the wrapper. A user could strip
  them.
- **Altitude**: this content *is* the system prompt. There's no subordinate
  region to fence it into (contrast persona → `fence_persona`, phase directive →
  `fence_phase_directive`, skills → `fence_skills_menu`). It would sit at the
  highest priority altitude — exactly what those fences exist to prevent.

`designer-interactive` gets away with a full replacement only because it is
**bundled = trusted** and the author re-supplies the slots + safety blocks by
hand. That trust doesn't extend to DB-authored experts.

## Safe design (if picked up later)

Do **not** make `systemprompt_interactive` overlay-able. Instead, mirror Part 2:
add a **subordinate content slot** to the interactive wrapper.

1. Add `{expert_workflow}` (or reuse the `{prompt_content}` idea) to
   `config/prompts/systemprompt_interactive.txt` — a clearly-bounded region
   *below* the framework's `<interactive_mode>` / `<constraints>`.
2. Add `interactive` (or `interactive_workflow`) to `_OVERLAY_PROMPT_KEYS` in
   `config_resolver.py` — one family-agnostic string, per the Part 2 thesis
   (family adaptation stays in the `_<family>` wrappers).
3. In `get_phase_system_prompt`'s interactive branch (`loader.py:3588`), fence
   the DB-authored workflow string via `fence_phase_directive` (reuse — it's
   already brace-safe + subordinate-but-authoritative) when the key is in
   `_db_prompt_keys`, and slot it into `{expert_workflow}`.
4. Surface an "Interactive workflow" textarea in the session branch of the
   cockpit editor (`expert-editor.component.ts`); worker branch keeps
   strategic/tactical. Extend `_bundled_expert_bundle` if we want forking a
   bundled session expert to capture its interactive content.
5. Migration: none structural (`prompts` JSONB is open, same as Part 2).

Framework keeps owning the wrapper + safety + slots; the user content is a
subordinate, fenced block. Bundled session experts keep their full-replace power
(trusted file resolution) untouched.

## Why deferred (value judgment)

- A DB session expert **already** gets `persona` (fenced into
  `{expert_identity}`) + a workspace `instructions.md`. That covers
  "who you are / domain identity / how to behave" for the realistic
  session-expert cases (an assistant variant, a domain helper).
- The one thing persona+instructions can't reach — reshaping the interactive
  behavior contract — is a **narrower** need than the worker phase-workflow case
  Part 2 addressed, and the expert that genuinely needs it
  (`designer-interactive`) is bundled and already works via file replacement.
- So the asymmetry Part 2 leaves (workers can customize their phase workflow;
  sessions can't customize their interactive workflow) is real but low-impact,
  and closing it is a small **feature** (new slot), not a pure overlay-widen —
  not worth pulling ahead of the default-expert-roster work.

## How to verify a fix (when picked up)

- Fork/create a DB session expert with a sentinel `interactive` workflow string
  containing a literal `{ "x": 1 }` (brace-safety regression guard) → attach a
  session → confirm the string renders inside the `{expert_workflow}` region
  wrapped in `<expert_workflow …>`, and `.format()` does not crash.
- Confirm a session expert that leaves the field empty falls back to the
  framework interactive prompt (inherit semantics, matching the truthy overlay
  guard).
- Confirm the framework `<instruction_hierarchy>` / `<constraints>` still render
  above the fenced user block (safety scaffolding intact).

## References

- Interactive prompt assembly: `src/core/loader.py:3588-3635`
  (`get_phase_system_prompt`, `prompt_type == "interactive"`).
- Interactive prompt build site: `src/api/persistent_session.py:282-288`.
- Overlay keys: `orchestrator/services/config_resolver.py` (`_OVERLAY_PROMPT_KEYS`).
- Wrapper: `config/prompts/systemprompt_interactive.txt` (no content slot);
  bundled full-replace example: `config/experts/designer-interactive/systemprompt_interactive.txt`.
- Fences to reuse: `src/core/expert_resolution.py` — `fence_persona`,
  `fence_phase_directive`, `fence_skills_menu`.
- Part 1/Part 2: `docs/issues/expert_prompts_shadowed_by_family_variants.md`;
  `docs/done/global_expert_management.md` (Decision 7).
