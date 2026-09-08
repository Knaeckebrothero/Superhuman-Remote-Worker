"""Capability-grant enforcement: the PEPs over the grant resolver.

Extracted verbatim from ``orchestrator.main`` (R1.B05 lane P, census group
``R_GRANTS``). ``services.grants_service.resolve_grants_for`` and
``shared.runtime.core.capability_grants`` stay the RESOLVER and the PDP; this
module is the policy *over* them and re-derives neither. Every function here
answers "may this principal have this config", never "what are this principal's
grants".

Five decision points, deliberately not one:

* **Save time** — :func:`enforce_save_grants` refuses outright (422) for four
  expert-write routes; :func:`strip_save_grants` strips-and-reports for
  ``duplicate_expert`` alone. Both resolve through
  :func:`resolve_user_save_grants` so the admin bypass cannot drift into two
  checks. ``strip_save_grants`` RE-EVALUATES the stripped result and still
  refuses on a residual violation — that is what makes an incomplete
  ``strip_to_grants`` entry a false refusal rather than a permitted escape.
* **Dispatch time** — :func:`enforce_dispatch_grants` is the authoritative PEP
  and raises :class:`GrantDenied`, which callers must NOT swallow in a resolve
  fallback.
* **Create time** — :func:`enforce_session_create_grants` /
  :func:`enforce_job_create_grants` run the SAME PDP early and translate the
  denial to 422, so a never-startable config is rejected synchronously instead
  of failing later. They fail callers early; they never grant.
* **Upgrade time** — :func:`enforce_workspace_upgrade_grants_for_config` re-runs
  the dispatch PDP on the POST-upgrade config, plus the VM operator gate.
* **Drift acknowledgement** — :func:`strip_acknowledged_grants` drops ONLY the
  acknowledged violations, and leaves the fragment completely untouched when
  anything unacknowledged drifted, so one acknowledgement can never smuggle a
  different grant through.

Refusal shapes are the contract. :class:`GrantDenied` carries ``violations`` as
a plain ``list[str]``; :func:`grant_violations_detail` produces the exact string
the HTTP layer renders. A dict where a string was expected is a contract break.

Copy semantics: :func:`strip_acknowledged_grants` returns the CALLER's fragment
unchanged on no-violation and on unacknowledged-drift, and a new stripped dict
only when it actually strips.

Collaborators arrive per invocation through
:class:`GrantEnforcementDependencies`. The four callable fields are not
convenience: ``orchestrator.main`` exposes each as a name that tests rebind, and
resolving them here in the module's own namespace would make such a rebind
green but inert (B05 port contract §P3).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from orchestrator.security.access import user_visible_project_ids

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GrantEnforcementDependencies:
    """Collaborators for one grant decision, resolved per invocation.

    ``store`` is main's ``postgres_db``. The remaining fields are the
    application's own wrappers over functions defined IN this module; they are
    injected rather than called directly because each is a documented seam that
    tests rebind on ``orchestrator.main``:

    * ``user_experts_enabled`` — the runtime kill-switch, checked by the expert
      save prelude.
    * ``resolve_runner_grants`` — consulted by :func:`enforce_dispatch_grants`.
    * ``enforce_dispatch_grants`` — the authoritative PEP the create-time and
      upgrade-time gates delegate to.
    * ``check_vm_permission`` — the VM operator gate
      (``services.vm_workspace_policy``), kept on top of the ``vm_workspace``
      capability grant rather than replacing it.
    """

    store: Any
    user_experts_enabled: Callable[[], Awaitable[bool]]
    resolve_runner_grants: Callable[..., Awaitable[dict[str, Any] | None]]
    enforce_dispatch_grants: Callable[..., Awaitable[None]]
    check_vm_permission: Callable[..., Awaitable[None]]


class GrantDenied(Exception):
    """A merged config exceeds the runner's grants (dispatch PEP). Must NOT be
    swallowed by a resolve fallback (fail closed)."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


async def user_experts_enabled(*, dependencies: "GrantEnforcementDependencies") -> bool:
    """Runtime kill-switch (decision 8). Absent row = enabled (fail-open for fresh
    installs). When disabled, DB-expert creation + grant enforcement are off."""
    try:
        row = await dependencies.store.get_system_setting("user_experts")
    except Exception:
        logger.exception("user_experts read failed; fail-open")
        return True
    value = (row or {}).get("value") or {}
    return not (isinstance(value, dict) and value.get("enabled") is False)


def grant_violations_detail(violations: list[str]) -> str:
    return "config exceeds your capability grants: " + "; ".join(violations)


async def grant_project_ids(
    user: dict, *, dependencies: "GrantEnforcementDependencies"
) -> list[str]:
    """Project scope ids for grant resolution. user_visible_project_ids returns
    'all' for admins (who bypass anyway) — treat as no project constraint."""
    vis = await user_visible_project_ids(user, dependencies.store)  # security/access.py
    return [] if vis == "all" else [str(p) for p in vis]


async def scan_raw_request_fragment(request: Request) -> None:
    """Slice-2 hardening (decision 10): scan the RAW request bytes for duplicate
    or non-ASCII keys (parser-differential + unicode-confusable defenses) that the
    parsed body has already silently collapsed. 422 on offence. Best-effort — if
    the body can't be re-read the parsed-dict hard-deny scan still ran."""
    from shared.runtime.core.expert_resolution import scan_fragment_text

    try:
        raw = (await request.body()).decode("utf-8")
    except Exception:
        return
    if not raw.strip():
        return
    offending = scan_fragment_text(raw)
    if offending:
        raise HTTPException(
            status_code=422,
            detail="config rejected (malformed, duplicate/non-ASCII, or credential "
            "keys): " + "; ".join(offending),
        )


async def resolve_user_save_grants(
    user: dict[str, Any], *, dependencies: "GrantEnforcementDependencies"
) -> dict[str, Any] | None:
    """Resolve save-time grants for `user`, or ``None`` on an admin bypass.

    Split out of the body of ``enforce_save_grants`` so its refuse-outright
    policy and ``strip_save_grants``'s strip-and-report policy (the
    ``duplicate_expert`` route only, decision 2026-08-04) resolve grants via
    one path and cannot drift into two admin checks."""
    if user.get("is_admin"):
        return None
    from orchestrator.services.grants_service import resolve_grants_for

    return await resolve_grants_for(
        dependencies.store,
        user_id=str(user["id"]),
        project_ids=await grant_project_ids(user, dependencies=dependencies),
    )


async def enforce_save_grants(
    config: dict[str, Any],
    *,
    user: dict[str, Any],
    dependencies: "GrantEnforcementDependencies",
) -> None:
    """Save-time PEP (decision 9): the author's grants must cover the raw fragment.
    422 naming offending keys. Admins bypass."""
    grants = await resolve_user_save_grants(user, dependencies=dependencies)
    if grants is None:
        return
    from shared.runtime.core.capability_grants import evaluate

    violations = evaluate(config, grants)
    if violations:
        raise HTTPException(status_code=422, detail=grant_violations_detail(violations))


async def strip_save_grants(
    config: dict[str, Any],
    *,
    user: dict[str, Any],
    dependencies: "GrantEnforcementDependencies",
) -> tuple[dict[str, Any], list[str]]:
    """``duplicate_expert``'s save-time grants policy (2026-08-04 decision,
    knowledge-base/knowledge/superpowers/plans/2026-08-04-expert-write-gate-holes.md, task 3):
    strip what the copier's grants forbid from the source config instead of
    refusing the fork outright, and report the grant keys that were dropped.
    ``enforce_save_grants`` above is unchanged and still refuses for the
    other four expert-write routes — measured against the real PDP with
    default grants, refusing here blocked 7 of the 11 shipped experts,
    including the one the route's own docstring names ("start from scholar").

    Admins bypass exactly as ``enforce_save_grants`` does: unmodified config,
    nothing dropped.

    THE SAFETY PROPERTY, not optional: ``evaluate`` is re-run on the STRIPPED
    result, and any violation that survives is refused with the same 422
    ``enforce_save_grants`` raises. This is what makes an incomplete or wrong
    entry in ``capability_grants.strip_to_grants`` merely a false refusal,
    never a permitted escape.
    """
    grants = await resolve_user_save_grants(user, dependencies=dependencies)
    if grants is None:
        return config, []
    from shared.runtime.core.capability_grants import evaluate, strip_to_grants

    stripped, dropped = strip_to_grants(config, grants)
    residual = evaluate(stripped, grants)
    if residual:
        raise HTTPException(status_code=422, detail=grant_violations_detail(residual))
    return stripped, dropped


async def enforce_expert_save_prelude(
    request: Request, *, dependencies: "GrantEnforcementDependencies"
) -> None:
    """Kill-switch (403) + raw dup/non-ASCII key scan (422): the two save-time
    checks shared by every expert-write route regardless of how its grants
    half is enforced. ``enforce_expert_save`` below runs this then
    ``enforce_save_grants``; ``duplicate_expert`` runs this then
    ``strip_save_grants`` instead, so the kill-switch and the scan cannot be
    forgotten while that one route's grants policy differs from the other
    four."""
    if not await dependencies.user_experts_enabled():
        raise HTTPException(
            status_code=403,
            detail="User-defined experts are disabled by the administrator",
        )
    await scan_raw_request_fragment(request)


async def enforce_expert_save(
    request: Request,
    config: dict[str, Any],
    *,
    user: dict[str, Any],
    dependencies: "GrantEnforcementDependencies",
) -> None:
    """Combined save-time gate: kill-switch (403) + raw dup/non-ASCII key scan
    (422) + capability-grant enforcement (422). Admins bypass grants, not the
    kill-switch."""
    await enforce_expert_save_prelude(request, dependencies=dependencies)
    await enforce_save_grants(config, user=user, dependencies=dependencies)


async def resolve_runner_grants(
    *,
    runner_user_id: str | None,
    project_ids: list[str],
    runner_kind: str = "user",
    dependencies: "GrantEnforcementDependencies",
) -> dict[str, Any] | None:
    """Resolve effective dispatch grants for the job runner.

    ``None`` means admin bypass. Lifecycle subjobs keep the owner's capability
    grants but run with an elevated autonomy ceiling so system verification and
    research jobs do not pause on the owner's review ceiling.
    """
    user = await dependencies.store.get_user(runner_user_id) if runner_user_id else None
    if user and user.get("is_admin"):
        return None
    from orchestrator.services.grants_service import resolve_grants_for

    grants = await resolve_grants_for(
        dependencies.store, user_id=runner_user_id, project_ids=project_ids
    )
    if runner_kind == "lifecycle":
        grants = dict(grants)
        grants["autonomy_ceiling"] = "full"
    return grants


async def enforce_dispatch_grants(
    merged: dict,
    *,
    runner_user_id: str | None,
    project_ids: list[str],
    runner_kind: str = "user",
    dependencies: "GrantEnforcementDependencies",
) -> None:
    """Authoritative dispatch PEP: merged config must fit runner grants."""
    from shared.runtime.core.capability_grants import evaluate

    grants = await dependencies.resolve_runner_grants(
        runner_user_id=runner_user_id,
        project_ids=project_ids,
        runner_kind=runner_kind,
    )
    if grants is None:
        return
    violations = evaluate(merged, grants)
    if violations:
        raise GrantDenied(violations)


def strip_acknowledged_grants(
    fragment: dict[str, Any], grants: dict[str, Any], acknowledged: set[str]
) -> dict[str, Any]:
    """Drop acknowledged grant violations from a merged config fragment.

    A violation the user did NOT acknowledge is left in place, so
    ``enforce_dispatch_grants`` still denies on all of it — acknowledging one
    grant must never smuggle a different one through.

    ``strip_to_grants`` is advisory by contract; the authoritative re-check is
    the ``enforce_dispatch_grants`` call that runs on the resulting capture.

    Sync and pure (no grant resolution here) so it can run as
    ``resolve_config``'s ``grant_strip`` hook, applied to the fully-merged
    ``data`` before the delivered blob is built from it — not just to the
    detached ``capture["merged_fragment"]`` copy the PDP evaluates. Stripping
    only the capture leaves the delivered blob carrying the very capability
    the grant revoked (round-1 finding: acknowledging ``shell_tools`` stopped
    the denial but the agent still hydrated ``tools.shell=True``).
    """
    from shared.runtime.core.capability_grants import evaluate, strip_to_grants

    violations = evaluate(fragment, grants)
    if not violations:
        return fragment
    flagged = {v.split(":", 1)[0] for v in violations}
    if not flagged <= acknowledged:
        # Something drifted that was never acknowledged. Leave the fragment
        # untouched and let the dispatch PEP fail closed on all of it.
        return fragment
    stripped, _dropped = strip_to_grants(fragment, grants)
    return stripped


async def enforce_session_create_grants(
    fragment: dict,
    *,
    user_id: str | None,
    project_ids: list[str],
    dependencies: "GrantEnforcementDependencies",
) -> None:
    """Create/update-time PEP for sessions (Layer 2): run the SAME PDP as attach
    (``enforce_dispatch_grants``) on the requested config and translate a denial
    into HTTP 422 — so a never-startable config (e.g. a ``permission_mode`` above
    the owner's ceiling) is rejected synchronously at the API instead of being
    accepted and then failing later at provisioning with an opaque ready timeout.
    Admin owner bypasses (inside ``enforce_dispatch_grants``).
    See knowledge-base/knowledge/issues/session_permission_mode_grant_denied_ready_timeout.md.
    """
    try:
        await dependencies.enforce_dispatch_grants(
            fragment, runner_user_id=user_id, project_ids=project_ids
        )
    except GrantDenied as gd:
        raise HTTPException(
            status_code=422, detail=grant_violations_detail(gd.violations)
        ) from gd


async def enforce_job_create_grants(
    config_override: dict | None,
    *,
    user_id: str | None,
    project_ids: list[str],
    dependencies: "GrantEnforcementDependencies",
) -> None:
    """Submit-time PEP for jobs — the session counterpart above, for the job path.

    Without it, an over-reaching ``config_override`` (autonomy past the ceiling,
    an ungranted model, shell tools) is accepted at create and only denied at
    dispatch, which marks the job ``failed``. A session agent that created the
    job has already reported success by then, so the failure surfaces as a dead
    job rather than a refused request.

    Scoped to the OVERRIDE only: an expert's own fragment is resolved
    server-side after this point and is already PDP-checked at expert save
    (``enforce_expert_save``). Grant scoping is the request's project rather
    than the fully resolved job scope, so this can allow something dispatch
    later denies — it fails callers early, it never grants.
    ``enforce_dispatch_grants`` on the full merged config stays authoritative.

    No-ops for a userless system child (no principal whose grants to resolve).
    """
    if not user_id or not config_override:
        return
    try:
        await dependencies.enforce_dispatch_grants(
            config_override, runner_user_id=user_id, project_ids=project_ids
        )
    except GrantDenied as gd:
        raise HTTPException(
            status_code=422, detail=grant_violations_detail(gd.violations)
        ) from gd


async def enforce_workspace_upgrade_grants_for_config(
    *,
    owner_id: Any,
    config_override: dict | None,
    target_tier: str,
    dependencies: "GrantEnforcementDependencies",
) -> None:
    """Sec-1 — shared upgrade-authorization gate core (server-side, fail-closed).

    Parameterized by the raw ``(owner_id, config_override)`` so BOTH the session
    path (``thread.metadata.config_override``) and the worker path (the
    ``jobs.config_override`` column) reuse one PEP — see the thin
    ``enforce_workspace_upgrade_grants`` (session) /
    ``enforce_job_workspace_upgrade_grants`` (worker, §4.3 W2) wrappers.

    Re-runs the dispatch PDP (``capability_grants.evaluate``) on the POST-UPGRADE
    config — the stored ``config_override`` with ``workspace.backend`` flipped to
    ``target_tier`` — exactly as ``enforce_dispatch_grants`` does at dispatch,
    just re-run at upgrade time:

    - ``target_tier='vm'`` trips the ``vm_workspace`` grant requirement, and
      additionally keeps the operator gate (the global ``vm_workspaces``
      kill-switch + per-user ``can_use_vm`` via
      ``vm_workspace_policy.check_vm_permission``).
    - ``target_tier='sandbox'`` is NOT gated by the backend (the PDP gates only
      ``vm`` and explicitly-declared tool flags), so it passes by default —
      matching "sandbox is the ungated default tier" — unless the config already
      declares a gated tool (e.g. ``tools.shell``) the owner lacks, in which case
      dispatch would have rejected it too.

    Raises ``HTTPException(403)`` on violation. No new grant key, no
    sandbox-specific rule — identical to dispatch-time enforcement.
    """
    # owner_id comes back from asyncpg as a UUID object; get_user (and
    # enforce_dispatch_grants below) expect a string — coerce once, matching the
    # str(user["id"]) convention used elsewhere.
    owner_id = str(owner_id) if owner_id is not None else None
    owner = await dependencies.store.get_user(owner_id) if owner_id else None

    # vm keeps its operator gate (global kill-switch + can_use_vm), on top of the
    # vm_workspace grant the PDP enforces below.
    if target_tier == "vm":
        await dependencies.check_vm_permission(owner, job_needs_vm=True)

    if not isinstance(config_override, dict):
        config_override = {}
    # Post-upgrade config = the frozen override with the backend flipped. A
    # shallow merge of the workspace sub-dict suffices — the PDP only reads
    # workspace.backend plus declared tool/autonomy flags.
    post_upgrade = {
        **config_override,
        "workspace": {
            **(config_override.get("workspace") or {}),
            "backend": target_tier,
        },
    }
    project_ids = (
        await grant_project_ids(owner, dependencies=dependencies) if owner else []
    )
    try:
        await dependencies.enforce_dispatch_grants(
            post_upgrade, runner_user_id=owner_id, project_ids=project_ids
        )
    except GrantDenied as exc:
        raise HTTPException(
            status_code=403, detail=grant_violations_detail(exc.violations)
        ) from exc


async def enforce_workspace_upgrade_grants(
    thread: dict,
    *,
    target_tier: str,
    dependencies: "GrantEnforcementDependencies",
) -> None:
    """Session wrapper over the shared Sec-1 gate — extracts the owner +
    ``config_override`` from a thread row (``metadata.config_override``) and
    delegates to ``enforce_workspace_upgrade_grants_for_config``."""
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    await enforce_workspace_upgrade_grants_for_config(
        owner_id=thread.get("user_id"),
        config_override=metadata.get("config_override") or {},
        target_tier=target_tier,
        dependencies=dependencies,
    )


async def enforce_job_workspace_upgrade_grants(
    job: dict,
    *,
    target_tier: str,
    dependencies: "GrantEnforcementDependencies",
) -> None:
    """Worker wrapper over the shared Sec-1 gate (§4.3 W2). Jobs carry
    ``config_override`` as a top-level JSONB column (not under ``metadata``) and
    ``user_id`` as the owner — extract those and delegate to the shared core."""
    config_override = job.get("config_override") or {}
    if isinstance(config_override, str):
        try:
            config_override = json.loads(config_override)
        except (json.JSONDecodeError, TypeError):
            config_override = {}
    await enforce_workspace_upgrade_grants_for_config(
        owner_id=job.get("user_id"),
        config_override=config_override,
        target_tier=target_tier,
        dependencies=dependencies,
    )


__all__ = [
    "GrantDenied",
    "GrantEnforcementDependencies",
    "enforce_dispatch_grants",
    "enforce_expert_save",
    "enforce_expert_save_prelude",
    "enforce_job_create_grants",
    "enforce_job_workspace_upgrade_grants",
    "enforce_save_grants",
    "enforce_session_create_grants",
    "enforce_workspace_upgrade_grants",
    "enforce_workspace_upgrade_grants_for_config",
    "grant_project_ids",
    "grant_violations_detail",
    "resolve_runner_grants",
    "resolve_user_save_grants",
    "scan_raw_request_fragment",
    "strip_acknowledged_grants",
    "strip_save_grants",
    "user_experts_enabled",
]
