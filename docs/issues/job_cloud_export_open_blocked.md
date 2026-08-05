# "Open cloud folder" opens nothing, and the folder isn't shared

**Status:** Popup + affordance FIXED; sharing gap OPEN (needs a one-time user login per cluster)
**Date:** 2026-08-04
**Reported from:** dev cluster (`cockpit.srw.works`), job `a6fa6f2a-9101-41f4-9ccb-5a7f362dc305`

## Symptom

Clicking **Open cloud folder** on a completed job flashed a toast ("Exported 1
file(s) to your cloud"), turned the button into a static **Exported** badge, and
never opened a tab. Brave showed a *Pop-ups blocked* bubble naming
`https://cloud.srw.works/apps/files/?dir=/…`.

## Three stacked defects

### 1. The popup was blocked, not skipped

`job-list.component.ts` called `window.open()` from the export POST's subscribe
callback. Orchestrator log for that click:

```
POST /api/jobs/a6fa6f2a-…/export-to-shared-folder 200 (11375ms)
```

11.4 s. Both Chromium (`blink::UserActivationState`, `kActivationLifespan = 5s`)
and Firefox (`dom.user_activation.transient.timeout = 5000`) require *transient
user activation* for `window.open()`, and a click grants exactly 5 seconds of
it. An `await` on an 11-second request always outlives that — this was never
going to work, on any browser, for any user. Nothing to do with site
reputation; the popup blocker has no such notion.

Nextcloud MKCOL/PUT round trips on this cluster run 1.5–2.5 s each, so the
export will keep exceeding the window even for one-file jobs.

### 2. The folder was created but never shared — silently

The endpoint does:

```python
resolved_user_id = await backend.ensure_user(...)
if resolved_user_id:
    await backend.share_session_folder(folder_handle, resolved_user_id)
```

…with no `else`. `NextcloudBackend.ensure_user` *cannot create accounts* — the
`user_oidc` app materialises them on the user's first browser login — so it
returns `None` until then, and the share is skipped without a trace. The
endpoint still returns `200` with a `browser_url`.

Verified on the dev Nextcloud at the time of the report:

```
occ user:list          → admin, agent-service        (no human accounts)
select count(*) oc_share → 0                          (nothing ever shared)
oc_filecache            → files/sessions/job-a6fa6f2a9101/output/digest.md
```

The export was real; the folder just lived in the agent's home, invisible to
its intended reader. Opening the returned URL would have shown an empty folder.

**This is a regression from the OpenCloud → Nextcloud migration.**
`OpenCloudBackend.ensure_user` POSTs a LibreGraph user when one is missing
(`opencloud.py`), so shares worked without a prior login. Nextcloud has no
equivalent admin path for OIDC-linked accounts.

Blast radius on a freshly migrated cluster is wider than job export: the
project groups (`project-<uuid>`) existed but were **empty**, and the persistent
session folder `sessions/1930dec9` was likewise unshared. Everything
user-facing in the new Nextcloud is gated on one login.

### 3. After exporting, there was no way to reach the folder at all

The row swapped the button for a non-interactive `Exported` badge.
`exported_folder_handle` is stored, but no endpoint resolved it to a URL for
jobs (only threads had `_resolve_cloud_session_url`), so the export response was
the one and only chance to open the folder — the chance the popup blocker ate.

## Fix (this change)

* **Export and open are two separate clicks.** Export copies and stops; the row
  then offers **Open cloud folder**, whose handler calls `window.open()`
  synchronously inside the click. Popup blockers never see it as unsolicited.
  Decision logic is the pure `jobCloudAction()` helper so the wide-layout button
  row and the narrow-layout overflow menu can't drift.
* **`exported_folder_url` on the job read model.** `_resolve_exported_folder_url`
  turns the opaque handle into a browser URL in `_with_cloud_review_mode`, so
  the Open button survives reloads and the badge is only the degraded state
  (cloud backend down → nothing to link to).
* **The share miss is reported.** The endpoint logs a warning naming the backend
  and user, and returns `shared: false`; the cockpit turns that into a warning
  toast that says the folder isn't shared yet, instead of an unqualified
  success.
* **The toast names the destination** — `Exported {{count}} file(s) to
  {{folder}}` rather than "to your cloud".

## Follow-up: folder names (2026-08-05)

`job-a6fa6f2a9101` identifies a folder without describing it, and the export
lands at the root of the user's own cloud drive, so ten exports produced ten
unreadable siblings. `_job_export_folder_name()` now slugs the job's first
description line and keeps a short id suffix for uniqueness —
`you-maintain-a-daily-digest-a6fa6f2a`. Accents fold (`Führe` → `fuhre`) rather
than being dropped, output is restricted to `[a-z0-9-]` (a safe single path
segment for both backends), truncation lands on a word boundary, and a
description that slugs to nothing falls back to the old `job-<id>` form.

The name must stay **deterministic** because the endpoint re-derives it to find
the folder on a re-sync. Two guards:

* `description` is fixed at job creation, so a job always maps to one name.
* A job that already has an `exported_folder_handle` reuses it verbatim instead
  of re-deriving. Without this, the rename would have stranded every
  already-exported folder (and its share) at the next re-sync — and it makes any
  *future* naming change safe by construction.

The response also carries `folder.path` (`/<name>`) so the toast can state where
the files actually are rather than just naming the folder.

Not done: the exports are still flat siblings at the cloud root. Grouping them
under one per-user parent (`SRW Jobs/…`) would need a per-user folder the agent
can write into and share once — sharing the current `sessions/` parent is not an
option, it holds every user's folders.

## Follow-up: collapse shared wrapper directories (2026-08-05)

A job whose single deliverable was `output/digest.md` produced an export folder
containing one folder containing one file — two clicks to reach one document.

`_common_dir_prefix()` computes the longest **whole-segment** directory prefix
shared by every file being copied, and the copy strips it. Both copy paths (the
declared-deliverables list and the `output/` tree fallback) now build a source
list first and run through one loop, so the rule is stated once:

| deliverables | lands as |
|---|---|
| `output/digest.md` | `digest.md` |
| `output/a.md`, `output/sub/b.md` | `a.md`, `sub/b.md` |
| `repo/src/a.py`, `repo/tests/b.py` | `src/a.py`, `tests/b.py` |
| `spec.yaml`, `output/report.md` | unchanged (nothing shared) |

Segment-wise on purpose: `out/` and `output/` share no directory, only a string
prefix. Structure that distinguishes files always survives — only the shared
head comes off. A declared-but-missing deliverable still counts toward the
prefix (it's filtered at copy time, not before), so a stale entry degrades the
result to *less* collapsing, never to wrong paths.

**Interaction with the no-prune re-sync.** v1 overwrites in place and never
deletes, and the prefix is recomputed per call, so re-syncing a folder that was
exported under the old shape leaves the old copy behind. Confirmed on k3d:

```
verify-deliverables-…/critic_verdict.json      <- new, collapsed
verify-deliverables-…/job_frozen.json
verify-deliverables-…/output/critic_verdict.json   <- stale, from the old shape
verify-deliverables-…/output/job_frozen.json
```

Only affects folders exported before this change AND re-synced after it; fresh
exports are clean. Fixing it properly means recording the applied prefix in the
handle's `vendor_meta` and deleting the old prefix directory when it changes —
deliberately not built here, because it puts a delete path into a sync endpoint
that users may also have dropped their own files into.

## Still open

`ensure_user` returning `None` is *reported* now, not *fixed*. The user must
sign in to the cloud (`https://cloud.srw.works`, Keycloak) once per cluster
before any share can target them; a re-sync after that shares the folder.

Options if that hand-off proves too sharp:

1. **Provision on Keycloak login instead of cloud login** — have the
   orchestrator drive an OIDC-backed account creation at first cockpit login.
   Needs a Nextcloud-side mechanism that survives `user_oidc` reconciliation;
   `occ user:add` creates a *Database*-backend account that the OIDC login may
   not adopt (`docs/done/nextcloud_oidc_username.md` documents the related
   uid-mapping trap).
2. **Fall back to a public link share** for the export folder when no account
   resolves — reachable, but sidesteps per-user access control.
3. **Block the export with a 409** and an actionable "sign in to the cloud
   first" message, rather than copying into a folder nobody can read.

(3) is the smallest honest behaviour; (1) is the right end state.
