# Job Debug: 8a202851 (Validator for Quadlet Redeployment)

**Date**: 2026-03-07
**Job ID**: `8a202851-d3b6-44cd-984c-82ab322cb120`
**Parent Job**: `34f93b9d-e410-4e36-b1f2-b73a5f38890e`
**Task**: Verify deliverables of parent job ("Redeploy everything as quadlets" on server 10.18.2.105)
**Status**: Completed (with incomplete deliverables)
**Audit Entries**: 733 | **Chat Turns**: 356

## Issues & Barriers

### 1. Server Crashed During Deployment (Critical Blocker)

The target server `10.18.2.105` became completely unresponsive during the pixtral-12b deployment attempt in Phase 3. SSH fails with "Connection reset by peer" (exit code 255), HTTP endpoints on :8085/:8086 timeout. Likely the server ran out of memory pulling/starting the large vLLM model image.

The agent correctly identified this and used `todo_rewind` to archive the blocked phase, but couldn't complete 3 of 7 deliverables:

| Deliverable | Status |
|-------------|--------|
| gpt-oss-20b running | UNKNOWN (server unreachable) |
| pixtral-12b running | INCOMPLETE (deployment failed) |
| ai-model-router running | UNKNOWN (server unreachable) |

### 2. Massive Strategic Phase Overrun (Phase 0)

The first strategic phase (Phase 0) was supposed to be a review-and-plan step, but the agent treated it as a full tactical execution phase. The system issued escalating warnings at **15, 20, 25, 30, 35, 40, 45, and 50 tool calls** — all ignored. The agent performed heavy server verification (SSH sessions, paramiko scripts, firewall checks, endpoint tests) during strategic mode.

By the time Phase 0 completed, the agent had already:
- Opened 15 shell tabs (hitting the max limit)
- Created and run multiple paramiko helper scripts
- Verified firewall, services, containers, endpoints
- Written a verification report and returned the job with feedback

This is work that should have been done in a tactical phase. The strategic phase should have been ~10 tool calls max (read deliverables, assess, plan).

### 3. Shell Proliferation (~35 unique shells)

The agent spawned ~35 unique named shells across the job and rarely cleaned them up. Two distinct generations:

**Phase 0 (strategic — shouldn't have needed any):**
`verify-server`, `firewall-check`, `firewall-check2`, `router-check`, `llmprod-check`, `llmprod-services`, `routerprod-services`, `router-config`, `router-config2`, `server-quadlet-gpt`, `test-router`, `test-gpt`, `test-gpt-raw`, `test-gpt-local`

**Phases 1-3:**
`verify-ports`, `verify-firewall`, `verify-firewall2`, `verify-ports-sudo`, `verify-ports-sudo2`, `gpt-logs`, `llmprod-containers`, `llmprod-quadlets`, `fresh-shell`, `test-endpoint`, `quick-test`, `wait-model-load`, `check-logs`, `final-test`, `server-connection`, `llmprod-check`, `llmprod-ps`, `admin-check`, `deploy-pixtral`, `check-pixtral`, `check-pixtral-2`, `check-pixtral-3`, `test-pixtral`, `fresh-ssh`, `test-gpt`

This bloats the `<terminal_state>` injection every LLM call, wasting context tokens. The agent hit the **max tabs (15) limit 7 times**, each time requiring a cleanup loop of consecutive `shell_close` calls (e.g., page 7 turns 20-27: 8 consecutive closes just to free up slots).

### 4. Blocked Tab Cascading

Instead of resolving password prompts in existing tabs (sending the password via keys mode or Ctrl+C), the agent would open a NEW tab. This created cascading blockages:
- `verify-server` blocked by password prompt → opened `firewall-check`
- `firewall-check` also hit sudo prompt → opened `firewall-check2`
- `verify-ports` blocked → opened `verify-firewall` → blocked → `verify-firewall2`
- `verify-ports-sudo` blocked → `verify-ports-sudo2`

The original blocked tabs were never resolved, just abandoned — consuming tab slots until forced cleanup.

### 5. Helper Script Proliferation

The agent created **20+ one-off Python scripts** for SSH operations instead of using `sshpass` or `ssh` directly:
- `check_llmprod_quadlet.py`
- `check_llmprod_service.py`, `check_llmprod_service2.py`, `check_llmprod_service3.py`
- `check_llmprod_sudo.py`, `check_llmprod_sudo2.py`, `check_llmprod_sudo3.py`, `check_llmprod_sudo4.py`, `check_llmprod_sudo5.py`
- `check_llmprod_containers.py`, `check_llmprod_containers2.py`
- `check_failed_container.py`, `check_running_container.py`
- `check_quadlet_service.py`
- `check_container_config.py`, `check_container_config2.py`
- `start_quadlet_service.py`, `fix_quadlet_deployment.py`
- `check_service_failure.py`, `check_service_failure2.py`
- `start_gpt_oss_20b.sh`

Each script is a slight variation because the previous one didn't work. This is incremental debugging-by-script-creation rather than understanding the SSH/sudo/user permission model first and using direct commands.

### 6. DBus/Systemd Quadlet Incompatibility

The original task was "redeploy as quadlets" but the server's `llmprod` user doesn't have a DBus session (`$DBUS_SESSION_BUS_ADDRESS` and `$XDG_RUNTIME_DIR` not defined), so `systemctl --user` services can't work. The agent had to fall back to bare `podman run` commands — meaning the deliverable (quadlet-based deployment) is structurally impossible on this server without root-level changes.

The agent adapted but the underlying requirement is unfulfillable with the current server configuration.

### 7. Password Confusion and Credential Cycling

The agent cycled through multiple credentials and SSH approaches, burning many tool calls:
- `admin` user with `sshpass` and password `#4%KNg8*9$Jh7bm7U5j`
- `llmprod` user with password `sP0AKp$wy58$g5&C@60`
- `embeddingprod` user with password `%Q7jEgvfN&Bt5!2!ka2`
- `routerprod` user with password `Rt8$mK2v!pQ9xN3wL7j`
- Interactive password entry via `shell_execute` with `keys: true`
- Paramiko (Python SSH library) fallback attempts (audit entries [42]-[66])

Critically, the agent **guessed wrong passwords** — audit entries [181-185] show it sending `GhostPassword123!` (the local machine's sudo password) as the SSH password for `llmprod` on the remote server. Tried it twice via keys mode and once via sshpass before discovering the correct password from workspace files.

### 8. Loop Detection Triggered Multiple Times

The system's loop detector fired at least 4 times for repeated identical `shell_execute` calls:
- Page 5, Turn 14: Same SSH command 3 times (sudo password issue)
- Page 5, Turn 45: Same firewall rule command 3 times
- Page 6, Turn 15: Same iptables command 3 times
- Page 7, Turn 6: Same SSH connect attempt 3 times (server unreachable)

After loop detection, the agent typically opened a new tab with a slightly different command rather than reconsidering the approach.

### 9. write_file Type Error (Dict Instead of String)

Audit entries [86] and [88]: The agent passed a Python dict as the `content` parameter to `write_file` for `output/verification_report.json`. Failed twice with `ToolInvocationError` before falling back to writing via a Python script (entry [90]). The model didn't understand the tool expects a string, not a structured object.

### 10. write_file Before read_file Guard Hits

Hit at least twice:
- Audit entry [155]: Tried to overwrite `workspace.md` without reading first
- Page 5, Turn 6: Tried to overwrite `start_gpt_oss_20b.sh` without reading first

Minor per-instance, but part of a pattern of rushing to write before understanding current state.

### 11. Workspace Escape Attempt

Page 5, Turn 40: The agent tried to read `/tmp/start_gpt_oss_20b.sh` — blocked with "Path escapes workspace boundary". The agent had previously transferred a script to the server's `/tmp/` and then tried to read it locally, confusing local and remote file systems.

### 12. Invalid Tab Name

Page 7, Turn 32: "Error: Invalid tab name 'test-pixtral-endpoint': must match ^[a-z0-9-]{1,20}$" — name exceeded 20 character limit. The agent then created `test-pixtral` instead. Minor but shows the constraints aren't internalized.

### 13. Job Marked Complete Despite Incomplete Deliverables

The agent called `job_complete` even though the deliverable table shows pixtral-12b as INCOMPLETE and two others as UNKNOWN. Pragmatically correct (server is down, nothing more can be done), but the summary could have been clearer about partial completion blocked by infrastructure.

## Quantitative Summary

| Metric | Value |
|--------|-------|
| Total audit entries | 733 |
| Total chat turns | 356 |
| Unique shell names | ~35 |
| Max-tabs-reached errors | 7 |
| Loop detection warnings | 4 |
| Helper scripts created | 20+ |
| Strategic mode overrun warnings | 8 (at 15, 20, 25, 30, 35, 40, 45, 50 calls) |
| write_file guard rejections | 2 |
| write_file type errors | 2 |
| Password guess failures | 3 (GhostPassword123!) |
| Workspace escape errors | 1 |
| Tab name validation errors | 1 |

## Recommendations

### Immediate (Agent Behavior)
1. **Shell cleanup discipline** — Consider adding a max-shells limit or auto-close idle shells. The agent should `shell_close` tabs it's done with, especially SSH sessions. Consider auto-closing tabs that have been idle for N tool calls.
2. **Resolve blocked tabs, don't abandon them** — When a tab hits a password prompt, the agent should either send the password (keys mode) or Ctrl+C, not open a new tab. Add stronger prompting around this pattern.
3. **Strategic phase guardrails** — The 15-call warning is ignored. Consider making the transition mandatory after 20 calls, or degrading tool availability in strategic mode (e.g., disable shell_execute).

### Structural (System/Config)
4. **Credential map in job description** — Providing a table of `user → password → purpose` would eliminate the credential discovery phase that burned ~20 tool calls.
5. **Server health pre-check** — Before deploying heavy workloads (large model pulls), require checking available memory/disk/GPU.
6. **Quadlet feasibility check** — The DBus issue should have been caught in a survey/pre-check phase before attempting deployment. Add a pre-flight checklist for systemd-dependent deployments.
7. **SSH helper pattern** — Instead of creating 20+ paramiko scripts, the agent should establish a working SSH pattern once (e.g., `sshpass -p $PASS ssh -o StrictHostKeyChecking=no user@host "cmd"`) and reuse it. Consider providing an SSH wrapper tool or documenting the pattern in instructions.

### Code Improvements
8. **write_file content validation** — The tool should provide a clearer error when receiving a non-string content argument, suggesting JSON serialization.
9. **Tab auto-cleanup on phase transition** — When entering a new phase, auto-close all tabs from the previous phase to prevent accumulation.
10. **Loop detector escalation** — After 3 loop detections in the same phase, force a strategic pause or inject a "step back and reconsider" prompt.

## Timeline

| Phase | Tool Calls | Outcome |
|-------|-----------|---------|
| Phase 0 (Strategic) | ~94 | Massive overrun. Full server verification, returned job with 3 critical issues. Hit max tabs. |
| Phase 1 (Tactical) | ~170 | Fixed gpt-oss-20b container — running via direct podman, endpoint verified at :8085. 20+ helper scripts created. |
| Phase 2 (Strategic) | ~18 | Reviewed Phase 1, staged pixtral-12b deployment. Clean execution. |
| Phase 3 (Tactical) | ~50 | BLOCKED — server became unreachable during pixtral-12b deployment. `todo_rewind` used correctly. |
| Phase 4 (Strategic) | ~20 | Acknowledged blocker, completed job with caveats. Clean execution. |

## Key Takeaway

The core technical blocker (server crash) was unavoidable. But the agent burned ~200 tool calls on SSH/sudo/credential wrestling and shell management overhead that better tooling and clearer job descriptions would have prevented. Phases 2 and 4 (strategic) were clean and efficient — the problems are concentrated in Phase 0 (strategic overrun) and Phase 1 (tactical SSH flailing).
