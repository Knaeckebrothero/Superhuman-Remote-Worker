---
tags:
  - issue
  - orchestrator
  - sessions
  - tools
  - workspace
related:
  - "[[session_create_tool_toggles_cannot_enable_a_group]]"
---

# A `.zip` attached to a session is unreachable — the worker path extracts archives, the session path does not, and `read_file` reports the failure as a UTF-8 codec error

**Status:** OPEN, diagnosed 2026-08-01. Not started.
**Severity:** medium — the user's data is in the workspace and cannot be read by
any means available to a default session. Fully reproducible: attach any archive
to any session.
**Component:** `orchestrator/services/thread_uploads.py`,
`src/tools/workspace/files.py` (`read_file`), `src/agent.py` (`_extract_zip`).

**Motivating incident:** dev session `1930dec9-181d-4fd5-a030-90b3d0b363d6`,
2026-08-01. The user attached
`uploads/drive-download-20260801T200341Z-1-001.zip` containing their existing
job-application letter and asked the agent to put it on the canvas. The agent
called `read_file` on it and received:

```
Error: 'utf-8' codec can't decode byte 0xdd in position 12: invalid continuation byte
```

It then searched the workspace for an unzip capability, found none, and asked
the user to unpack the archive by hand. The user replied "Nimm einfach shell
commands" — and the session had no shell, for reasons tracked separately in
`[[session_create_tool_toggles_cannot_enable_a_group]]`.

## Root cause

### 1. The capability exists, but only on the worker path

`src/agent.py:4067` defines `_extract_zip(zip_path, dest_dir_relative,
job_logger)`, which walks `zipfile.ZipFile.infolist()`, skips directories,
dotfiles and `__MACOSX`, and writes each entry through the workspace backend.
It is called at `src/agent.py:2625`, `:2676` and `:2718`, all on the worker-job
attachment path, extracting into `documents/`.

`orchestrator/services/thread_uploads.py` — the session path — writes bytes
verbatim into `<workspace>/uploads/` over either SFTP (`_sftp_write_files`) or
the object store (`_virtual_write_files`). There is no suffix check and no
extraction on either transport.

So the *same archive* is transparently expanded for a worker job and left inert
for a session. Nothing in the UI signals the difference.

### 2. `read_file` has no binary branch, so the failure is misreported

`src/tools/workspace/files.py` special-cases images (`:774`), audio (`:784`) and
visual documents — PDF/PPTX/DOCX (`:797`). Everything else falls through to the
text path at `:821`:

```python
content = workspace.read_file(path)   # strict UTF-8 decode
```

`UnicodeDecodeError` subclasses `ValueError`, so it is caught by the handler at
`:879-880` and returned as `f"Error: {str(e)}"` — the raw codec message. The
tool never says "this is a binary file" or "this is a zip archive", so a
perfectly correct refusal reads like an encoding bug in the file.

Note the near miss: a `.docx` would have worked, because DOCX is in
`_is_visual_document`. Only archives (and other binaries) fall into the gap.

## Why this went unnoticed

Session attachments are a comparatively recent surface. The design doc behind
`thread_uploads.py`
(`[[session_uploads_never_implemented_for_lite_workspace_tiers]]`, in
`docs/done/`, referenced from the module's own test header) was concerned with
getting bytes
into the workspace *at all* across four workspace tiers — the virtual tier had
no path whatsoever and 409'd with a transient-sounding message. Reaching parity
with the worker path's *content handling* was never in scope, and the worker's
`_extract_zip` is a private method on the agent, invisible from the orchestrator
process.

## Proposed fix

1. **Extract at the session upload seam.** In `thread_uploads.py`, expand
   `.zip` entries into `uploads/<stem>/…` for both transports, mirroring the
   worker behaviour. Harden while porting — the worker version's protection
   against `../` traversal is *incidental* (`any(part.startswith("."))` happens
   to catch `..`), and there is no entry-count or uncompressed-size cap. Both
   should be explicit here, since this seam is directly user-facing.
2. **Give `read_file` a binary branch.** Return an entry listing for archives
   and an honest `[binary file: <name>, N bytes]` for everything else, instead
   of leaking a codec error. This fixes the whole class, not just zip.

Neither needs a new tool, which keeps the agent's menu — and its context cost —
unchanged.

## Verification owed

- Unit: a `.zip` uploaded to a thread lands as extracted files under `uploads/`,
  on both the SFTP and object-store transports.
- Unit: traversal (`../`), zip-bomb entry counts, and oversized members are
  rejected rather than written.
- Unit: `read_file` on a binary returns the binary/archive message, not a codec
  error.
- Live gate on dev: attach an archive to a session, confirm the agent reads a
  member without shell.
