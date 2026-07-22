# Shared Browser Plan 2 verification

**Date:** 2026-07-22

**Scope:** Attested container workspaces, default-off shared-browser feature

**Status:** The container user flow is implemented and repository-verified.
Live k3d acceptance is substantial but not a release pass: the VM image build,
prompt-driven LLM handoff, and a full orchestrator-pod rotation remain
unverified for the reasons recorded below. The committed Tilt profile therefore
remains disabled.

This record intentionally omits thread IDs, workspace addresses, ports,
fingerprints, browser generations, cookies, tokens, page/frame content, and
local credential values.

## Implementation identity

Plan 2 implementation spans `2e103cb9` through `5296ccaa`:

| Area | Commit |
|---|---|
| Pinned browser-start SSH | `2e103cb9` |
| Capability and authoritative open | `23389dfb` |
| WebSocket admission hardening | `438f87f6` |
| Active-target/loading lifecycle | `343a39a4` |
| Container/VM conformance wiring | `e2a642a5` |
| Agent Canvas browser source | `4f2fc468` |
| Agent baton/refusal output | `3bfd1a2c` |
| Cockpit protocol and models | `9fdd152e` |
| Cockpit stream controller | `b5e59aba` |
| Cockpit renderer | `0fad013f` |
| Cold-open workflow | `0e107915` |
| Input and baton toolbar | `9b1e3ba3` |
| Three-engine lifecycle coverage | `0c691202` |
| Browser-visible admission close codes | `e208fae9` |
| Clean service-restart reconnect | `5296ccaa` |

The two final fixes came from live acceptance rather than synthetic tests:
ASGI pre-accept rejection hid application close `4429` behind browser code
`1006`, and Uvicorn uses clean close `1012` while restarting. The broker now
accepts and immediately closes rejected handshakes so the contractual code is
observable, and Cockpit treats `1012` as transient.

## Automated gates

| Gate | Result |
|---|---|
| Focused Python command from Plan 2 | 649 passed; 303 dependency/test-fixture warnings |
| Ruff lint | `ruff check src/ orchestrator/ tests/` passed |
| Ruff formatting | All 31 Plan-2-touched Python files passed independently. The repository-wide check reports six unrelated owner-work files: `orchestrator/database/postgres.py`, `orchestrator/main.py`, `orchestrator/services/completion.py`, `orchestrator/services/default_experts.py`, `tests/test_automations_service.py`, and `tests/test_db_backed_default_experts.py`. |
| Cockpit i18n | German matched all 2,193 English keys; no hardcoded user-facing strings |
| Cockpit Vitest | 95 files, 1,347 tests passed |
| Cockpit production build | Passed; only the existing bundle-budget warning and existing CommonJS warnings remain |
| Production-browser conformance | 33/33 passed: 11 each in Chromium, Firefox, and WebKit |
| Helm | Both required lint overlays passed; default render is `false`, experimental render is `true` |
| Container image | A fresh Podman workspace image passed the real `assert-browser-stack` run as `agent-host`, including loading, target change, JPEG stream, baton/refusal, reconnect, and viewer cap |

The WebKit run used the already-installed Playwright browser with a local ABI
compatibility workaround outside the repository. No repository or deployment
pin changed.

## Live local k3d evidence

The feature was enabled only through a temporary ignored local-values key.
The live ConfigMap and both orchestrator and session-agent environments carried
the boolean gate. The temporary key was removed after acceptance, Tilt was
stopped, and all temporary host/image workarounds were restored or removed.

Passed live checks:

- a new full-workspace session exposed **Open browser** before any Canvas row;
  cold open reached a real frame in roughly 9–11 seconds;
- user navigation updated the shared surface, and the exact executor used by
  agent browser tools returned the same page from `browser_snapshot`;
- releasing the baton let the executor navigate while the existing Cockpit
  surface updated; taking it made mutating executor actions return
  `user_is_driving` while read-only snapshots continued to work;
- an invalid non-HTTP navigation produced the visible bounded rejection and
  did not disconnect the stream;
- the authenticated popout attached to the same stream and rendered a frame;
- three simultaneous viewers streamed; the fourth received the explicit
  viewer-limit state and did not enter a reconnect loop;
- one attached viewer advanced thread activity across a 70-second interval and
  the thread did not become paused or suspended;
- after the last user-driving viewer detached for 38 seconds, a new viewer
  observed that the daemon had returned the baton to the agent;
- executor shutdown produced **Ended**; **Restart browser** selected exactly
  one new Canvas presentation revision and returned to a live frame;
- a real Uvicorn process restart emitted clean close `1012`; Cockpit opened a
  replacement socket and returned the same surface to Ready; and
- a lite/virtual session kept **Open browser** discoverable but disabled and
  showed the `workspace_required` explanation.

The temporary flag was then removed and the live ConfigMap reconciled to
`false`. Automated production-browser coverage proves that a cold feature-off
session hides the empty affordance. Repeating that final UI assertion against
the local cluster was blocked after pod rotation by the host/CNI condition
below.

## Acceptance boundaries and blockers

### Prompt-driven agent handoff

The installed default model endpoint had no available serving backend. A
separate configured external-model probe reached its provider but the local
credential was a placeholder and was rejected. Neither attempt made a tool
call. The executor-level same-browser, baton, refusal, and snapshot behavior is
proven, but this record does not claim that a natural-language agent prompt
completed the handoff live.

### Orchestrator pod rotation

A real pod replacement produced the expected clean `1012` close and led to the
Cockpit reconnect fix. The replacement orchestrator then could not open SSH to
workspace pods created before the rotation because this local k3d host's CNI
returned `No route to host`. A same-pod Uvicorn restart passed the complete
1012 reconnect flow. This record therefore proves client/server restart
semantics but not full pod-rotation continuity on this host.

### VM image and runtime

`/dev/kvm` was present, and repository tests prove that both workspace-image
paths install the same conformance program and that unattested/unroutable VM
contexts fail closed. The required stage-1 qcow2 and Packer executable were not
available, so no stage-2 VM artifact was built and no VM image conformance
claim is made. Runtime capability remains disabled without both an attested
SSH binding and orchestrator routing.

## Release decision

Tasks 1–13 and the container implementation are complete. Task 14 is only
partially complete because the three boundaries above prevent the plan's full
acceptance definition. Chart defaults remain off, the experimental profile
remains explicitly on, and `deployment/values-tilt.yaml` was deliberately not
enabled.

The next implementation document should be **Plan 3 — Shared Browser Release
and Enablement**. It should convert the remaining Plan-2 Task-14 work into a
repeatable acceptance harness, run it on infrastructure with healthy pod
networking, a usable disposable LLM endpoint, Packer, and the stage-1 VM image,
then enable only the Tilt overlay after every gate passes. New collaboration
features are explicitly deferred until this release checkpoint is closed.
