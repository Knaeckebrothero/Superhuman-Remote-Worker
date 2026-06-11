# Persistent sessions can silently run on the worker YAML (config_name plumbing)

**Status**: FIXED + LIVE-VERIFIED on k3d 2026-06-11 (same day as found):
- Hole A: `ThreadCreateRequest.config_name` default flipped to
  `"persistent_defaults"` (orchestrator/main.py). Verified: bare
  `POST /api/persistent/threads` → thread row stores `persistent_defaults`.
- Hole B: `_send_session_attach` now carries the thread's `config_name` on
  all three call sites (provision_or_assign k8s create path, compose create
  path, resume path); agent-side, **BOTH** `/session/attach` routes forward
  it — `persistent_app.py` AND `dual_app.py` (the job pool runs the dual
  app; the first live verify missed that route — it answered 200 and
  silently dropped the field). `_attach_session` resolves it via the new
  `_load_expert_config()` (mirrors the worker job path's expert reload,
  settings-matrix included) as the session base instead of the pod's boot
  config. Unknown names raise → attach 500s/releases → the orchestrator
  re-provisions with the right config. Verified live: bare-created thread
  pool-attached to a worker-booted dual pod logged
  `Attach: session base config 'persistent_defaults' (overrides pod boot
  config)` and bound `['persistent_interval_extractor',
  'teardown_extractor']` (thread `d09ee110`).
- Bonus (same arc): the dual pod's `/session/detach` now terminates with
  reason `"rest_detach"` (was the `"legacy"` back-compat shim), so the
  documented `Terminate(rest_detach)` signal greps identically on pool pods.
Pinned by `tests/test_session_config_plumbing.py` (12 cases incl.
source-level pins on both attach routes). Found 2026-06-11 during the
memory-overhaul Phase-1 closure step 1 (live k3d verify, flag on).
Pre-existing plumbing, newly consequential once `memory.manager` pipelines
became per-mode YAML choices. Original analysis below.

## Why this matters now

`config/defaults.yaml` (worker) and `config/persistent_defaults.yaml`
(sessions) now diverge *semantically* in `memory.pipeline`:

- worker writers: `interval_extractor, phase_boundary_extractor,
  memory_assembler, compaction_memory, queued_memory`
- persistent writers: `persistent_interval_extractor, teardown_extractor`

A session that ends up on the worker YAML binds the worker writer set. Net
effect: **session_end / idle_archive / terminate captures find no subscriber —
final memory extraction is silently lost** (the B11 capture itself still runs
and logs, but extracts nothing), and in-loop extraction runs the worker modulo
gate + auxiliary task-flag gates instead of the persistent elapsed gate.

Pre-cutover this mostly didn't matter: the memory scalars are identical in
both files and the extraction algorithms were hardcoded per loop, not
config-selected.

## Hole A — `ThreadCreateRequest.config_name` defaults to `"defaults"`

`orchestrator/main.py` (`class ThreadCreateRequest`, ~11832):

```python
config_name: str = Field("defaults", description="Agent config to use")
```

Every *other* session-config resolution site in main.py falls back to
`"persistent_defaults"` (the SessionCreate model at ~10829, user-settings
fallback at ~12016, thread-row fallbacks at ~12894/12956/14084, and
`persistent_provisioner.py`). Because the request-model default is a truthy
string, the downstream `request_body.config_name or user_settings...` never
falls through — a bare `POST /api/persistent/threads` (no explicit
config_name) lands on the **worker** YAML.

Live evidence (k3d, 2026-06-11): thread `28b613f4` created without
config_name → agent pod logged `Starting persistent agent: config=defaults`
and bound the worker writer set.

**Fix**: one line — change the Field default to `"persistent_defaults"`.
Check the cockpit first: if it always sends config_name explicitly, the change
is invisible to it; anything relying on the bare default getting worker
config for a *session* is almost certainly wrong anyway.

## Hole B — `/session/attach` (idle-pool reuse) ignores the thread's config_name

When the orchestrator assigns a thread to an already-running idle agent pod
(`provision_or_assign` pool path), the agent-side handler
`src/api/persistent_app.py::session_attach` receives only
`thread_id + config_override + project_ids + datasources`. `_attach_session`
then builds the session from the **pod's boot config** (`AGENT_CONFIG`, which
is `defaults` for job-pool pods) merged with the override — the thread row's
`config_name` never crosses the wire.

Live evidence (k3d, 2026-06-11): thread `7cf18b5f` created with explicit
`config_name: persistent_defaults` was attached to idle job-pool pod
`srw-agent-j-4bd66b89` → bound the worker writer set anyway.

**Fix options** (pick one):
1. Orchestrator includes `config_name` in the attach payload; agent-side
   `_attach_session` re-resolves the expert config like the boot path does
   (mirror of the `metadata["config_name"]` reload in `src/agent.py`).
2. Cheaper but blunter: exclude worker-pool pods from session assignment so
   sessions always get a dedicated pod provisioned with
   `AGENT_CONFIG=persistent_defaults` (cost: pool reuse latency win is lost).

## Workaround until fixed

Always pass `config_name: "persistent_defaults"` when creating threads via
the REST API, and be aware that pool-attached sessions still bind worker
pipelines regardless (Hole B). Dedicated session pods (the path taken when no
idle pool pod exists) are correct.

## Verification once fixed

Create a thread without config_name AND a thread while an idle job-pool pod
exists; both agent logs must show
`MemoryManager bound: ... writers: ['persistent_interval_extractor',
'teardown_extractor']`.
