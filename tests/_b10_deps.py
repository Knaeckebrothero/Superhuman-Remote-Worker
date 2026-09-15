"""Shared per-request dependency builders for B10-extracted route tests.

The extracted routers resolve collaborators from the owning application at
request time. Direct handler calls in tests pass explicit dependencies built
from the (patched) orchestrator.main globals, so existing monkeypatches on
main-owned collaborators keep flowing into the route bodies.
"""


def _tp_deps(main=None):
    """ThreadProjectionDependencies from orchestrator.main's current globals."""
    if main is None:
        import orchestrator.main as main
    from orchestrator.routers.thread_projection import ThreadProjectionDependencies

    return ThreadProjectionDependencies(
        store=main.postgres_db,
        vector_db=main.vector_db,
        resolve_cloud_session_url=main._resolve_cloud_session_url,
        resolve_session_config=main._resolve_session_config,
        enforce_session_create_grants=main._enforce_session_create_grants,
        acknowledged_grant_strip=main._acknowledged_grant_strip,
        agent_toolset_measurement=main._agent_toolset_measurement,
        prefetch_roster_refs=main._prefetch_roster_refs,
        resolve_runner_grants=main._resolve_runner_grants,
        session_config_dependencies=main._session_config_dependencies,
        user_experts_enabled=main._user_experts_enabled,
    )


def _tt_deps(main=None):
    """ThreadTransportDependencies from orchestrator.main's current globals."""
    if main is None:
        import orchestrator.main as main
    from orchestrator.routers.thread_transport import ThreadTransportDependencies

    return ThreadTransportDependencies(
        store=main.postgres_db,
        schedule_stateless_workspace_ensure=main._schedule_stateless_workspace_ensure,
        protected_cloud_delivery_state=main._protected_cloud_delivery_state,
    )
