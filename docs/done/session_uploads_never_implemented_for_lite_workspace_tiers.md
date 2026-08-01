---
tags:
  - issue
  - fix-spec
  - sessions
  - workspace-lifecycle
  - uploads
  - lite-backend
---

# Issue — attachment upload was never implemented for the lite workspace tiers

**Status:** Found 2026-07-30 on dev while diagnosing session `e6e6d412`.
**FIXED 2026-07-30** (commit `94eb3e71` on `develop`) for `virtual`; `none` now
refuses honestly. Unit-tested (`tests/test_thread_uploads.py`, 26 cases) and
**k3d live-gated**, then **confirmed on dev 2026-08-01** against the original
repro payload (session `5833c729`) — see "Live gate" and "Dev confirmation".

**One line:** `POST /api/persistent/threads/{id}/uploads` only knows how to SFTP
into a workspace container or VM, so on `virtual` — the **default** session tier,
which has no workspace pod by design — every attachment dies with
`409 Workspace is not ready — try again in a moment`, a message that will never
come true.

## What actually breaks

Session `e6e6d412` on dev, created 2026-07-30 12:40:46Z with
`config_override.workspace.backend = "virtual"`. Ten files (~1.9 MB of PDFs plus
a `.cql`) queued in the composer, then send:

```
12:40:47.675  Thread e6e6d412…: lite workspace backend — no workspace pod provisioned   (main.py:22488)
12:48:02.520  OPTIONS /api/persistent/threads/e6e6d412…/uploads 200 (0ms)
12:48:18.648  POST    /api/persistent/threads/e6e6d412…/uploads 409 (9315ms)
```

The composer surfaces the server `detail` verbatim
(`api.service.ts:1040-1051`) and **refuses to send the message at all** —
`sendMessage` returns `false` before dispatch (`persistent-chat.service.ts:2205-2212`).
The typed text stays in the box, the ten chips stay queued marked `FAILED`, and
pressing send again re-uploads all 1.9 MB and re-fails. There is no state in
which this session accepts a file.

Not a limit: 10 files against `MAX_FILES_PER_REQUEST = 20`, largest 644 KB
against `MAX_FILE_SIZE = 100 MB`.

## Root cause

`_resolve_target()` (`services/thread_uploads.py:89-141`) recognises exactly two
destinations:

```python
if vm_ctx.get("status") == "ready":              # metadata.vm — KubeVirt VM
    ...
elif ws_ctx.get("status") == "ready":            # metadata.workspace_container — sandbox pod
    ...
if not host:
    raise ThreadUploadError(409, "Workspace is not ready — try again in a moment")
```

The lite tiers (`virtual`, `none` — `src/core/backends/factory.py:24`) have
neither and never will. Thread create logs the intent explicitly and skips every
provisioning path below it (`main.py:22488`). So `host` is `None`, and the only
error the function can produce for that case is the transient one.

The module was written SSH-first — its docstring opens *"Upload files into a
persistent thread's live workspace via SFTP"* — and when the lite tiers landed
(`docs/features/no_workspace_agent_mode.md`) this endpoint was never revisited.
Grepping that design doc for `upload` returns nothing about user→session file
ingress; the tier was designed around what the *agent* can reach, and the
inbound path was missed.

### This is not the `workspace_container`-presence misread

Worth stating because the same overloaded key is involved. Thread `e6e6d412`'s
metadata *does* carry a `workspace_container` — Gitea coordinates
(`repo_name`, `git_remote_url`) written by `_setup_gitea` for every tier —
alongside a healthy `_workspace_binding`:

```json
"_workspace_binding": {
    "kind": "virtual",
    "backing_id": "rclone:f5e94d4a8868…",
    "generation": "1c2e86d3-bf97-4ce4-9028-9fc9f91044ac"
},
"workspace_container": {"repo_name": "thread-e6e6d412", "git_remote_url": "…"}
```

`_resolve_target` checks `status == "ready"` rather than key presence, so unlike
`suspend_thread_workspace` (see
`docs/issues/workspace_suspension_infers_tier_from_metadata_presence.md`) it
reaches the *right* conclusion — there is no pod. It then reports that correct
conclusion as a timing problem. Fixing the overload does not fix this; the
missing feature does.

### The blast radius is the default tier

`SESSION_DEFAULT_WORKSPACE_BACKEND = "virtual"` (`main.py:3526`). Any session
created without explicitly picking the sandbox tier lands here. Attachments,
camera capture, and voice messages all funnel through this one endpoint
(`api.service.ts:1011-1037`), so all three are dead on the default.

### The rejection is late and expensive

`upload_files_to_thread` reads every `UploadFile` body into memory
(`main.py:26206-26214`) *before* `upload_files_to_thread_workspace` resolves a
target (`thread_uploads.py:277`). The 9.3 s above is 1.9 MB being transferred
and buffered before anything asks whether there is somewhere to put it. Nothing
about the decision depends on the bytes.

**Caveat on fixing this by reordering:** with `files: list[UploadFile] = File(...)`,
FastAPI parses the multipart body during dependency solving, i.e. *before* the
handler runs. By the time any line of ours executes, the client has already
uploaded everything and Starlette has spooled it. Reordering inside the handler
therefore saves the second, in-memory copy — not the transfer. A genuinely early
reject has to happen client-side, which is the real argument for the composer
knowing the tier. Moot for `virtual` now that uploads work there; it only bites
`none`, which is why the shipped fix reorders inside the handler and leaves the
client alone.

### The composer does not gate on tier

The attach button is unconditional (`persistent-chat.component.ts:1706`), so the
UI cheerfully accepts ten files into a session that structurally cannot hold
them, and only says so after the upload. **Left as-is deliberately** — once
`virtual` works, `none` is the only tier that refuses, and conditional UI for one
rarely-used tier costs more than the honest error message buys.

## Why the destination already exists

The virtual tier is not fileless — only this path is.

- **Agent side:** `VirtualWorkspaceBackend` (`src/core/backends/virtual.py`)
  implements the full surface — `read_file`, `write_file`, `append_file`,
  `mkdir`, `list_dir`, `walk`, `search_files`, `move`, `copy`, `delete_file`.
- **Orchestrator side:** it already writes into that store for Canvas —
  `_write_virtual_default` (`services/canvas_files.py:1071`) does
  `RcloneObjectStore.put` + `copy` at key `threads/<thread_id>/<path>`.
- **Binding:** `ensure_virtual_thread_workspace_binding`
  (`services/workspace_binding.py:70-83`) binds the durable namespace at thread
  create — present and current on `e6e6d412`, so a fix works on existing
  sessions, not just new ones.
- **Deployment:** dev orchestrator ships `/usr/bin/rclone` and has
  `VIRTUAL_WORKSPACE_RCLONE_*` configured (s3 → MinIO, root `srw-workspaces`).

**The prefixes already agree.** The orchestrator writes
`threads/<thread_id>/<path>`, and the agent's lite backend is attached with
`prefix=f"threads/{thread_id}/"` at both session-attach seams
(`main.py:3319` warm-pool, `main.py:20504` cold/dedicated, via
`_inject_lite_workspace_config` at `main.py:3844`). So an orchestrator-side
write of `uploads/<name>` lands exactly where the agent's
`read_file("uploads/<name>")` looks — which is where the cockpit's existing
`[Attached files in uploads/: …]` hint (`persistent-chat.service.ts:2229-2233`)
already points it. No new contract between the two sides.

## What shipped

| Where | What |
|---|---|
| `services/workspace_binding.py` | `resolve_virtual_thread_backing(thread) -> (spec, prefix)` + `VirtualBackingUnavailable(reason, detail)` with reasons `not_configured` / `backing_changed` / `transport_missing`. Read-only by design: it never binds, so an ordinary file write can't mutate workspace identity |
| `services/thread_uploads.py` | `resolve_thread_upload_destination(thread)` returns `_SshTarget` \| `_VirtualTarget(spec, prefix)` and raises a **truthful** error per tier. The transient 409 survives only on tiers that actually provision a pod/VM |
| `services/thread_uploads.py` | `_virtual_write_files()` — one store for the batch, **one** `list()` seeding a taken-set, `put()` per file, in a single `asyncio.to_thread`. Store is injectable for tests |
| `services/thread_uploads.py` | `_name_candidates()` shared by both transports, so SFTP and object-store uploads resolve collisions to identical names |
| `main.py` (uploads endpoint) | Resolves the destination before materializing bodies; the tier errors flow through the existing `ThreadUploadError` mapping, so the response contract is unchanged |
| `tests/test_thread_uploads.py` | New, 26 cases over `InMemoryObjectStore` — no rclone, no SSH |

No new infrastructure, no schema change, no chart change, **no cockpit change** —
the composer already renders the server's `detail` verbatim.

### Deliberately not done

**The Canvas guard was not de-duplicated.** The fix spec originally called for
lifting the identical guard out of `canvas_files._write_virtual_default` into the
shared helper. That is wrong as written: the Canvas tests monkeypatch
`canvas_files.virtual_workspace_rclone_spec` and `canvas_files.shutil.which`
*by name* (`tests/test_canvas_slice1_backend.py:905-1030`), so moving the guard
silently makes those patches inert. ~12 duplicated lines beat rewriting a
load-bearing Canvas path. Both sites derive identity from the same
`virtual_thread_backing_id`, so they cannot disagree. **Follow-up:** adopt the
helper in `canvas_files` together with its test monkeypatch targets, as its own
change.

Two cockpit refinements were also left out on purpose, both only reachable on
`none`: unblocking the typed message when an upload fails
(`persistent-chat.service.ts:2205-2212` refuses the whole send), and a
client-side tier pre-check that would reject before transferring bytes.

## Live gate

Run on k3d 2026-07-30 against thread `77d84753` (`virtual`, Garage-backed).
Three files posted in one request, two deliberately sharing a name:

```
HTTP 200
uploads/Themen Proposal.pdf     10 bytes
uploads/Themen Proposal_1.pdf   11 bytes   ← intra-batch collision resolved
uploads/notes.md                 7 bytes
```

1. **Objects exist with the right bytes.** `rclone lsl` on the thread prefix
   shows all three; `rclone cat` returns `first copy` / `second copy` / `# notes`
   in the right places — the second same-named file did not clobber the first.
2. **The agent's own class reads them back.** Driving
   `VirtualWorkspaceBackend(store, prefix="threads/77d84753-…/")` — the exact
   class the agent runs — against the live store:
   `is_dir("uploads")` → `True`, `list_dir("uploads")` → all three,
   `read_file("uploads/notes.md")` → `'# notes'`. The prefix agreement is now
   asserted, not inferred.
3. **The prefix is provably the agent's own namespace**: `uploads/` landed
   alongside that thread's existing agent-written `tools/*.md`, under one prefix.
4. **`none` refuses honestly.** Posting to a `none`-tier thread returns
   `409 This session has no workspace, so files cannot be attached to it. Start a
   session with a workspace to upload files.` — no "try again in a moment".

## Dev confirmation — the original repro, now working

Session `5833c729` on dev (2026-08-01), `virtual` tier, the **same payload that
produced the 409**: ten files, ~1.95 MB, thesis PDFs plus `schema.cql`.

```
08:49:55  POST /api/persistent/threads/5833c729-…/uploads 200 (3267ms)
```

Against the original `409 (9315ms)`. Both open questions are now answered:

1. **The hint reads correctly for a shell-less agent.** It did not try to `cat`
   anything. It called `list_files` on `uploads` (all ten returned), then
   `get_document_info` on the PDFs, then `read_file("uploads/…")` on six — the
   exact surface `VirtualWorkspaceBackend` supports. PDF text extraction works
   through the object store too. No tier-aware rewording needed.
2. **Latency is a non-issue.** 3.3 s for ten objects, one rclone subprocess
   each. The per-object subprocess cost does not dominate at this batch size.

Two incidental confirmations worth keeping:

- **Collision renaming preserved real content.** Both duplicate-named pairs
  (`Themen Proposal.pdf` / `_1.pdf`, `expose_thesis_A_kg_quality_criteria.md.pdf`
  / `_1.pdf`) turned out to be genuinely *different* documents that shared a
  display name — different sizes and different extracted content. The agent read
  both as distinct sources. Overwriting would have silently destroyed one.
- **The tier does the full round trip.** Upload in → read → deliverable written
  to `output/expose_thesis_A_schema_fitness_revised.md` → rendered in Canvas.
  Uploads were the missing leg, not the whole leg.

Unrelated snag observed in the same run: the agent's first `cite_document` batch
was rejected with "You must read `skills/cite-as-you-write/SKILL.md` before using
cite_document", costing a round of tool calls. That is skill gating, not uploads.
