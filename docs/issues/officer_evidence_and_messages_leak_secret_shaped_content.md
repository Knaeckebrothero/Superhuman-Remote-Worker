---
tags:
  - issue
  - officers
  - security
  - evidence
  - communication
status: open
priority: P2
created: 2026-08-15
aliases:
  - OC-05
  - officer content redaction gap
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_supervision_surface]]"
  - "[[officer_message_routing]]"
---

# Remaining unsanitized surfaces: completion reports, SITREP excerpts, notifications

**Status:** OPEN — RESCOPED 2026-08-15. Residue of audit finding **OC-05**.

**No longer a deployment blocker.** The sanitizer exists
(`src/shared/content_redaction.py`) and the two paths that carry the most
worker-authored text — evidence reads and routed message subjects/bodies — go
through it, reporting a count so a surface can say content was withheld rather
than silently shortening it.

What is left is wiring the same sanitizer into the remaining presentation
surfaces: raw completion reports, officer inbox/SITREP excerpts, escalation
context, and outbound notification bodies. Each is a call site, not a design
problem — the shared piece and its false-positive characterisation already
exist. Do this before relying on officer evidence triage at volume.

## Problem

Evidence reads call `services.kb_git_source.redact_git_error(text)` without supplying any
secret values. In that form it removes URL userinfo but not generic API keys, bearer tokens,
JWTs, passwords, or provider credentials. The generic secret-shape redactor in
`src/core/logging_config.py` is not used on officer evidence, completion reports, routed
worker messages, or notification bodies.

The raw completion-report endpoint also parses and returns inline worker JSON unchanged.
Consequently a worker, compromised tool, or prompt-injected source can copy a credential
into routine officer/user-visible content.

## Boundary

Raw immutable evidence may need to retain its checksum/source bytes. The officer and user
views must be sanitized at the trust boundary without pretending the underlying artifact
changed. Redaction is not a substitute for treating worker text as untrusted instructions.

## Required direction

- Establish one shared content-sanitization service for model/user presentation, not the
  logging formatter itself.
- Combine known runtime secret values (where safely available) with maintained generic
  secret-shape patterns.
- Apply it to evidence pages, completion reports, message subject/body, officer inbox/SITREP
  excerpts, escalation context, and outbound notification bodies.
- Return a machine-readable `redacted: true`/count signal so an officer knows evidence was
  withheld rather than silently altered.
- Never log the raw value while reporting that redaction occurred.

## Acceptance

- Fixtures covering bearer tokens, JWTs, `sk-`, GitHub/Slack tokens, password/key-value
  forms, URL userinfo, encoded known secrets, and multiline private-key fragments never
  reach officer tool text or user notification output.
- Benign IDs/hashes are not broadly erased; false-positive behavior is characterized.
- Inline and file-backed evidence use the same sanitizer.
- Completion-report, officer-first, officer-and-user, and user-direct paths have parity.
- Tests assert captured logs and error responses do not re-expose the original secret.

## Dependencies

This can land before the other routing repairs and should. It blocks live evidence and
officer message triage regardless of `auto_pull`.
