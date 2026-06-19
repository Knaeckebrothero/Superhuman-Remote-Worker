{{/*
Chart name, truncated to 63 chars.
*/}}
{{- define "srw.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name: release-name + chart-name, truncated to 63 chars.
If release name contains chart name, don't repeat it.
*/}}
{{- define "srw.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels applied to all resources.
*/}}
{{- define "srw.labels" -}}
helm.sh/chart: {{ include "srw.chart" . }}
{{ include "srw.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels (subset of common labels — used in matchLabels).
*/}}
{{- define "srw.selectorLabels" -}}
app.kubernetes.io/name: {{ include "srw.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Chart label value.
*/}}
{{- define "srw.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Secret name — resolves across the three secret modes:
  1. existingSecret set → use that name
  2. externalSecrets.enabled → ESO creates secret with fullname
  3. secrets.create → chart creates secret with fullname
All templates reference this single helper.
*/}}
{{- define "srw.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- include "srw.fullname" . }}
{{- end }}
{{- end }}

{{/*
VM SSH key secret name.
*/}}
{{- define "srw.vmSshKeySecretName" -}}
{{- if .Values.secrets.existingVmSshKeySecret }}
{{- .Values.secrets.existingVmSshKeySecret }}
{{- else }}
{{- printf "%s-vm-ssh-key" (include "srw.fullname" .) }}
{{- end }}
{{- end }}

{{/*
ConfigMap name (used by all deployments that read from the shared configmap).
*/}}
{{- define "srw.configMapName" -}}
{{- printf "%s-config" (include "srw.fullname" .) }}
{{- end }}

{{/*
VM controller — resource names + URLs. The controller can run in the same
namespace as the orchestrator (vmController.namespace = .Release.Namespace)
or in a dedicated namespace (the typical case). When enabled, the controller
exposes an HTTP API on Service `srw.vmControllerServiceName` port
`vmController.service.port`.
*/}}
{{- define "srw.vmControllerName" -}}
{{- printf "%s-vm-controller" (include "srw.fullname" .) }}
{{- end }}

{{- define "srw.vmControllerServiceName" -}}
{{- include "srw.vmControllerName" . }}
{{- end }}

{{- define "srw.vmControllerNamespace" -}}
{{- default .Release.Namespace .Values.vmController.namespace }}
{{- end }}

{{/*
URL the orchestrator uses to reach the controller's HTTP API. Empty when
vmController.enabled is false or transport=nats — orchestrator falls back
to NATS / direct K8s in those cases. When transport=both, the URL is
exported so the orchestrator's HTTP transport is available even though
NATS takes priority.
*/}}
{{- define "srw.vmControllerUrl" -}}
{{- if and .Values.vmController.enabled (ne .Values.vmController.transport "nats") }}
{{- printf "http://%s.%s.svc.cluster.local:%d"
    (include "srw.vmControllerServiceName" .)
    (include "srw.vmControllerNamespace" .)
    (int .Values.vmController.service.port) }}
{{- end }}
{{- end }}

{{/*
Component labels — extends common labels with a component identifier.
Usage: {{ include "srw.componentLabels" (dict "context" . "component" "orchestrator") }}
*/}}
{{- define "srw.componentLabels" -}}
{{ include "srw.labels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Component selector labels.
Usage: {{ include "srw.componentSelectorLabels" (dict "context" . "component" "orchestrator") }}
*/}}
{{- define "srw.componentSelectorLabels" -}}
{{ include "srw.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Internal service hostname for a component.
Usage: {{ include "srw.serviceName" (dict "context" . "component" "postgres") }}
*/}}
{{- define "srw.serviceName" -}}
{{- printf "%s-%s" (include "srw.fullname" .context) .component }}
{{- end }}

{{/*
Domain-derived URLs. Each component host is `global.hostnames.<key>` when
set, otherwise `<subdomain>.<global.domain>`. Centralised in srw.host so a
single override propagates to ingress, configmap, OIDC redirects, and the
cockpit env-init script.

Usage:
  {{ include "srw.host" (dict "context" . "key" "git" "default" "git") }}
  {{ include "srw.host" (dict "context" . "key" "cockpit" "default" "") }}  # root
*/}}
{{- define "srw.host" -}}
{{- $ctx := .context -}}
{{- $hostnames := default (dict) $ctx.Values.global.hostnames -}}
{{- $override := index $hostnames .key -}}
{{- if $override -}}
{{- $override -}}
{{- else if eq .default "" -}}
{{- required "global.domain is required" $ctx.Values.global.domain -}}
{{- else -}}
{{- printf "%s.%s" .default (required "global.domain is required" $ctx.Values.global.domain) -}}
{{- end -}}
{{- end }}

{{/*
Strip the scheme from an https:// URL so URL helpers can be reused as
ingress / TLS host strings without duplicating per-component logic.
*/}}
{{- define "srw.urlHost" -}}
{{- . | trimPrefix "https://" | trimPrefix "http://" -}}
{{- end }}

{{/*
URL scheme — "https" when TLS is enabled on the ingress, "http" otherwise.
Centralized so every URL helper picks up local-dev (no-TLS) deployments.
*/}}
{{- define "srw.urlScheme" -}}
{{- if .Values.ingress.tls.enabled -}}https{{- else -}}http{{- end -}}
{{- end }}

{{- define "srw.cockpitUrl" -}}
{{- printf "%s://%s" (include "srw.urlScheme" .) (include "srw.host" (dict "context" . "key" "cockpit" "default" "")) }}
{{- end }}

{{- define "srw.apiUrl" -}}
{{- printf "%s://%s" (include "srw.urlScheme" .) (include "srw.host" (dict "context" . "key" "api" "default" "api")) }}
{{- end }}

{{- define "srw.authUrl" -}}
{{- if and .Values.keycloak.enabled (not .Values.keycloak.internal) .Values.keycloak.externalIssuerUrl }}
{{- .Values.keycloak.externalIssuerUrl }}
{{- else }}
{{- printf "%s://%s" (include "srw.urlScheme" .) (include "srw.host" (dict "context" . "key" "auth" "default" "auth")) }}
{{- end }}
{{- end }}

{{/*
Internal cluster URL for the orchestrator. Used by other in-cluster pods
that POST to the orchestrator without round-tripping through the public
ingress (Keycloak's backchannel-logout callback, future webhook receivers).
Bypassing the ingress avoids two foot-guns: pods can't always resolve the
public hostname (split-DNS, the `localhost` loopback-hijack on k3d, etc.),
and even when they can, the public path is slower and may pin TLS certs
the in-cluster CA bundle doesn't trust.
*/}}
{{- define "srw.orchestratorInternalUrl" -}}
{{- printf "http://%s-orchestrator:8085" (include "srw.fullname" .) -}}
{{- end }}

{{/*
Internal cluster URL for Keycloak — used by orchestrator for back-channel JWKS fetches.
*/}}
{{- define "srw.keycloakInternalUrl" -}}
{{- if .Values.keycloak.internal }}
{{- printf "http://%s-keycloak:8080" (include "srw.fullname" .) }}
{{- else if .Values.keycloak.externalInternalUrl }}
{{- .Values.keycloak.externalInternalUrl }}
{{- else }}
{{- include "srw.authUrl" . }}
{{- end }}
{{- end }}

{{/*
JDBC URL the bundled Keycloak uses to talk to its own Postgres. Resolves to the
in-cluster `srw-keycloakdb` Service when databases.keycloak.internal is true,
or to the operator-supplied externalUrl otherwise (e.g. managed Postgres).
*/}}
{{- define "srw.keycloakDbJdbcUrl" -}}
{{- if .Values.databases.keycloak.internal -}}
jdbc:postgresql://{{ include "srw.fullname" . }}-keycloakdb:5432/keycloak
{{- else -}}
{{- required "databases.keycloak.externalUrl is required when databases.keycloak.internal is false" .Values.databases.keycloak.externalUrl -}}
{{- end -}}
{{- end }}

{{/*
Resources for the Keycloak bootstrap Job.

  - srw.keycloakBootstrapServer  — URL kcadm authenticates against.
      Resolution: bootstrap.serverUrl override → externalInternalUrl
      (stripped of any /realms/... suffix) → externalIssuerUrl (same
      strip). Errors if none are set, since the Job has nothing to talk to.
  - srw.keycloakBootstrapRealm   — target realm. Defaults to keycloak.realm.
  - srw.keycloakBootstrapImage   — image with kcadm.sh. Defaults to
      keycloak.image so the Job re-uses what's already pulled.
*/}}
{{- define "srw.keycloakBootstrapServer" -}}
{{- $b := .Values.keycloak.bootstrap -}}
{{- if $b.serverUrl -}}
{{- $b.serverUrl -}}
{{- else if .Values.keycloak.externalInternalUrl -}}
{{- regexReplaceAll "/realms/.*$" .Values.keycloak.externalInternalUrl "" -}}
{{- else if .Values.keycloak.externalIssuerUrl -}}
{{- regexReplaceAll "/realms/.*$" .Values.keycloak.externalIssuerUrl "" -}}
{{- else -}}
{{- fail "keycloak.bootstrap.enabled requires keycloak.bootstrap.serverUrl, keycloak.externalInternalUrl, or keycloak.externalIssuerUrl" -}}
{{- end -}}
{{- end }}

{{- define "srw.keycloakBootstrapRealm" -}}
{{- default .Values.keycloak.realm .Values.keycloak.bootstrap.realm -}}
{{- end }}

{{- define "srw.keycloakBootstrapImage" -}}
{{- default .Values.keycloak.image .Values.keycloak.bootstrap.image -}}
{{- end }}

{{- define "srw.keycloakBootstrapName" -}}
{{- printf "%s-keycloak-bootstrap" (include "srw.fullname" .) -}}
{{- end }}

{{/*
NATS hub names + URL resolver. When nats.internal is true, the chart
deploys a StatefulSet + Service named <fullname>-nats and the orchestrator's
NATS_URL points at the in-cluster Service. When false, NATS_URL is the
user-supplied .Values.nats.url (empty disables VM lifecycle).

Setting both nats.internal=true and nats.url is a chart-render error
(enforced in helm/templates/nats/configmap.yaml at top).
*/}}
{{- define "srw.natsName" -}}
{{- printf "%s-nats" (include "srw.fullname" .) -}}
{{- end }}

{{- define "srw.natsUrl" -}}
{{- if .Values.nats.internal -}}
{{- printf "nats://%s.%s.svc.cluster.local:4222" (include "srw.natsName" .) .Release.Namespace -}}
{{- else -}}
{{- .Values.nats.url -}}
{{- end -}}
{{- end }}

{{/*
Per-orchestrator id used to scope vm.lifecycle.* NATS subjects. Defaults to
the chart fullname (srw, srw-prod, …) so single-cluster installs Just Work;
override .Values.orchestratorId when sharing a NATS hub across orchestrators
so each one binds disjoint vm.lifecycle.*.{id} subjects. Must match the paired
VM controller chart's orchestratorId.
*/}}
{{- define "srw.orchestratorId" -}}
{{- default (include "srw.fullname" .) .Values.orchestratorId -}}
{{- end }}

{{/*
Effective Vault path the bootstrap ExternalSecret reads from.
  - If keycloak.bootstrap.adminCredentialsVaultPath is set explicitly, use it.
  - Otherwise fall back to externalSecrets.vaultPath (the main bundle).
The fallback lets a single Vault entry serve both runtime and bootstrap, which
is the common case — KC_ADMIN_*, MCP_OIDC_CLIENT_SECRET, and CLOUD_SERVICE_*
live alongside the rest of the runtime config.
Returns "" when neither path is set.
*/}}
{{- define "srw.keycloakBootstrapVaultPath" -}}
{{- coalesce .Values.keycloak.bootstrap.adminCredentialsVaultPath .Values.externalSecrets.vaultPath -}}
{{- end }}

{{/*
Name of the K8s Secret holding the bootstrap admin credentials (KC_ADMIN_USER,
KC_ADMIN_PASSWORD, MCP_OIDC_CLIENT_SECRET, and optionally CLOUD_SERVICE_USER /
CLOUD_SERVICE_PASSWORD). Resolves to either:
  - the user-provided pre-existing Secret (.Values.keycloak.bootstrap.adminCredentialsSecret), or
  - the chart-managed Secret synced by the bootstrap ExternalSecret pre-install
    hook (when an effective Vault path resolves):
    `<fullname>-keycloak-bootstrap-creds`
*/}}
{{- define "srw.keycloakBootstrapAdminSecretName" -}}
{{- if .Values.keycloak.bootstrap.adminCredentialsSecret -}}
{{- .Values.keycloak.bootstrap.adminCredentialsSecret -}}
{{- else if include "srw.keycloakBootstrapVaultPath" . -}}
{{- printf "%s-keycloak-bootstrap-creds" (include "srw.fullname" .) -}}
{{- end -}}
{{- end }}

{{- define "srw.gitUrl" -}}
{{- if and .Values.gitea.enabled (not .Values.gitea.internal) .Values.gitea.externalUrl }}
{{- .Values.gitea.externalUrl }}
{{- else }}
{{- printf "%s://%s" (include "srw.urlScheme" .) (include "srw.host" (dict "context" . "key" "git" "default" "git")) }}
{{- end }}
{{- end }}

{{/*
Internal cluster URL for Gitea — used by orchestrator/agents for back-end API calls.
*/}}
{{- define "srw.giteaInternalUrl" -}}
{{- if .Values.gitea.internal }}
{{- printf "http://%s-gitea:3000" (include "srw.fullname" .) }}
{{- else if .Values.gitea.internalUrl }}
{{- .Values.gitea.internalUrl }}
{{- else }}
{{- include "srw.gitUrl" . }}
{{- end }}
{{- end }}

{{- define "srw.cloudUrl" -}}
{{- if and (not .Values.opencloud.enabled) (not .Values.nextcloud.enabled) .Values.cloud.externalUrl }}
{{- .Values.cloud.externalUrl }}
{{- else }}
{{- printf "%s://%s" (include "srw.urlScheme" .) (include "srw.host" (dict "context" . "key" "cloud" "default" "cloud")) }}
{{- end }}
{{- end }}

{{/*
Internal URL the agent uses for server-to-server calls to an external cloud.
Falls back to externalUrl if externalServiceUrl is not set.
*/}}
{{- define "srw.cloudServiceUrl" -}}
{{- if .Values.cloud.externalServiceUrl }}
{{- .Values.cloud.externalServiceUrl }}
{{- else }}
{{- .Values.cloud.externalUrl }}
{{- end }}
{{- end }}

{{- define "srw.mcpUrl" -}}
{{- printf "%s://%s" (include "srw.urlScheme" .) (include "srw.host" (dict "context" . "key" "mcp" "default" "mcp")) }}
{{- end }}

{{- define "srw.headscaleUrl" -}}
{{- if .Values.headscale.url }}
{{- .Values.headscale.url }}
{{- else }}
{{- printf "%s://%s" (include "srw.urlScheme" .) (include "srw.host" (dict "context" . "key" "headscale" "default" "headscale")) }}
{{- end }}
{{- end }}

{{- define "srw.neo4jBoltHost" -}}
{{- include "srw.host" (dict "context" . "key" "neo4jBolt" "default" "bolt-neo4j") }}
{{- end }}

{{/*
Hosts for the optional admin UIs and the cockpit deep-link to MinIO. These
have no URL helper because nothing else in the chart uses them as URLs —
they're consumed only as ingress hosts and as window.env.* deep-links.
*/}}
{{- define "srw.neo4jBrowserHost" -}}
{{- include "srw.host" (dict "context" . "key" "neo4j" "default" "neo4j") }}
{{- end }}

{{- define "srw.pgadminHost" -}}
{{- include "srw.host" (dict "context" . "key" "pgadmin" "default" "pgadmin") }}
{{- end }}

{{- define "srw.mongoHost" -}}
{{- include "srw.host" (dict "context" . "key" "mongo" "default" "mongo") }}
{{- end }}

{{- define "srw.dozzleHost" -}}
{{- include "srw.host" (dict "context" . "key" "dozzle" "default" "dozzle") }}
{{- end }}

{{- define "srw.minioHost" -}}
{{- include "srw.host" (dict "context" . "key" "minio" "default" "minio") }}
{{- end }}

{{/*
Database connection parts — host/port/db for postgres + vector + citation.

Credentials (user/password) live in the Secret only; the DSN is composed at
runtime by the application (orchestrator/utils/db_url.py and src/utils/
db_url.py). That avoids the redundancy + URL-encoding footgun of also
shipping a DATABASE_URL/VECTOR_DB_URL/CITATION_DB_URL Vault key.

For external mode, set databases.<which>.externalHost/externalPort/
externalDb in values; the chart still injects the host/port/db via
ConfigMap, and only the credentials come from the Secret.
*/}}
{{- define "srw.postgresHost" -}}
{{- if .Values.databases.postgres.internal -}}
{{- printf "%s-postgres" (include "srw.fullname" .) -}}
{{- else -}}
{{- required "databases.postgres.externalHost is required when internal=false" .Values.databases.postgres.externalHost -}}
{{- end -}}
{{- end }}

{{- define "srw.postgresPort" -}}
{{- if .Values.databases.postgres.internal -}}
5432
{{- else -}}
{{- .Values.databases.postgres.externalPort | default 5432 -}}
{{- end -}}
{{- end }}

{{- define "srw.postgresDb" -}}
{{- if .Values.databases.postgres.internal -}}
srw
{{- else -}}
{{- .Values.databases.postgres.externalDb | default "srw" -}}
{{- end -}}
{{- end }}

{{- define "srw.vectorPostgresHost" -}}
{{- if .Values.databases.vector.internal -}}
{{- printf "%s-pgvector" (include "srw.fullname" .) -}}
{{- else -}}
{{- required "databases.vector.externalHost is required when internal=false" .Values.databases.vector.externalHost -}}
{{- end -}}
{{- end }}

{{- define "srw.vectorPostgresPort" -}}
{{- if .Values.databases.vector.internal -}}
5432
{{- else -}}
{{- .Values.databases.vector.externalPort | default 5432 -}}
{{- end -}}
{{- end }}

{{- define "srw.vectorPostgresDb" -}}
{{- if .Values.databases.vector.internal -}}
srw_vector
{{- else -}}
{{- .Values.databases.vector.externalDb | default "srw_vector" -}}
{{- end -}}
{{- end }}

{{- define "srw.auditPostgresHost" -}}
{{- if .Values.databases.audit.internal -}}
{{- printf "%s-auditdb" (include "srw.fullname" .) -}}
{{- else -}}
{{- required "databases.audit.externalHost is required when internal=false" .Values.databases.audit.externalHost -}}
{{- end -}}
{{- end }}

{{- define "srw.auditPostgresPort" -}}
{{- if .Values.databases.audit.internal -}}
5432
{{- else -}}
{{- .Values.databases.audit.externalPort | default 5432 -}}
{{- end -}}
{{- end }}

{{- define "srw.auditPostgresDb" -}}
{{- if .Values.databases.audit.internal -}}
srw_audit
{{- else -}}
{{- .Values.databases.audit.externalDb | default "srw_audit" -}}
{{- end -}}
{{- end }}

{{/*
MongoDB URL — supports external mode (no auth in current setup, so URL is non-secret).
*/}}
{{- define "srw.mongodbUrl" -}}
{{- if .Values.databases.mongodb.internal }}
{{- printf "mongodb://%s-mongodb:27017/srw_logs" (include "srw.fullname" .) }}
{{- else }}
{{- required "databases.mongodb.externalUrl is required when databases.mongodb.internal=false" .Values.databases.mongodb.externalUrl }}
{{- end }}
{{- end }}

{{/*
Neo4j Bolt URL — internal cluster service or external URL.
*/}}
{{- define "srw.neo4jUrl" -}}
{{- if .Values.databases.neo4j.internal }}
{{- printf "bolt://%s-neo4j:7688" (include "srw.fullname" .) }}
{{- else }}
{{- required "databases.neo4j.externalUrl is required when databases.neo4j.internal=false" .Values.databases.neo4j.externalUrl }}
{{- end }}
{{- end }}
