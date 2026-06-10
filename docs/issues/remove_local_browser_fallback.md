# Remove the local in-pod browser fallback (latent execution path on the agent pod)

**Status**: Open — security hardening. Filed 2026-06-10.

## Context

The browser was moved onto the workspace pod: the direct `browser_*` tools
drive the workspace-side `browser-exec` daemon over SSH
(`ToolContext.browser_exec()` → `backend.exec_command("browser-exec <action>
--json ...")`), so CDP never crosses the pod boundary and page content is
interpreted inside the NetworkPolicy-restricted workspace runtime. See
`docs/features/browser_workspace_executor.md`.

That migration **retained a local fallback**: when auto-detection decides the
session is not "remote", every `browser_*` tool silently executes against an
in-process Chromium (`browser_use.BrowserSession`) **inside the agent pod**.
The fallback is currently unreachable in cluster deployments (production
backends are always SSH-remote), which is exactly the problem: it is a dormant
execution path guarded by an inference, not a capability check, kept alive for
a dev posture (bare-metal `agent.py --loop`) that is already slated for
removal (`docs/issues/deprecate_docker_compose_stack.md`).

**Decision: remove the local browser execution path entirely.** Fallbacks that
never trigger are pure downside — if a condition we didn't model flips the
detection, the agent pod becomes the execution surface. Remote/`browser-exec`
becomes the *only* browser path.

## Why a dormant path is a real vulnerability

`_is_remote_browser()` (src/tools/research/browser.py:40-51) selects **local**
when *any* of these hold:

1. `browser.remote: "local"` in config — a one-line YAML override (or typo in
   an expert config) away from in-pod execution;
2. `not context.has_workspace()` — true in init/teardown windows and any
   future workspace-less mode;
3. `backend.host is None` — duck-typing for "is this backend SSH-shaped".
   The planned no-workspace/virtual-filesystem backend (rclone/S3) has no
   `host`, so the moment lite mode exists, this latent path **goes live by
   default** for any lite profile that registers browser tools.

Why in-pod Chromium is the worst case:

- The agent pod has **no NetworkPolicy** — the chart's only policy is
  `workspace-network-policy.yaml`. From the agent pod, Chromium can reach the
  cluster network, the org intranet/LAN, and node addresses.
- The agent process environment holds **internal credentials** (LLM keys,
  `EMBEDDING_*`, DB credentials injected via `config_override.env_keys`) that
  are deliberately never propagated to workspaces. A JS-executing engine that
  renders attacker-controlled web content does not belong in that pod.
- The L7 guards in `browser_security.py` (scheme/metadata/K8s-DNS blocklists)
  are string-level checks on the requested URL; they do not survive redirects,
  DNS rebinding, or fetches embedded in page resources.
- Browser downloads land on the agent pod's local filesystem, violating the
  "agent never uses its own filesystem as a workspace" invariant.

This is the same anti-pattern as the ShellManager → local-libtmux degradation:
written when "local" meant a developer's laptop and was the safe default. In a
cluster, local is the *least* safe place to execute anything.

## Inventory of code to remove

> Line numbers were accurate on 2026-06-10 and will drift — re-grep
> `_is_remote_browser` / `get_browser_session` / `browser_use` when
> implementing.

### 1. `src/tools/research/browser.py`

- `_is_remote_browser()` (:40-51) — the detection itself. Delete; remote is
  the only mode.
- `_start_remote_chromium()` (:54-111) / `_stop_remote_chromium()` (:114+) —
  the **old CDP-over-network path** (`ws://<pod-ip>:9222`), retained only for
  the papers.py fallback (comment :30-34). Already broken cluster-wide:
  Chrome 147 binds the debugging port to loopback and ignores
  `--remote-debugging-address` (see `project_remote_browser_cdp_loopback`
  memory / session b4478b88 diagnosis). Dead code on the cluster today.
- `_get_browser_config()` (:246-317) — returns local headless/downloads
  kwargs, including the local-only proxy wiring (:305). Both branches go
  (remote branch only feeds the broken CDP path).
- `_get_browser_llm()` (:165+), `_find_new_files()` (:329+),
  `_find_new_files_remote()` (:124+), `_register_downloaded_file()` (:352+) —
  support code for the papers.py browser fallback. Remove whatever has no
  consumers left after step 4.

### 2. `src/tools/context.py`

- `get_browser_session()` (:657-711) — lazy-starts an in-process
  `browser_use.BrowserSession` (local Chromium), including the
  `BROWSER_HEADLESS` env handling and `browser.persistence.user_data_dir`
  resolution. Delete.
- `_close_browser_session()` and the `_browser_session` / `_browser_started`
  instance state. Delete.
- The shutdown branch that distinguishes local vs remote (:757-766): keep only
  the `browser_exec("shutdown")` path.
- **Keep** `browser_exec()` (:713-743) — that seam *is* the browser story.

### 3. `src/tools/research/browser_direct.py`

- `_local_action()` (:227-275) and the local helpers it drives:
  `_local_page_state`, `_click_element`, `_type_text`, `_select_option`,
  `_scroll` (:127-224, including the inline `browser_use.browser.events`
  imports). Delete.
- `_run_action()` (:278-286): drop the branch; always
  `return await context.browser_exec(action, **args)`. If the backend cannot
  run browser-exec, return the clear error the daemon path already produces —
  never degrade to local.
- The `_is_remote_browser` import (:32).

### 4. `src/tools/research/papers.py`

- `_try_browser_download()` (:~415-531) — remove the whole browser fallback,
  both branches:
  - the **local** branch spawns an autonomous `browser_use.Agent` +
    `Browser(**local_kwargs)` in-pod — an LLM-driven browser inside the agent
    pod, the worst instance of this pattern;
  - the **remote** branch uses the broken `_start_remote_chromium` CDP path
    and therefore already fails on the cluster (it logs and returns `None`).
  Callers keep the existing direct-HTTP download paths and their "not
  available" messaging. If browser-assisted PDF retrieval is ever wanted
  again, it should be a `browser-exec` action on the workspace daemon (or the
  future shared browser pool, see `docs/issues/egress_proxy_pool.md`) — filed
  separately if needed.

### 5. `src/tools/research/utils/network.py`

- `ProxySettings` import from `browser_use.browser.profile` (:162) — part of
  the local-browser proxy wiring. Verify remaining consumers; remove with it.

### 6. Config / env surface

- `browser.remote` (`auto` | `local`) config key — delete (no modes left).
- `BROWSER_HEADLESS` env handling agent-side — delete (the workspace
  `browser-exec` daemon owns its own headless config).
- `browser.persistence.user_data_dir` agent-side resolution — delete.

### 7. Dependencies and the agent image

After steps 1-5, `browser_use` has **zero** imports left in `src/` (current
consumers: context.py:668, papers.py:439, browser_direct.py:172-215,
utils/network.py:162 — all in the removal set):

- `requirements.txt:24-26` — drop `browser-use` and `playwright` from the
  agent's requirements.
- `docker/Dockerfile.agent:124` — drop `RUN playwright install --with-deps
  chromium`. This removes a full Chromium + system deps (~hundreds of MB and
  its CVE surface) from the **agent** image.
- The **workspace** image keeps Playwright/Chromium for the `browser-exec`
  daemon — `.playwright-version` remains the pin source of truth for Packer +
  `Dockerfile.workspace`. Only the agent image changes.
- Update the `playwright install chromium` line in CLAUDE.md / README setup
  instructions accordingly.

### 8. Tests

- `tests/tools/research/test_browser_tools.py` — `TestGetBrowserConfig`
  (:228+) and the `_is_remote_browser` tests (:332+) test the deleted
  branches; remove or rewrite against the `browser_exec` dispatch.
- Add a regression guard that the agent runtime has no local browser path:
  e.g. a test asserting `browser_use` is not importable from agent runtime
  modules, or a CI grep that `from browser_use` does not appear under `src/`.

## What stays (explicitly out of scope)

- `ToolContext.browser_exec()` and the workspace-side `browser-exec` daemon —
  the production browser path, unchanged.
- Registration of the `browser_*` direct tools
  (`config/persistent_defaults.yaml:63-72`).
- `browser_security.py` URL validation — still useful as defense-in-depth on
  the daemon path.

## Hardening that lands with the removal

Tool registration should gate on **capability, not inference**: register
`browser_direct` tools only when the workspace backend can execute the daemon
(`supports_shell` / a future explicit capability flag), instead of inferring
placement from `backend.host`. Combined with the removal, the failure mode for
a misconfigured or workspace-less session becomes "browser tools absent or
erroring loudly" — never "silently executing in the wrong place". This is the
same defense-in-depth shape as the phase-restricted tools (schema binding
primary, runtime gate backup).

## Accepted behavior changes

- Bare-metal / Compose dev (`agent.py --loop` without a remote workspace)
  loses browser tooling. Acceptable: that stack is deprecated
  (`docs/issues/deprecate_docker_compose_stack.md`); k3d is the dev target and
  uses workspace pods with `browser-exec`.
- papers.py loses its browser-download fallback — which is already
  non-functional on the cluster (broken CDP path), so net cluster behavior is
  unchanged.
- Agent image shrinks and drops the Chromium supply-chain surface.

## Verification

1. `grep -rn "browser_use" src/` → no hits; `grep -rn "_is_remote_browser\|get_browser_session" src/ tests/` → no hits outside removed tests.
2. `pytest tests/tools/research/ -x -q` + `ruff check src/ tests/`.
3. Agent image builds without the Playwright layer; note the size delta.
4. k3d smoke: create a session, drive `browser_navigate`/`browser_snapshot` —
   still served by the workspace `browser-exec` daemon; verify
   `chromium` is **not** present in the agent pod
   (`kubectl exec deploy/<agent> -- which chromium || true`).

## Related

- `docs/features/browser_workspace_executor.md` — the migration this completes.
- `docs/issues/deprecate_docker_compose_stack.md` — removes the dev posture
  that justified the fallback; the sibling ShellManager → local-libtmux
  degradation should die with it.
- `docs/issues/egress_proxy_pool.md` — future shared browser/egress pool
  (where browser capability for workspace-less "lite" agents would live).
- Planned agent-pod egress NetworkPolicy (companion control: the agent pod
  currently has no NetworkPolicy at all; tracked with the no-workspace /
  lite-mode design discussion).
