# Enable the agent-pod egress NetworkPolicy (per-deployment checklist)

**Status**: Open — the policy is implemented and shipped **default-off**
(`agent.networkPolicy.enabled: false`); enabling it per deployment is the
remaining step. Filed 2026-06-11 with the policy
(`helm/templates/agent/network-policy.yaml`,
`no_workspace_agent_mode.md` §9.1 / S4).

## Why it ships default-off

Agent pods previously had **no** NetworkPolicy. Turning on egress
restriction against a live cluster can break the agent's outbound paths if
one is missed — most importantly **model access**. The stock chart is safe
(no in-cluster LLM; `llm.seed.systemEndpoints` defaults to `[]`, so external
LLM/Keycloak/Tavily all work over the 443 wildcard), but a real deployment
may route LLM, Keycloak, or cloud through an **RFC1918** address that the
`except` list denies. Those need an `extraEgress` carve-out *before* the
toggle flips, so enablement is a deliberate per-deployment action, not a
chart default.

## What the policy already allows

In-cluster, by podSelector: orchestrator (8085), NATS (4222), Postgres +
pgvector (5432), Neo4j (7688), MongoDB (27017), Nextcloud (80) / OpenCloud
(9200) when enabled, workspace pods (SSH 30022 + CDP 9222). Plus kube-dns
(53) and public internet TCP 80/443/22 minus the `except` CIDRs (cluster +
home LAN + link-local/metadata).

## Enablement checklist (per deployment / values overlay)

1. **Discover the agent's real outbound endpoints**, especially LLM,
   embeddings, and Keycloak. From the resolved config / running pod:
   ```sh
   # System LLM/embedding endpoints (DB-backed catalog)
   kubectl exec deploy/<orchestrator> -- \
     psql "$APP_DB_URL" -c "select label, base_url from llm_endpoints;"
   # Keycloak issuer the agent uses for cloud token-exchange
   kubectl exec <agent-pod> -- printenv | grep -iE 'KC_|KEYCLOAK|ISSUER|EMBEDDING_BASE_URL|.*BASE_URL'
   ```
2. **Classify each base_url:**
   - Public DNS / public IP → already allowed by the 443 wildcard. Nothing to do.
   - In-cluster, **same** namespace as a charted component above → already
     allowed by its podSelector.
   - In-cluster, **other** namespace (e.g. `vllm.ai.svc.cluster.local`) →
     add a `namespaceSelector` carve-out.
   - Home LAN / other RFC1918 (e.g. `ai.h4ll.app` → `192.168.178.x`) → add
     an `ipBlock` carve-out.
3. **Add the carve-outs** to `agent.networkPolicy.extraEgress` in the
   deployment's values overlay. Examples:
   ```yaml
   agent:
     networkPolicy:
       enabled: true
       extraEgress:
         # in-cluster LLM in the `ai` namespace
         - to:
             - namespaceSelector:
                 matchLabels:
                   kubernetes.io/metadata.name: ai
           ports:
             - { protocol: TCP, port: 8000 }
         # LAN LLM router / external-but-LAN Keycloak
         - to:
             - ipBlock: { cidr: 192.168.178.30/32 }
           ports:
             - { protocol: TCP, port: 443 }
   ```
4. **Stage on dev first.** Enable in `deployment/values-experimental.yaml`,
   deploy, then exercise an agent end-to-end: a chat turn (LLM reachable),
   a datasource query, a cloud-sync session (Keycloak token-exchange +
   WebDAV), and a delegated subjob (orchestrator). Watch for connection
   timeouts in agent logs — each is a missing carve-out.
5. **Promote to prod overlays** (`srv-prod-private`, customer values) only
   after dev is clean; repeat the discovery there (different LLM/Keycloak
   topology likely).

## Verification once enabled

```sh
# Metadata endpoint must be unreachable from the agent pod:
kubectl exec <agent-pod> -- curl -s -m3 http://169.254.169.254/ ; echo "exit=$?"  # expect non-zero (blocked)
# A legitimate model call must still succeed (chat turn in the cockpit).
```

## Related

- `helm/templates/agent/network-policy.yaml` — the policy.
- `helm/templates/workspace-network-policy.yaml` — the tiered sibling whose
  `ipBlock`-except pattern this mirrors.
- `docs/features/no_workspace_agent_mode.md` §9.1 — prerequisite context;
  lite mode makes this a hard requirement (the lite values overlay should
  enable it with verified carve-outs).
- `docs/features/workspace_network_isolation.md` §3 — the tier model and
  the cluster-CIDR rationale.
