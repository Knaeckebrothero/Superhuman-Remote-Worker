# Rclone Cloud Mount - Dev Cluster Verification Runbook

Manual end-to-end verification for
`docs/features/rclone_cloud_mount.md`, especially the Phase 1 v4/v5 default
path. This covers the real Kubernetes workspace runtime, FUSE/rclone, Cockpit
session creation, REST input, supervised approval, and the default-project
fallback behavior that unit tests cannot fully exercise.

Target time: **30-45 minutes** after the dev image/chart has rolled out.

---

## 0. What Was Implemented

The workspace cloud data plane now uses `rclone mount` instead of eager
WebDAV clone/sync when `cloud.workspaceDriver=rclone_mount`.

Core behavior:

- Orchestrator keeps the main-cloud abstraction as the control plane and emits a
  generic `cloud_mount` payload for rclone-capable runtimes.
- `thread_mounts` remains the source of truth for allowed cloud scope.
- Agent runtime starts `RcloneMountManager` before shell/tools initialize.
- Mounts live under `/cloud/<name>`.
- `/workspace/cloud` points to `/cloud/home` for the single default mount, or
  becomes a symlink directory for multiple mounts.
- Active rclone sessions skip legacy initial `pull_all()` and the legacy
  `nc_session_folder` sync coordinator.
- Workspace container images include `rclone`, `fuse3`, `/cloud`, and FUSE
  security context support.
- The Helm default workspace driver is now `rclone_mount`.
- Nextcloud rclone specs use WebDAV behind the generic cloud abstraction.
- Default user-home mounts require explicit safe credentials; otherwise the
  session-folder fallback is mounted instead.

Guardrails and status:

- `.cloudignore` and deployment/session default ignores are compiled into rclone
  filters before mount startup.
- `srw_cloud_status` is available in mounted sessions.
- Obvious broad cloud scans such as recursive `grep`, `rg`, `du`, archive, and
  recursive copy commands are blocked or warned based on config.
- Cloud-touching shell and file operations check the configured hard VFS cache
  limit before starting.

Live k3d validation also fixed:

- Workspace NetworkPolicy egress to bundled Nextcloud/OpenCloud pods.
- Session create/prepare race that could provision duplicate agent pods.
- Cockpit startup readiness when `/connection` is the first ready signal.
- REST input starting the persistent loop without relying on a legacy WS start.
- Expert model prefill no longer suppressing backend system-default model
  injection.
- Cockpit permission approvals now prefer durable REST
  `/approve/{approval_id}` with WS fallback.

Known remaining gaps:

- No per-command hydration delta accounting while a process is running.
- No pause/resume process-group workflow for commands that exceed a byte budget.
- No cloud-wide index/vector/regex search layer yet.
- The hard cache guard is a preflight guard; it does not police every byte during
  a long-running command after the command starts.

---

## 1. Prerequisites

Set the target cluster variables:

```bash
export KUBE_CONTEXT=<dev-context>
export NAMESPACE=<dev-namespace>
```

The dev deployment should contain the rclone cloud mount implementation and the
Phase 1 v5 fixes. Confirm the chart/runtime config:

```bash
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" get deploy,sts,svc
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" get configmap -o name | sort
```

Expected deployment settings:

- `cloud.workspaceDriver` / `CLOUD_WORKSPACE_DRIVER` is `rclone_mount`.
- Workspace image contains `rclone` and `fusermount3`.
- Workspace pods can access `/dev/fuse`.
- Dev NetworkPolicy allows workspace pods to reach the configured main-cloud
  backend.
- If testing default user-home mount on Nextcloud, explicit rclone user-home
  credentials are configured. If not configured, fallback to the session folder
  is expected and should be treated as a pass for fallback behavior.

Do not print or paste secret values while running this runbook.

---

## 2. Static Cluster Checks

### 2.1. Confirm workspace driver and FUSE env

```bash
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" get deploy/srw-orchestrator \
  -o jsonpath='{range .spec.template.spec.containers[?(@.name=="orchestrator")].env[*]}{.name}={.value}{"\n"}{end}' \
  | grep -E 'CLOUD_WORKSPACE_DRIVER|CLOUD_RCLONE_ALLOW_CONTAINER|WORKSPACE_FUSE'
```

Pass criteria:

- `CLOUD_WORKSPACE_DRIVER=rclone_mount`.
- Container rclone/FUSE is not disabled.

### 2.2. Confirm workspace image dependencies

After a session workspace pod exists, run:

```bash
WS=<workspace-pod-name>

kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" exec "$WS" -- rclone version
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" exec "$WS" -- which fusermount3
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" exec "$WS" -- test -e /dev/fuse
```

Pass criteria:

- `rclone version` succeeds.
- `fusermount3` exists.
- `/dev/fuse` exists.

---

## 3. Cockpit Session Smoke

Use the dev Cockpit UI.

1. Open **Sessions**.
2. Create a **New Session**.
3. Select the **default project**.
4. Select the **Developer** expert.
5. Name it `rclone dev cluster smoke`.
6. Create the session.

Pass criteria:

- The session reaches `Connected`.
- The startup card clears without a reload.
- The composer is usable.
- Header/model metadata shows the dev system default model, not an expert YAML
  fallback model unless that is intentionally the configured default.

Capture the thread id from the URL:

```bash
export THREAD_ID=<thread-id-from-url>
```

---

## 4. Provisioning and Pod Shape

Find the session pods:

```bash
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" get pods \
  -l "srw.io/thread-id=$THREAD_ID" --show-labels

kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" get pods \
  -l "srw/thread-id=${THREAD_ID:0:13}" --show-labels
```

If labels differ in the dev chart, use:

```bash
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" get pods --show-labels \
  | grep "$THREAD_ID"
```

Pass criteria:

- Exactly one session agent pod for the thread.
- Exactly one workspace pod for the thread.
- Both pods become `Running`.
- No duplicate agent pod appears during create/prepare.

Check orchestrator logs:

```bash
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" logs deploy/srw-orchestrator \
  --since=10m | grep "$THREAD_ID"
```

Expected useful signals:

- `Thread create: injected system default chat model: ...`
- `Agent pod created: ...`
- If `/prepare` raced with creation:
  `agent pod already provisioning - waiting for binding`
- No second `Agent pod created` line for the same thread.

---

## 5. Mount Verification

Set the workspace pod:

```bash
export WS=<workspace-pod-name>
```

Check the mount and workspace shortcut:

```bash
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" exec "$WS" -- \
  readlink /home/agent-host/workspace/cloud

kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" exec "$WS" -- \
  findmnt -T /cloud/home

kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" exec "$WS" -- \
  su agent-host -c "ls -la /cloud/home"
```

Pass criteria for the normal default mount:

- `readlink` prints `/cloud/home`.
- `findmnt` shows `/cloud/home` with `FSTYPE` `fuse.rclone`.
- `agent-host` can list `/cloud/home`.

Pass criteria for missing explicit default-home credentials:

- The session still starts.
- The mount points at the regular session-folder fallback.
- There is no eager default-home clone.
- Logs explain fallback without exposing credentials.

---

## 6. Agent Tool Smoke

In the session composer, send:

```text
Please run a quick workspace check: pwd; readlink /workspace/cloud; findmnt -T /cloud/home | head -n 2. Keep the answer short.
```

Pass criteria:

- The message is accepted through the UI.
- The agent loop starts from REST input.
- If supervised mode asks for command approval, the approval card appears live.
- Approving the request completes the command.
- The answer includes:
  - `/home/agent-host/workspace`
  - `/home/agent-host/workspace/cloud` or `/cloud/home`
  - `fuse.rclone`

Check logs:

```bash
AGENT=<agent-pod-name>

kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" logs "$AGENT" --since=5m \
  | grep -E 'Persistent loop|/api/input|chat/completions|embeddings|Turn .* complete'

kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" logs deploy/srw-orchestrator \
  --since=5m | grep -E "$THREAD_ID|/approve/|/input"
```

Pass criteria:

- Agent log shows REST input started or reused the persistent loop.
- Chat and embedding calls route to the configured dev/local model endpoint.
- No fallback to the wrong provider endpoint.
- Approval uses `POST /api/persistent/threads/<id>/approve/<approval_id>` when
  a durable permission request is emitted.

---

## 7. Guardrail Smoke

Ask the agent to run an obviously broad scan:

```text
Use run_command to execute: grep -R "anything" /workspace/cloud
```

Pass criteria:

- The operation is blocked or warned according to the deployment's
  `cloud_scan_guard` setting.
- In blocking mode, the command does not run.
- The message tells the agent to use targeted paths or cloud/search tooling
  instead of blind recursive scans.

Optional direct shell check from inside the workspace:

```bash
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" exec "$WS" -- \
  su agent-host -c "cd /home/agent-host/workspace && grep -R anything cloud"
```

This direct shell check bypasses agent-side tool policy and is only useful for
understanding raw mount behavior. The product guardrail must be tested through
the agent `run_command` tool.

---

## 8. `srw_cloud_status` Smoke

In the session composer, ask:

```text
Use srw_cloud_status and summarize the mount target, cache usage, and any rclone stats in one short paragraph.
```

Pass criteria:

- The tool is available.
- Output includes the active mount target.
- Output includes VFS cache usage or a clear "unknown/unavailable" status.
- Output does not expose rclone RC credentials or cloud passwords.

---

## 9. `.cloudignore` / Fallback Folder Check

If the mounted cloud root is writable and safe to modify on dev:

1. Add a small `.cloudignore` at the cloud root with a harmless ignored folder,
   for example:

   ```text
   Photos/
   *.tmp
   ```

2. Create a fresh session.
3. Confirm the mount still starts.
4. Confirm ignored paths are not visible through the mount.

Pass criteria:

- `.cloudignore` does not break mount startup.
- Directory-style ignore rules expand recursively.
- Unsafe parent traversal patterns, if added, are ignored rather than applied.

Skip this section if the dev cloud root must not be modified.

---

## 10. Cleanup Behavior

End the session from Cockpit or delete the test thread through the normal UI.

Then check:

```bash
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" get pods --show-labels \
  | grep "$THREAD_ID" || true
```

Pass criteria:

- Session agent and workspace pods are removed or move to the expected terminal
  state for the dev retention policy.
- No stale ready session Service endpoint remains for the thread.

If a workspace pod remains for debugging, inspect mount cleanup before deleting
it:

```bash
kubectl --context="$KUBE_CONTEXT" -n "$NAMESPACE" exec "$WS" -- \
  findmnt -T /cloud/home || true
```

---

## 11. Failure Triage

### Session starts but clone/sync logs appear

Fail signal:

- Agent logs mention the legacy cloud sync coordinator after
  `rclone cloud mount started`.

Expected fix area:

- Active cloud mount should suppress both `cloud_sync` and legacy
  `nc_session_folder` sync fallback.

### Session creates duplicate agent pods

Fail signal:

- Two `srw-agent-s-*` pods for the same thread.
- Orchestrator logs show two `Agent pod created` lines for one thread.

Expected fix area:

- `threads.metadata.agent_pod` in-flight marker and
  `agent_pod_provisioning_in_progress()`.

### Composer stays disabled after session is ready

Fail signal:

- `/api/sessions/<thread>/connection` returns ready but Cockpit still shows
  startup state.

Expected fix area:

- Cockpit `PersistentChatService` must mark the session ready from `/connection`
  readiness, not only from WS `session.state`.

### UI accepts input but the agent never works

Fail signal:

- `/api/persistent/threads/<id>/input` returns 200, but no agent LLM/tool logs
  follow.

Expected fix area:

- Agent REST input path must call the persistent loop starter.

### Wrong model/provider is used

Fail signal:

- Thread create does not inject system default chat/aux/embedding.
- Agent logs route chat calls to an unintended provider endpoint.

Expected fix area:

- Cockpit model-group prefill must not serialize expert YAML defaults as user
  overrides.

### Permission card does not approve

Fail signal:

- `permission.request` is persisted, but clicking Approve does not unblock the
  agent.

Expected fix area:

- Cockpit should retain `approval_id` and call
  `/api/persistent/threads/<thread_id>/approve/<approval_id>`.

---

## 12. Pass Criteria Summary

The dev-cluster smoke passes when all are true:

- One agent pod and one workspace pod per new session.
- Workspace pod has rclone/FUSE support.
- `/workspace/cloud` resolves to the mounted cloud surface.
- `/cloud/home` is a `fuse.rclone` mount or the documented session-folder
  fallback.
- No eager default-home clone starts.
- Cockpit reaches `Connected` and composer is usable without reload.
- REST-submitted user input starts or reuses the persistent loop.
- Local/dev model routing is correct.
- Supervised command approval renders live and resolves through durable REST.
- A shell command can read the workspace and mount state.
- Broad cloud-scan guardrails block or warn as configured.
- `srw_cloud_status` works without leaking credentials.

---

## 13. Run Results — 2026-06-10, dev cluster (`main` ctx / `superhuman-remote-worker` ns)

Deployed state: orchestrator/agent `sha-383b0a4` → agent rolled to `sha-ea373fd`
mid-run (see incident below), cockpit `sha-fbce77f`, workspace `sha-4830d12`,
chart `0.0.0-dev.sha-ea373fd` (helm rev 227). All rclone commits
(`407662b7`..`4830d122` + v5 fixes via PR #113) verified present in the
deployed images.

**Headline: dev runs `MAIN_CLOUD_BACKEND=opencloud`, and OpenCloud does not
implement `SupportsRcloneMount`. The orchestrator therefore never emits a
`cloud_mount` payload on this deployment — every session takes the documented
session-folder fallback. The fuse-mount path itself (mount, `.cloudignore`,
scan-guard active path, `srw_cloud_status` output, cache guard) is NOT
exercisable on dev as configured; it remains validated only on local
k3d + bundled Nextcloud (Phase 1 v5 smoke).**

| Criterion | Result |
|---|---|
| §2.1 driver/FUSE env (`rclone_mount`, `CLOUD_RCLONE_ALLOW_CONTAINER=true`, `WORKSPACE_FUSE_ENABLED/PRIVILEGED=true`) | PASS |
| §2.2 workspace image deps (rclone v1.60.1-DEV, fusermount3, /dev/fuse, `/cloud` owned by agent-host) | PASS |
| §3 Cockpit session Connected + composer, no reload | PASS (×2 sessions) |
| §3 system-default model injected (not expert YAML) | PASS (`injected system default chat model: gemma-4-moe`) |
| §4 one agent pod + one workspace pod, no duplicates | PASS (session 1: dedicated `srw-agent-s-*`; session 2: pool attach; prepare race guard fired: "agent pod already provisioning — waiting for binding") |
| §5 mount state | Fallback as designed: no `cloud_mount`, no symlink/fuse mount, OpenCloud session folder `sessions/<id>` created, sync coordinator started with exactly 1 mount, **no eager home clone** |
| §6 REST input starts loop | PASS (`POST /input 200` → turn ran) |
| §6 supervised approval live + durable REST | PASS (`POST .../approve/<approval_id> 200`) |
| §6 model routing | PASS (chat/aux/embedding → `ai.h4ll.app` dev endpoints) |
| §7 cloud-scan guard | N/A-as-designed: guard scoped to active cloud mounts; verified inactive in fallback (grep ran, exit 2) |
| §8 `srw_cloud_status` | N/A-as-designed: tool correctly NOT bound without an active mount |
| §9 `.cloudignore` | Skipped (requires real mount) |
| §10 cleanup | PASS (pods deleted + snapshot captured, Gitea repo + OpenCloud session folder deleted, no stale services) |

Incidents and observations:

1. **Mid-run develop deploy killed the live session.** CI deploy commit
   `e189f683` (agent → `sha-ea373fd`) landed during the test; Fleet synced,
   Reloader bounced the orchestrator, and the lifecycle reconciler drained all
   build-sha-drifted agent pods (`drift: 4, drained: 1, skipped_busy: 3`) —
   including the just-created, *idle but user-attached* session agent. The UI
   still showed `Connected`; the next input hit `Restoring suspended workspace`,
   raced the half-deleted workspace pod (409), returned 503, and the session
   showed "ended". Works-as-designed for drift-drain, but on dev every develop
   push currently kills live idle sessions. Consider exempting session-bound
   agents from drift-drain (or rebinding instead of draining).
2. **`gemma-4-moe` (injected default) failed its first turn**: endpoint
   returned HTTP 200 with an empty stream → `No generation chunks were
   returned`. Known flaky homelab backend; retried on `gpt-5.4-mini`
   successfully via the session-settings model switch.
3. Minor cosmetics: ended-session header chip falls back to config name
   (`developer`) instead of model; new-session form once rendered raw i18n key
   `agentSettings.permissionModes.supervised.description`; developer expert
   config references unknown tools `browse_website`/`download_from_website`
   (tool-loading warning in agent log).

To exercise the real mount path on dev infra, either implement OpenCloud
`SupportsRcloneMount` (token-helper strategy from the feature doc §11) or point
a dev deployment at a Nextcloud main-cloud backend.
