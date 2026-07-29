---
tags:
  - issue
  - loops
  - backlog
  - orchestrator
  - logging
related:
  - "[[project_backlog_pipeline]]"
  - "[[loop_campaign_scheduling]]"
---

# Re-closing an already-closed backlog ticket logs a wrong cause and a false "mirror did not land"

**Status:** FIXED 2026-07-29 — committed on `develop` in `2362d2ce`.
**Not pushed and not deployed** (the owner pushes). `_rewrite_status` now
returns `(markdown, outcome)` naming the branch it took, so the guard can tell
the three byte-identical returns apart: `_ALREADY_SET` (a well-formed line
already at the target) logs at debug, skips the Gitea write, and returns True;
`_NOT_REWRITABLE` (no frontmatter, or no closing `---`) keeps the existing
warning and False. `main.py`'s caller warning is unchanged and still reads
correctly for the two cases that now reach it.
**Verification:** unit level only, which is the whole of this change — three
tests, one per outcome, in `tests/test_project_backlog.py::
TestCloseBacklogTicket`, each assertion proved non-vacuous by inversion, plus
two behavioural mutations: folding case 3 back into the no-op branch (the
pre-fix behaviour) fails the two re-close tests while the unterminated-
frontmatter test stays green, and folding case 2 into case 3 fails only that
test. Nothing to smoke on k3d beyond this — the fix moves one log line and one
bool. At the deployed INFO level a successful re-close is now simply quiet,
which is what this issue asked for; the debug line is there for anyone who
turns it up.
**Severity:** low — logging-only, no state consequence (see below).
**Component:** `orchestrator/services/project_backlog.py` (`_rewrite_status`,
`close_backlog_ticket`).

**Filed:** 2026-07-28, from the final re-review of the project-backlog pipeline.
Line numbers are develop @ 2026-07-28. Logging-only — **no state consequence.**

## Symptom

When `close_backlog_ticket` runs against a ticket whose note file already carries the
target status, the frontmatter rewrite produces byte-identical content. The no-op guard
added for a different case (a note with no frontmatter at all) catches this too, so the
close:

1. logs `… has no rewritable frontmatter status line (missing or malformed frontmatter)`
   — a **wrong cause**; the frontmatter is present and perfectly well-formed, it simply
   already says `resolved`,
2. returns `False`, which makes `orchestrator/main.py` log
   `close_backlog_ticket reported failure … the durable mirror did not land`,
3. …when the durable mirror is in fact already exactly correct.

## Why it is harmless (and why it should still be fixed)

The index `UPDATE` still runs on the same call, and the database remains authoritative
for work state, so the ticket ends up in the right state either way. Nothing is lost.

The cost is diagnostic: two log lines that actively lie about what happened, on a path
whose entire purpose is to make ticket closure auditable. The backlog feature was built
around the principle that a close which closes nothing must never be silent — a close
that succeeded but *reports* failure is the same problem wearing the opposite mask, and
it will burn someone's afternoon during a real incident.

## Reachability

- **Torn-advance heal window.** The close runs before `campaign=None` is persisted, so a
  heal that re-drives the advance can call it twice on the same ticket.
- **A user pre-setting the status** in the note file before the campaign disposes.

## Suggested fix

Distinguish the two cases the guard currently conflates:

- `_rewrite_status` found no frontmatter status line to rewrite → genuine no-op, keep
  today's warning and `False`.
- `_rewrite_status` found the line and it already holds the target value → **success**;
  log at debug ("already at `<status>`") and return `True`.

Add a test covering the idempotent re-close specifically — the existing no-op test only
exercises the missing-frontmatter case, which is why the two paths were conflated.
