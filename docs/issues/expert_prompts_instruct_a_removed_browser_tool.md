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
**Status:** OPEN.
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
