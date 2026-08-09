---
tags:
  - issue
  - agent
  - prompts
  - experts
related:
  - "[[stale_tool_names_degrade_every_worker_job_tool_load]]"
---

# Ten expert prompt files still tell the model to call `browse_website`, a tool that no longer exists — while the replacement sits unmentioned in the same config

**Filed:** 2026-07-29, found while removing the stale names from the config tool
lists (`docs/done/stale_tool_names_degrade_every_worker_job_tool_load.md`). That
fix cleaned the `tools:` lists; this is the *prose* half, deliberately left out
of it because rewording agent instructions changes behaviour and deserved its
own decision.
**Status:** FIXED 2026-08-08 — all ten files reworded, plus a lint
(`tests/test_expert_prompts_only_name_granted_tools.py`) so the class cannot
recur. Not yet deployed; prompt text is baked into `resolved_config` at thread
creation, so this reaches **new** sessions/jobs only.
**Severity:** medium — this is a live behavioural defect, not cosmetic. The
model is being instructed to call a tool that is not in its toolset, so at best
it ignores the instruction, at worst it burns a turn on a hallucinated call and
recovers. It also means the visual-verification step these prompts describe is
effectively unavailable to the model unless it discovers `browser_*` on its own.
**Component:** `config/experts/{designer,designer-interactive,developer,scholar}/`
— see the file list below.

## Summary

`browse_website` was removed from `TOOL_REGISTRY`
(`src/tools/research/__init__.py:27`) when the direct `browser_*` tools replaced
the autonomous sub-agent. The tool *lists* have now been cleaned. The prompt text
has not:

| File | Reference |
|---|---|
| `designer/persona.txt:22` | "Use browse_website to inspect the current application when available." |
| `designer/strategic.txt:13` | "use `browse_website` to see the current UI in action" |
| `designer/instructions.md:20` | "use browse_website to see the actual rendered UI" |
| `designer/design_guide.md:565` | "If `browse_website` is available…" |
| `designer/strategic_todos_initial.yaml:41` | "use browse_website to see the current UI rendered" |
| `designer-interactive/design_guide.md:565` | same as designer |
| `designer-interactive/systemprompt_interactive.txt:90` | "Use browse_website to inspect the running application" |
| `developer/strategic.txt:63` | "via web_search / browse_website" |
| `developer/strategic_minimax.txt:84` | "via web_search / browse_website" |
| `scholar/tactical.txt:21` | "`browse_website` for interactive sites needing JavaScript rendering" |

**The replacement is already present.** All four experts `$extends: worker_base`
and therefore inherit the full `browser_direct` group — 9 tools
(`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`,
`browser_select`, `browser_scroll`, `browser_screenshot`, `browser_back`,
`browser_close`). So every one of these instructions can be reworded to a
capability the agent actually holds; none of them needs to be deleted for lack
of a replacement.

Note `designer/design_guide.md:565` hedges — "If `browse_website` is available
and a file server is running" — so that one degrades gracefully today. The
unhedged ones (`persona.txt`, `systemprompt_interactive.txt`) do not.

## Fix

Reword each to the `browser_direct` tools. The natural phrasing for the designer
family is navigate + snapshot/screenshot, since what those prompts want is
"look at the rendered UI":

> Use `browser_navigate` to open the running app and `browser_snapshot` (or
> `browser_screenshot`) to inspect the rendered UI.

For `developer/strategic*.txt` and `scholar/tactical.txt` the reference is a
research fallback next to `web_search`; `browser_navigate` + `browser_snapshot`
covers the "interactive sites needing JavaScript" case the scholar prompt names.

Two cautions:

1. `persona.txt` and the `systemprompt_interactive*` variants are **forked per
   model family** in places — check for siblings before editing one, or the
   edit reaches only some models.
2. Prompt text is baked into `resolved_config` at thread creation and preferred
   over disk on read, so a `.txt` edit reaches **new sessions only**. Existing
   sessions keep the old instruction until they end.

## As applied (2026-08-08)

All ten references now name `browser_navigate` plus `browser_snapshot` (read the
DOM) or `browser_screenshot` (see the rendering), following the existing house
phrasing at `config/experts/bughunter/tactical.txt:34`. Verified first that all
four experts really do hold those three tools in their merged config — naming a
tool they lacked would have been this same bug again.

**No forks were missed.** `developer` has four `strategic*` variants and
`scholar` three `tactical*`; only the two that actually contained the stale name
needed editing, and the newer `gpt_5` / `codex_spark` variants already phrase
this generically ("websearch/browser"), so they were already correct.

### The lint

`tests/test_expert_prompts_only_name_granted_tools.py` scans every expert's own
prose files (`.txt` / `.md` / `.yaml`, excluding the config and matrix files)
for snake_case tokens matching a curated vocabulary, and asserts each is in that
expert's **merged** toolset. Two failure shapes: a tool that exists but isn't
granted, and a tool from `_REMOVED_TOOL_NAMES` that no longer exists at all —
the second is invisible to the registry check precisely because it is gone.

Runtime grants are modelled from the registry rather than allow-listed: the 38
tools already marked `grant: "code"` (officer, datasource, repo, product-guide,
session-task) are skipped from metadata. Only `approve_job`,
`return_job_with_feedback` and `loop_plan` need a local exception, because
`src/tools/registry.py:160-178` documents that they are deliberately left
unclassified — marking them would stop `evaluation: true` resolving to them. A
companion test fails if the registry ever does classify them, so that local set
shrinks rather than rots.

**Running it flagged two more instances of the same class**, both investigated
and both legitimate runtime grants rather than defects:
`centurion/persona.txt` names `notify_user` (registry says `grant: "code"`,
gated on `officer.enabled`, appended at `persistent_session.py:1653-1656`), and
`critic`'s prompts name `approve_job` / `return_job_with_feedback` (stamped into
the job's config fragment by `_critic_config_override`,
`src/api/orchestrator_client.py:1880`).

## Verification

- 15 lint cases green across all discovered experts; `browse_website` /
  `download_from_website` now appear nowhere in `config/` except one
  explanatory NOTE in `config/README.md`.
- Teeth confirmed both ways: swapping a granted name for `browse_website` fails
  the designer case, and swapping it for `cite_web` (a real tool the designer
  lacks) fails it too. Restored after each.
- Full suite green apart from the known local-env gaps.
- **Owed:** nothing on the cluster to check — this is prompt text, and it takes
  effect for new sessions/jobs on the next rollout. A spot check that a fresh
  designer job's system prompt says `browser_navigate` would confirm delivery.

## Why it wasn't fixed with the config change

The config fix was mechanical and verifiable by a test — a name either is in
`TOOL_REGISTRY` or is not. Rewording ten prompt files across four experts is a
semantic change to agent behaviour with no test to catch a bad rewrite, and it
lands during a feature freeze. Splitting it keeps the safe, testable half
independently shippable.

There is currently **no automated guard** against a prompt naming a tool the
config does not grant. That check is harder than the config one (prose, not a
list) but would be the real fix for the class — a lint that greps prompt files
for known tool names and asserts each is in that expert's merged toolset.
