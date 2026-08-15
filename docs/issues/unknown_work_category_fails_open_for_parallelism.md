---
tags:
  - issue
  - officers
  - backlog
  - concurrency
  - hardening
status: open
priority: P2
created: 2026-08-15
aliases:
  - OC-09
  - unknown category parallel-safe
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_backlog_pools]]"
---

# Unknown work categories fail open for parallelism

**Status:** OPEN/LATENT. Audit finding **OC-09**.

## Problem

`orchestrator/services/work_categories.py::allows_parallel(category)` returns true for
anything not normalized to `executor`. Unknown strings and `None` therefore receive the
less restrictive researcher/tester concurrency rule.

There is no production caller today, so this is latent. The helper’s name and export make a
future caller likely, and the unsafe default would silently weaken executor serialization
for malformed or newly introduced categories.

## Required direction

- Make unknown/absent categories fail closed: return false or raise `UnknownCategory` at a
  boundary that cannot be ignored.
- Prefer an explicit exhaustive mapping from the three recognized categories to policy.
- Require new categories to add tests and a deliberate serialization decision.
- Audit adjacent helpers for the same “analysis is closed, execution is open” assumption;
  that law is appropriate for loop role mapping, not for untrusted stored category data.

## Acceptance

- `researcher` and `tester` allow their documented concurrency; `executor` denies it.
- `None`, blank, typo, mixed invalid tag, and future unknown value fail closed.
- Production callers cannot swallow `UnknownCategory` and then assume parallel-safe.
- A test pins the distinction between role-to-category fallback and category-policy input.

## Dependencies

No release dependency while the helper has no production caller. Close before using it in
admission, executor disposition, or UI policy.
