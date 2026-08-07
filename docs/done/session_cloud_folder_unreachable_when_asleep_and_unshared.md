# An asleep session hides its cloud button, and the folder it points at was never shared

**Status**: DONE — shipped 2026-08-06, verified on k3d 2026-08-07.
**Filed**: 2026-08-06
**Observed on**: main dev cluster, thread `1930dec9-181d-4fd5-a030-90b3d0b363d6`
(ended, Nextcloud backend). Verified on k3d against thread `5833c729`.
**Severity**: the user could not reach a finished deliverable by any route. The
chat view offered no cloud button at all, and the button in the sessions list
opened an empty folder with no explanation.

The user asked for one file out of an ended session. Getting to it surfaced three
independent defects stacked on the same path, plus one unrelated live outage.

| # | Defect | State |
|---|--------|-------|
| 1 | Files button hidden on an asleep/ended session | **Fixed** — `8da4b27c` |
| 2 | Session folder never shared → the URL resolves to nothing | **Fixed** — swept by `af1ed9f8` |
| 3 | Content sync into the session folder is inconsistent | **Open** — see below |
| 4 | Helm rendered a byte limit in scientific notation → orchestrator crashloop | Fixed independently by `b2881fe1` |

## 1. The button was gated on the wrong thing

`persistent-chat.component.ts:735` wrapped the entire header action cluster in
`@if (chat.isConnected())`. That gate is correct for settings, view, citations,
IDE and Disconnect — they all drive a live agent. It is wrong for Files, which
only calls `window.open` on an external URL that `loadThreadMeta` has already
resolved over plain REST. The proof it was available: the session *title* renders
fine on a disconnected thread, and it comes from the same REST call.

Fix: an `@else` branch rendering Files (icon-only when `headerCompact()`, full
button otherwise). The connected path is untouched.

The sessions list had no such gate (`sessions-page.component.ts:200`), which is
why that was the only reachable route and why the bug read as "the chat view is
missing a button" rather than "the gate is wrong".

## 2. The folder existed but was never shared

`_setup_main_cloud` creates `sessions/<id8>` under the `agent-service` account and
shares it with the owner, but the share is guarded on `resolved_user_id` being
truthy. With no cloud account to share with, that branch is skipped silently: the
folder is created and its handle stamped, `main_cloud_share_handle` stays NULL.

`get_session_folder_browser_url` (`nextcloud.py:1077`) then builds
`{public_url}/apps/files/?dir=/<id8>` — the **basename**, on the assumption a share
placed the folder in the owner's root. Without the share that path does not exist
for them, and Nextcloud renders an empty view rather than an error. That is exactly
what the user saw.

Timeline on this deployment, which is what made it survivable elsewhere and fatal
here:

| When | What |
|---|---|
| 08-03 20:53 | `sessions/1930dec9` created during the Nextcloud cutover |
| 08-04 13:24:53 | owner's OIDC account first resolves (`users.cloud_identity` cached) |
| 08-04 13:25:11 | first successful share, 18 s later |

Every folder created in that ~16.5 h window was orphaned. Four of the five threads
recovered on their own because a later **resume** ran the `needs_share_only` retry
(`main.py:27180`). `1930dec9` had ended and was never resumed, so it never did.

Fix: `scripts/backfill_session_folder_shares.py` — dry-run by default, idempotent,
routed through `backend.share_session_folder` so the leaky bucket that keeps us
under Nextcloud's 20-shares/10-min ceiling still applies. Run 08-06: one candidate,
now share id 6; re-run reports zero.

## 3. Still open: content sync is inconsistent, not uniformly absent

Sharing fixed reachability, not content. `sessions/1930dec9` held a 48-byte
placeholder `README.md` and one `skills/` entry — a frozen snapshot from session
start — and the agent's actual output never reached it.

But this is **not** a blanket "sessions never sync". Measured across all five
Nextcloud session folders on 2026-08-07, cross-checked against whether each session
ever called a file-writing tool (`write_file` / `edit_file` / `create_directory`,
counted from `llm_requests`):

| Thread | Turns | File writes | Synced entries | Verdict |
|---|---|---|---|---|
| `5833c729` | 16 | yes | 48 (incl. a 27 KB `output/` deliverable) | correct |
| `c90f83b7` | 2 | yes | 44 (incl. `documents/external/`) | correct |
| `4ad107ad` | 7 | **0** | 0 | correct — nothing to sync |
| `00ae0977` | 2 | **0** | 0 | correct — nothing to sync |
| `1930dec9` | 12 | **11 files** | 22, *all from the manual restore below* | **broken** |

Four of the five are explained: two synced their deliverables correctly, and two are
empty because those sessions never wrote a file at all. An empty folder is not
by itself evidence of a sync defect — check the write count first.

That leaves exactly **one** unexplained failure, this thread. It is not a general
"sync is inconsistent" fault, and the scope is much narrower than it first appeared.
What makes `1930dec9` resist the obvious explanations:

- Not simply pre-cutover. It was created 08-01 under OpenCloud — but so was
  `5833c729`, which syncs fine.
- Not simply "never ran after the folder existed". Its folder was created 08-03
  20:53 and it ran turns on 08-04 08:34–08:37, after that, writing five files in
  the process. None reached the folder.

The plausible remaining difference is that `5833c729` was re-attached repeatedly
through 08-06 while `1930dec9`'s last attach predates the cutover, so its sync
config was never rebuilt against the Nextcloud target — but **this was not
confirmed**, and the 08-04 turns argue against the simple form of it. Tracked in
`docs/issues/session_deliverables_in_workspace_output_not_in_cloud_files_button.md`.

## Recovering the lost files

The workspace was gone: `pvc-agent-s-1930dec9-181` survives teardown but came back
holding only `lost+found`, dated 08-05 09:58. Mount it read-only in a throwaway pod
before assuming loss — it is cheap and occasionally the files are there.

They were reconstructed from the audit store instead. `agent_audit` has **zero**
rows for persistent threads, but `llm_requests.job_id` carries the thread UUID, and
the last row's `request->'messages'` is the whole accumulated conversation. Replaying
its `write_file` **and** `edit_file` calls in message order rebuilt all 11 files.

Two checks made that trustworthy rather than merely plausible:

- **Completeness**: counting every tool name in the transcript showed only
  `write_file` (11), `edit_file` (3), `create_directory` (6) — no shell, move or
  delete, so nothing mutated files outside the replay.
- **Correctness**: `canvas_snapshots.source_version` stores a sha256 of any file the
  agent presented on canvas. The rebuilt README matched byte-exact (1412 B).

Restored into the now-shared folder via `backend.put_session_file`, which MKCOLs
parents on the way.

## 4. Found in passing: the metering byte limit crashlooped the orchestrator

`INFRASTRUCTURE_METERING_MAX_SNAPSHOT_BYTES` rendered as `6.7108864e+07`. Helm parses
every `values.yaml` number as float64, and Go's shortest-float formatting switches to
exponent notation once the decimal exponent reaches 6 — so any value ≥ 1,000,000.
`50000` renders fine; `67108864` does not. Every other large value in the chart
already piped through `| int` / `| int64`; line 279 was the sole omission. Fixed
independently as `b2881fe1` while this investigation was running, with a regression
test in `tests/test_infrastructure_metering_helm.py`.

## Related

`docs/issues/cloud_folder_invisible_until_owner_signs_into_cloud.md` (defect 2's
root cause; still open for the unbuilt user-facing affordance) ·
`docs/issues/session_deliverables_in_workspace_output_not_in_cloud_files_button.md`
(defect 3) · `docs/done/session_resume_cloud_sync_race_late_provision.md` (the
resume-path retry this leaned on)
