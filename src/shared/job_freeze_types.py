"""Shared freeze-type registry for agent/orchestrator continuation contracts.

This module is deliberately stdlib-only because both independently deployed
images import it.  A freeze belongs in a set only when every consumer of that
set agrees on its lifecycle semantics; human-review and terminal freezes must
remain outside the automatic continuation sets.

Rollout invariant: orchestrators must understand a new agent-emitted freeze
type before any agent is allowed to emit it.  In particular, worker batch
admission stays disabled until ``batch_boundary`` is deployed everywhere.
"""

FREEZE_TYPE_BATCH_BOUNDARY = "batch_boundary"
FREEZE_TYPE_VERSION_UPGRADE = "version_upgrade"

# Clean checkpoint boundaries that continue the same logical job in a new
# process.  Both resolve to ``paused`` at completion time.
CONTINUE_AS_NEW_FREEZE_TYPES: frozenset[str] = frozenset(
    {
        FREEZE_TYPE_BATCH_BOUNDARY,
        FREEZE_TYPE_VERSION_UPGRADE,
    }
)

# Pauses which the legacy jobs-row dispatcher may automatically redispatch.
# The orphan sweep also uses this exact set when clearing a row-level freeze.
AUTO_REDISPATCH_FREEZE_TYPES: frozenset[str] = CONTINUE_AS_NEW_FREEZE_TYPES | frozenset(
    {
        "memory_unavailable",
        "kb_unavailable",
        "workspace_upgrade_required",
    }
)

# A coincident interrupt/error must not override these durable pause outcomes.
# LLM outage has its own backoff dispatcher rather than the ordinary auto lane.
ERROR_IMMUNE_FREEZE_TYPES: frozenset[str] = AUTO_REDISPATCH_FREEZE_TYPES | frozenset(
    {"llm_unavailable"}
)

# END-checkpoint resumes which should clear the old freeze before continuing.
# This currently has the same membership as the coincident-error set, but the
# separate name keeps the agent contract explicit if those semantics diverge.
AUTO_CONTINUE_FREEZE_TYPES: frozenset[str] = ERROR_IMMUNE_FREEZE_TYPES

# Subjobs can reuse the shared type-specific pause/retry paths for this subset.
# workspace_upgrade_required intentionally retains its existing subjob review
# behavior until that lifecycle is designed independently.
SUBJOB_REDISPATCH_FREEZE_TYPES: frozenset[str] = (
    CONTINUE_AS_NEW_FREEZE_TYPES
    | frozenset(
        {
            "memory_unavailable",
            "kb_unavailable",
            "llm_unavailable",
        }
    )
)
