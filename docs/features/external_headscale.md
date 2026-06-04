---
tags:
  - deployment
  - infrastructure
  - networking
---

# External Headscale Server

The SRW chart **does not** deploy headscale itself. The headscale coordination
server lives outside the chart's release lifecycle and is consumed as an
external service. SRW only needs:

1. The **URL** of an existing headscale instance (`headscale.url` chart value).
2. A **pre-auth key** with the `tag:agent` tag stored in Vault as
   `TAILSCALE_AUTH_KEY` in the existing `srw` ExternalSecret blob.

The agent pods' tailscale sidecar uses those two pieces to register on the
tailnet. Nothing else needs to be wired up on the SRW side.

## Why external

Headscale is shared infrastructure (long-lived control plane, SQLite-backed
identity, ACLs, DERP keys). Coupling it to the SRW chart's lifecycle was
fragile: every `helm upgrade` could roll the StatefulSet and kick every node
off the tailnet, `helm uninstall` would tear down the whole mesh, the SQLite
PVC was bound to chart-name conventions, and a single headscale couldn't
serve multiple SRW environments. Splitting the two means SRW redeploys
never touch headscale.

The bootstrap step (creating the user + keys) was unavoidable in either
shape — headscale has no first-boot init mode that materializes credentials
to Vault — so embedding it in the chart had no operational upside.

## Architecture

```
┌─────────────────────────────────┐         ┌──────────────────────────────┐
│ headscale (separate Fleet bundle│         │ SRW main cluster (chart)     │
│  HomeLab/deployments_managed/   │         │                              │
│   headscale/)                   │         │  agent pod                   │
│                                 │         │  ┌─────────────┐             │
│   Namespace: headscale          │         │  │ agent       │             │
│   StatefulSet: headscale        │         │  │ container   │             │
│   Service: ClusterIP :443       │         │  └─────────────┘             │
│   IngressRouteTCP (HostSNI      │         │  ┌─────────────┐             │
│     passthrough on websecure)   │◄────────┼──┤ tailscale   │             │
│   Certificate (DNS-01 via       │ join    │  │ sidecar     │             │
│     cloudflare-dns-issuer)      │ tailnet │  │ TS_AUTHKEY  │             │
│                                 │         │  └─────────────┘             │
└─────────────────────────────────┘         └──────────────────────────────┘
        ▲                                                ▲
        │ admin API                                      │ TS_AUTHKEY
        │                                                │ from Vault
┌─────────────────┐                            ┌─────────────────────┐
│ vm-controller   │                            │ Vault               │
│ (vm cluster)    │                            │  homelab/superhuman-│
│  HEADSCALE_URL  │                            │    remote-worker/   │
│  HEADSCALE_API_ │◄───── api-key ─────────────┤    srw-secrets      │
│    KEY          │                            │  homelab/agent-vms/ │
└─────────────────┘                            │    headscale-api-key│
                                               └─────────────────────┘
```

Two independent consumers, two Vault secrets, one shared headscale.

## SRW chart values

The relevant block in `helm/values.yaml`:

```yaml
headscale:
  # Full URL. Empty -> falls back to https://headscale.<global.domain>
  url: ""

agent:
  tailscale:
    enabled: false   # Skip the sidecar entirely when no tailnet is in use
```

Per-environment overrides in `deployment/values-experimental.yaml`:

```yaml
headscale:
  url: "https://headscale.h4ll.app"

agent:
  tailscale:
    enabled: true
```

The chart used to expose `headscale.enabled`, `headscale.image`,
`headscale.storageClass`, etc. — those are gone. The only knob is `url`.

## What gets wired up at render time

- `srw.headscaleUrl` helper resolves `.Values.headscale.url`, falling back
  to `https://headscale.<global.domain>`.
- The configmap exports `HEADSCALE_URL` and `AGENT_TAILSCALE_ENABLED`.
- The orchestrator deployment receives both as env vars (optional refs).
- The static agent chart template includes the tailscale sidecar
  conditional on `.Values.agent.tailscale.enabled`.
- The dynamic agent provisioner (`agent_provisioner.py`) reads
  `AGENT_TAILSCALE_ENABLED` + `HEADSCALE_URL` and conditionally appends
  the sidecar + state volume. With the gate off, no sidecar is added at
  all — pods come up `1/1`.

## Standalone headscale deployment

`HomeLab/deployments_managed/headscale/` ships the standalone bundle:

- `00-namespace.yaml` — `headscale` namespace
- `02-configmap.yaml` — `headscale-config` (server config) + `headscale-acl`
  (HuJSON ACL: `tag:agent` → `tag:vm:22`)
- `20-headscale.yaml` — single-replica StatefulSet (SQLite is the only
  supported backend; multi-replica corrupts the DB), Longhorn-backed PVC
- `30-services.yaml` — ClusterIP (HTTPS+metrics) + NodePort (STUN/UDP)
- `31-ingress.yaml` — cert-manager `Certificate` via
  `cloudflare-dns-issuer` + Traefik `IngressRouteTCP` with TLS passthrough
  (headscale terminates TLS itself; Traefik HTTP/2 ALPN breaks DERP's
  HTTP/1.1 Upgrade)
- `fleet.yaml` — independent Fleet bundle (no `dependsOn` on SRW)
- `README.md`

DNS resolution is internal-only on the homelab MikroTik
(`headscale.h4ll.app → 10.0.51.11`). cert-manager issues a Let's Encrypt
cert via DNS-01 against the Cloudflare zone — only a TXT challenge record
is briefly written and removed; no public A/AAAA is needed.

## Bootstrap

After the StatefulSet comes up `Ready`, run:

```bash
bash headscale-bootstrap.sh
```

The script execs into the headscale pod (no network call to
`headscale.<domain>` involved), creates the user, mints an admin API key
and a tagged reusable pre-auth key, and prints them along with the exact
`vault kv` commands to store each.

All defaults can be overridden via env vars — see `bash
headscale-bootstrap.sh --help`. Notable knobs:

| Env var | Default | Purpose |
|---|---|---|
| `KUBE_CONTEXT` | `main` | which cluster headscale lives on |
| `HEADSCALE_NAMESPACE` | `headscale` | namespace selector |
| `HEADSCALE_POD_LABEL` | `app=headscale` | pod selector |
| `HEADSCALE_USER` | `srw` | tailnet user |
| `API_KEY_TTL` | `8760h` | admin API key lifetime |
| `PREAUTH_KEY_TTL` | `8760h` | pre-auth key lifetime |
| `PREAUTH_KEY_TAGS` | `tag:agent` | tags applied to the pre-auth key |
| `VAULT_SRW_PATH` | `secret/homelab/superhuman-remote-worker/srw-secrets` | where `TAILSCALE_AUTH_KEY` goes |
| `VAULT_VMS_PATH` | `secret/homelab/agent-vms/headscale-api-key` | where `api-key` goes |

The agent pre-auth key is always created with `--ephemeral` (not a knob): agent
pods are transient, so their nodes must be reaped by headscale's inactivity GC
or they leak. See `docs/done/headscale_mesh.md`.

The script doesn't touch the headscale URL — that hostname only lives in
the standalone bundle's manifests (server config, cert, ingress route)
and in the consumer-side env vars (`headscale.url` chart value;
`HEADSCALE_URL` in the vm-controller).

## Vault layout

Two separate paths, two consumers:

| Path | Key | Consumer | Cluster |
|---|---|---|---|
| `secret/homelab/superhuman-remote-worker/srw-secrets` | `TAILSCALE_AUTH_KEY` | agent tailscale sidecar | main |
| `secret/homelab/agent-vms/headscale-api-key` | `api-key` | vm-controller | vm |

Both are picked up by existing ExternalSecrets:

- `superhuman-remote-worker/srw` (main) — already syncs the entire
  `srw-secrets` blob; `TAILSCALE_AUTH_KEY` rides along automatically.
- `agent-vms/headscale-api-key` (vm) — defined in
  `deployment-vms/srw-vm-controller/01-eso.yaml`.

After patching Vault, force ESO to refresh:

```bash
kubectl --context main annotate externalsecret srw \
  -n superhuman-remote-worker \
  force-sync=$(date +%s) --overwrite

kubectl --context vm annotate externalsecret headscale-api-key \
  -n agent-vms \
  force-sync=$(date +%s) --overwrite
```

`HEADSCALE_API_KEY` is **not** referenced anywhere in the SRW main chart,
orchestrator, or `src/` — it's only used by the vm-controller. So
`srw-secrets` does not need it.

## Disabling the sidecar

When the tailnet isn't needed (e.g. Docker Compose dev, or a deployment
with no KubeVirt VMs), set:

```yaml
agent:
  tailscale:
    enabled: false
```

That's enough. Both the static `agent` Deployment and the dynamically
provisioned `srw-agent-j-*` / `srw-agent-s-*` pods skip the sidecar
entirely. The agent container still runs; it just won't have a tailnet
interface.

## Using a different headscale (e.g. tailscale.com or a customer-hosted
coordination server)

Set `headscale.url` to the alternate URL and put a matching pre-auth key
in Vault under `TAILSCALE_AUTH_KEY`. No chart code changes required —
the sidecar passes the URL straight to `tailscale up --login-server=...`.

For a fully Tailscale-managed setup (rather than self-hosted headscale),
use `https://controlplane.tailscale.com` and a Tailscale-issued auth key.

## Historical context

The original design — headscale embedded in the SRW chart with a
`headscale.enabled` toggle — is documented in
[`docs/done/headscale_mesh.md`](../done/headscale_mesh.md). That doc is
preserved for context but the chart no longer carries those templates.
