"""Allowed saved/default and explicit-create session workspace tiers."""

# Session workspace tiers allowed as saved defaults, and
# the platform default applied when neither the request nor the owner's saved
# settings.persistent_agent.workspace_backend pick one. An S3 object store is
# an assumed platform prerequisite (knowledge-history/done/s3_object_store_bundled_fallback.md),
# so the default is the instant lite tier — see
# knowledge-base/knowledge/features/instant_landing_session.md.
SESSION_WORKSPACE_BACKENDS = ("sandbox", "virtual", "none")
SESSION_DEFAULT_WORKSPACE_BACKEND = "virtual"

# Backends a caller may *explicitly* select at session creation. ``vm`` is
# creatable (operator-gated + provisioned via KubeVirt, see create_thread) but
# deliberately NOT in SESSION_WORKSPACE_BACKENDS: it must never be an implicit
# or saved default (a KubeVirt VM per session is expensive), so it is a
# per-session opt-in only and is excluded from the default chain
# (_default_session_workspace_backend) and the settings-PATCH validator.
SESSION_CREATE_WORKSPACE_BACKENDS = SESSION_WORKSPACE_BACKENDS + ("vm",)
