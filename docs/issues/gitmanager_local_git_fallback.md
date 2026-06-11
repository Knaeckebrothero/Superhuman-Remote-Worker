# GitManager retains a local-subprocess git fallback

**Status**: Open — deferred hardening. Filed 2026-06-11 (fallout from the
`no_workspace_agent_mode` §9.4 datasource-clone audit).

## Context

The §9 prerequisite hardening removed three local-execution fallbacks from
the agent pod (in-pod browser, ShellManager → local libtmux, and the
repository-datasource subprocess `git clone`). One member of the family
remains: `GitManager` (`src/managers/git_manager.py`) still branches on
`use_backend = backend is not None and getattr(backend, "supports_shell",
False)` in `clone()` / `from_worktree()`, and instances constructed without
a backend run every git operation as a local subprocess — capability
*inference* with a silent local degradation, the exact shape the other
removals eliminated.

## Why it was deferred (lower risk than the removed fallbacks)

- It is reached only by the job-repo / workspace-versioning flows
  (`src/core/workspace.py` repo provisioning, `src/agent.py` pod-handoff
  clone), which always pass the workspace backend in cluster deployments.
- Repository **datasources** can no longer route into it without a backend:
  `clone_repository_datasources()` gates on `supports_shell` before any
  GitManager call and skips loudly otherwise.
- The lite tiers (`virtual`/`none`) reject repository datasources at
  dispatch and run git-off profiles (`git_versioning: false`), so S1/S2 of
  the no-workspace work do not expose it.
- Unit tests legitimately exercise the local path against tmp dirs — a
  hard-off needs a story for them, unlike the browser/shell cases.

## Risk

A workspace-less or shell-less context that still constructs a GitManager
would silently run git — including network fetches of
attacker-influenceable remotes — inside the credential-holding agent pod.
Dormant today; armable by a future refactor. Same argument as
`remove_local_browser_fallback.md`.

## Fix shape (when picked up)

Mirror the ShellManager hard-off: require a shell-capable backend at
construction (or an explicit opt-in reserved for unit tests, e.g. a
`local_for_tests=True` flag or a test-only subclass), delete the
subprocess branches from `clone`/`from_worktree`/`_run_git`, and let the
existing git-off gating (`git_mgr.is_active`) remain the degraded state.
Sequence after `docs/issues/deprecate_docker_compose_stack.md` lands —
the bare-metal/Compose posture is the last legitimate local user.

## Related

- `docs/features/no_workspace_agent_mode.md` §9 — the prerequisite series
  this completes.
- `docs/issues/remove_local_browser_fallback.md` — the pattern and its
  rationale.
- `docs/issues/datasource_legacy_dead_code.md` — sibling cleanup filed
  from the same audit.
