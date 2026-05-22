---
tags:
  - helm
  - chart
  - direct-session-websockets
  - low-priority
status: open
priority: low
created: 2026-05-22
---

# `sessionRouter.ingressHost` doesn't honor `global.hostnames.api` override

## Context

The [[direct_session_websockets]] feature's Helm value `sessionRouter.ingressHost` (in `helm/values.yaml`) defaults to `api.<global.domain>` via a literal `printf` in `helm/templates/orchestrator/deployment.yaml`:

```yaml
value: {{ .Values.sessionRouter.ingressHost | default (printf "api.%s" .Values.global.domain) | quote }}
```

This bypasses the chart's existing per-component hostname override mechanism, where `global.hostnames.api` can rename the `api` subdomain. Customers who set `global.hostnames.api: "api-srw.example.com"` will see the orchestrator API at that hostname, but the session router will still try to emit `api.example.com` as the per-session Ingress host — breaking direct-WS routing for them.

## Workaround

Customers who use `global.hostnames.api` should also explicitly set `sessionRouter.ingressHost` to the same value.

## Fix

Replace the literal `printf` with the existing `srw.host` helper:

```yaml
value: {{ .Values.sessionRouter.ingressHost | default (include "srw.host" (dict "context" . "key" "api" "default" "api")) | quote }}
```

(Verify the helper's exact signature in `helm/templates/_helpers.tpl` before applying.)

## Related

- [[direct_session_websockets]]
