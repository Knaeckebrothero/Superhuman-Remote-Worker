---
tags:
  - issue
  - officers
  - evidence
  - multimodal
  - tooling
status: open
priority: P1
created: 2026-08-15
aliases:
  - ES-01
  - officer screenshot blind spot
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_supervision_surface]]"
  - "[[officer_backlog_pools]]"
---

# A headless background officer cannot inspect screenshot evidence

**Status:** OPEN — E4/E5 acceptance gap. Audit finding **ES-01**.

## Problem

`job_evidence.read_evidence_entry()` verifies screenshot bytes and returns a
`job_repo_file` view pointer. `format_evidence_read()` converts that object into plain text
telling the caller to open the existing job-file viewer. Descriptor-backed officer tools
return a string, and the background officer has neither a Cockpit viewer nor object-plane
file tools.

The officer can inspect checksum, media type, and provenance but not the pixels needed for
the executor screenshot rubric. This does not satisfy the supervision design’s
“screenshot and report live read” gate.

## Decision/direction

Choose one honest contract:

1. **Bounded multimodal evidence:** the officer adapter returns a typed image attachment
   resolved solely from the opaque evidence ID at its pinned revision, with size/type/count
   ceilings and no model-selected path.
2. **Metadata-only officer:** explicitly remove screenshot-inspection claims and require a
   tester/recon job to turn images into a bounded textual report.

Do not grant arbitrary file/repository access to solve this; that would break the knowledge
plane’s object-plane ceiling.

## Acceptance

- A real headless officer turn consumes a known screenshot and makes a decision based on a
  visible feature in the image, not its filename/metadata.
- Cross-project IDs, path traversal, revision substitution, oversize files, bad media, and
  checksum mismatch fail closed.
- Image bytes do not appear in logs or ordinary string context unexpectedly.
- Models without multimodal support receive an explicit unavailable/fallback result.
- Tester/recon fallback remains available when evidence is absent or unsupported.

## Dependencies

Apply the presentation sanitizer from
[[officer_evidence_and_messages_leak_secret_shaped_content]] to textual metadata/report
surfaces. This issue must not widen the object-plane grant.
