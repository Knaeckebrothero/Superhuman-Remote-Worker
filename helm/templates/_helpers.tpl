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
First-party image reference. A real digest pin takes precedence over the
display/update tag; an empty digest preserves the existing repository:tag
behavior.
Usage: {{ include "srw.imageRef" (dict "image" .Values.image.agent) }}
*/}}
{{- define "srw.imageRef" -}}
{{- $digest := default "" .image.digest -}}
{{- if $digest -}}
{{- printf "%s@%s" .image.repository $digest -}}
{{- else -}}
{{- printf "%s:%s" .image.repository .image.tag -}}
{{- end -}}
{{- end }}

{{/*
Bounded public deployment provenance consumed by the orchestrator and
dynamically provisioned agents. The image digest comes only from the same
value that srw.imageRef uses, so a tag can never be reported as an artifact
digest. MCP is omitted when its deployment is disabled.
*/}}
{{- define "srw.deploymentProvenanceJson" -}}
{{- $root := . -}}
{{- $components := dict -}}
{{- range $name := list "orchestrator" "agent" "cockpit" "workspace" -}}
  {{- $image := index $root.Values.image $name -}}
  {{- $declaration := index $root.Values.provenance.components $name -}}
  {{- $_ := set $components $name (dict
      "source_revision" (default "" $declaration.sourceRevision)
      "artifact_digest" (default "" $image.digest)
      "release_version" (default "" $declaration.releaseVersion)
    ) -}}
{{- end -}}
{{- if $root.Values.mcp.enabled -}}
  {{- $image := $root.Values.image.mcp -}}
  {{- $declaration := $root.Values.provenance.components.mcp -}}
  {{- $_ := set $components "mcp" (dict
      "source_revision" (default "" $declaration.sourceRevision)
      "artifact_digest" (default "" $image.digest)
      "release_version" (default "" $declaration.releaseVersion)
    ) -}}
{{- end -}}
{{- dict
    "source_url" (default "" $root.Values.provenance.sourceUrl)
    "documentation_url" (default "" $root.Values.provenance.documentationUrl)
    "components" $components
  | toJson -}}
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
Replicate the aliased official subchart's fullname helper so the orchestrator
can fetch discovery over the exact generated ClusterIP Service name.
*/}}
{{- define "srw.collaboraServiceName" -}}
{{- if .Values.collabora.fullnameOverride -}}
{{- .Values.collabora.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default "collabora" .Values.collabora.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "srw.collaboraInternalUrl" -}}
{{- printf "http://%s:9980" (include "srw.collaboraServiceName" .) -}}
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
Connection parts the bundled Gitea uses for its metadata database when
gitea.database.type is "postgres". Resolves to the in-cluster `srw-giteadb`
Service when databases.gitea.internal is true, or to the operator-supplied
external server otherwise. Credentials never appear here — Gitea reads
GITEA__database__USER / __PASSWD from the Secret.
*/}}
{{- define "srw.giteaDbHost" -}}
{{- if .Values.databases.gitea.internal -}}
{{- printf "%s-giteadb" (include "srw.fullname" .) -}}
{{- else -}}
{{- required "databases.gitea.externalHost is required when databases.gitea.internal is false" .Values.databases.gitea.externalHost -}}
{{- end -}}
{{- end }}

{{- define "srw.giteaDbPort" -}}
{{- if .Values.databases.gitea.internal -}}
5432
{{- else -}}
{{- .Values.databases.gitea.externalPort | default 5432 -}}
{{- end -}}
{{- end }}

{{- define "srw.giteaDbName" -}}
{{- if .Values.databases.gitea.internal -}}
gitea
{{- else -}}
{{- .Values.databases.gitea.externalDb | default "gitea" -}}
{{- end -}}
{{- end }}

{{/*
True when the bundled Gitea should use Postgres for its metadata DB. Any value
other than "sqlite3" is rejected loudly rather than silently falling back —
a typo here would otherwise point a populated Gitea at a fresh database.
*/}}
{{- define "srw.giteaUsesPostgres" -}}
{{- $type := .Values.gitea.database.type | default "postgres" -}}
{{- if eq $type "postgres" -}}
true
{{- else if eq $type "sqlite3" -}}
{{- else -}}
{{- fail (printf "gitea.database.type must be \"postgres\" or \"sqlite3\", got %q" $type) -}}
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

{{- define "srw.dozzleHost" -}}
{{- include "srw.host" (dict "context" . "key" "dozzle" "default" "dozzle") }}
{{- end }}

{{- define "srw.minioHost" -}}
{{- include "srw.host" (dict "context" . "key" "minio" "default" "minio") }}
{{- end }}

{{/*
Database connection parts — host/port/db for postgres + vector.

Credentials (user/password) live in the Secret only; the DSN is composed at
runtime by the application (orchestrator/utils/db_url.py and src/utils/
db_url.py). That avoids the redundancy + URL-encoding footgun of also
shipping a DATABASE_URL/VECTOR_DB_URL Vault key.

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
Neo4j Bolt URL — internal cluster service or external URL.
*/}}
{{- define "srw.neo4jUrl" -}}
{{- if .Values.databases.neo4j.internal }}
{{- printf "bolt://%s-neo4j:7688" (include "srw.fullname" .) }}
{{- else }}
{{- required "databases.neo4j.externalUrl is required when databases.neo4j.internal=false" .Values.databases.neo4j.externalUrl }}
{{- end }}
{{- end }}

{{/*
Whether the bundled single-node object store runs.

Tri-state on purpose. `true` and `false` are explicit operator decisions and
always win. The default is null = AUTO: bring your own store by setting
`s3.endpoint`, or say nothing and get the bundled one.

Auto exists because every consumer of the object store fails SILENTLY without
it — canvas durability keeps no copy, workspace snapshots never capture, the
virtual tier stays unwired. "Operator said nothing" must therefore resolve to a
working store rather than to a deployment that looks healthy and quietly
forgets things.

Keyed on `s3.endpoint` alone, not on the virtual-tier endpoint: it is the
primary "do you already have an object store" signal, and an operator who has
one can point both consumers at it. This also keeps the rule one comparison
long, so a reader can predict the outcome without tracing the tier config.
*/}}
{{- define "srw.garageEnabled" -}}
{{- if kindIs "invalid" .Values.garage.enabled -}}
{{- if not (.Values.s3).endpoint -}}true{{- end -}}
{{- else if .Values.garage.enabled -}}
true
{{- end -}}
{{- end -}}

{{/*
Effective S3 endpoint for snapshots. External (s3.endpoint) wins; otherwise the
bundled Garage service when enabled; otherwise empty (snapshots disabled).
*/}}
{{- define "srw.effectiveS3Endpoint" -}}
{{- if .Values.s3.endpoint -}}
{{- .Values.s3.endpoint -}}
{{- else if (include "srw.garageEnabled" .) -}}
{{- printf "http://%s:3900" (include "srw.serviceName" (dict "context" . "component" "garage")) -}}
{{- end -}}
{{- end -}}

{{/*
Effective virtual-workspace S3 endpoint. External wins; else bundled Garage.
*/}}
{{- define "srw.effectiveVwEndpoint" -}}
{{- if .Values.virtualWorkspace.s3.endpoint -}}
{{- .Values.virtualWorkspace.s3.endpoint -}}
{{- else if (include "srw.garageEnabled" .) -}}
{{- printf "http://%s:3900" (include "srw.serviceName" (dict "context" . "component" "garage")) -}}
{{- end -}}
{{- end -}}

{{/*
Effective rclone backend type for the virtual tier. Explicit value wins; else
"s3" when Garage is bundled; else "" (tier disabled).
*/}}
{{- define "srw.effectiveVwRcloneType" -}}
{{- if .Values.virtualWorkspace.rclone.type -}}
{{- .Values.virtualWorkspace.rclone.type -}}
{{- else if (include "srw.garageEnabled" .) -}}
{{- "s3" -}}
{{- end -}}
{{- end -}}

{{/*
Effective rclone root (bucket) for the virtual tier. Explicit value wins; else
the bundled Garage workspace bucket.
*/}}
{{- define "srw.effectiveVwRcloneRoot" -}}
{{- if .Values.virtualWorkspace.rclone.root -}}
{{- .Values.virtualWorkspace.rclone.root -}}
{{- else if (include "srw.garageEnabled" .) -}}
{{- .Values.garage.buckets.workspaces -}}
{{- end -}}
{{- end -}}

{{/*
Effective rclone S3 provider profile. When auto-wiring to bundled Garage (no
external vw endpoint), use "Other" (Garage's rclone-compatible profile,
path-style). Otherwise the configured provider (default "Minio").
*/}}
{{- define "srw.effectiveVwProvider" -}}
{{- if and (include "srw.garageEnabled" .) (not .Values.virtualWorkspace.s3.endpoint) -}}
{{- "Other" -}}
{{- else -}}
{{- .Values.virtualWorkspace.s3.provider | default "Minio" -}}
{{- end -}}
{{- end -}}

{{/*
Effective Secret name for the Canvas gateway's restricted PostgreSQL login.
Chart-created and Vault/ESO sources use the stable chart-owned name; an
operator-precreated Secret keeps its explicit name.
*/}}
{{- define "srw.canvasGatewayDatabaseSecretName" -}}
{{- $credentials := .Values.canvas.livePreview.viewer.database.credentials -}}
{{- if or $credentials.create (ne (trim $credentials.vaultPath) "") -}}
{{- printf "%s-canvas-gateway-db" (include "srw.fullname" .) -}}
{{- else -}}
{{- $credentials.existingSecret -}}
{{- end -}}
{{- end -}}

{{/*
Keycloak SMTP port and STARTTLS flag for the realm bootstrap (kcadm).

Both are WHITELISTS, not sanitizers: each emits either a literal drawn from a
closed set, or the default. Nothing else can reach the rendered output, for
any value of email.smtp.port / email.smtp.useTls -- nil, bool, int64,
float64, string, map or slice, which is every Go kind Helm's loaders can
produce (--set, --set-string, --set-json, a values file, --set key=null).

Why a whitelist: four blacklist remedies shipped here and each one closed the
case in front of it while opening another. `default` swallowed a real boolean
false; `toString | default` then forwarded the literal "<nil>"; `kindIs
"invalid"` let an empty string through verbatim; adding `eq (toString .) ""`
let a whitespace-only string through. A blacklist is only ever as complete as
the list of shapes someone thought to try.

Why this is total, argued from structure rather than from a list of cases:
  - `toString` is total: Sprig falls through to fmt's "%v" for every type,
    nil included, so the guard always compares a string, never a bare Go
    value that could error or format surprisingly.
  - Membership is exact equality against a literal set (starttls), or a fully
    anchored ASCII-digit match (port). Go's ^ and $ are start/end of TEXT
    without (?m), so an embedded newline cannot slip past the anchors.
  - The else-branch is the DEFAULT, not the input: an unrecognised value is
    dropped, never forwarded.
The output alphabet is therefore closed -- "true"/"false" for starttls, a
decimal literal or "1025" for port -- under every input, including shapes
nobody has tried yet. `trim` and `lower` only widen which inputs map to an
intended value; they cannot widen that output.

Why the fallback direction matters: Keycloak parses starttls with Java's
Boolean.parseBoolean, which returns false for anything not literally "true"
and does no trimming. Forwarding a meaningless value therefore DISABLES
STARTTLS silently and puts the SMTP password on the wire in cleartext. Only
true/false -- any case, surrounding whitespace tolerated -- count as the
operator asking; every other shape means "did not ask", and gets the default.

The closed alphabet also settles a second exposure FOR THESE TWO FIELDS: the
text is rendered inside a double-quoted shell word in the Keycloak postStart
hook, so before the whitelist a value containing a double quote was command
injection, and one containing a newline broke the rendered manifest outright.

Scope that claim to port and starttls only -- it says nothing about the hook.
The same `$KC update` shell word also interpolates .Values.keycloak.realm and
.Values.email.smtp.from raw, and those are still injectable today (verified:
`--set-string 'email.smtp.from=a"; id; echo "'` renders a closing quote and a
second command). Hardening them is a separate pre-existing item; these two
helpers are the template for how, not evidence that it has been done.

Two deliberate boundaries:
  - `--set-string email.smtp.useTls=null` now yields the default instead of
    the literal it used to forward. Helm's --set-string contract is untouched
    (the value in .Values is still that 4-character string); the guard simply
    does not recognise it as a boolean -- and forwarding it meant
    parseBoolean == false, i.e. STARTTLS off without being asked.
  - The port guard validates FORM, not policy. Any decimal literal passes,
    including 0 (a real, explicitly-set value it must not swallow) and values
    above 65535. Range is Keycloak's to reject, loudly, with the operator's
    own number in the error; quietly substituting the dev mail-catcher port
    would be the very bug this task exists to fix.
*/}}
{{- define "srw.keycloak.smtpStartTls" -}}
{{- $canonical := lower (trim (toString .Values.email.smtp.useTls)) -}}
{{- if has $canonical (list "true" "false") -}}{{ $canonical }}{{- else -}}true{{- end -}}
{{- end -}}

{{- define "srw.keycloak.smtpPort" -}}
{{- $canonical := trim (toString .Values.email.smtp.port) -}}
{{- if regexMatch "^[0-9]+$" $canonical -}}{{ $canonical }}{{- else -}}1025{{- end -}}
{{- end -}}

{{/*
Resolved instance count for one database. Explicit `instances` wins; otherwise
the profile decides. Everything downstream (anti-affinity, PDBs) reads THIS,
never the profile name — a generated values file may set instances directly.
Usage: {{ include "srw.dbInstances" (dict "context" . "db" .Values.databases.postgres) }}
*/}}
{{- define "srw.dbInstances" -}}
{{- if .db.instances -}}
{{- .db.instances -}}
{{- else if eq .context.Values.databases.profile "ha" -}}
2
{{- else -}}
1
{{- end -}}
{{- end }}

{{/*
Which implementation backs a database. Rejects typos loudly rather than
silently falling back to the StatefulSet, which would strand a migrated
Cluster's data behind an empty one.
Usage: {{ include "srw.dbEngine" (dict "context" . "db" .Values.databases.postgres) }}
*/}}
{{- define "srw.dbEngine" -}}
{{- $engine := .db.engine | default "statefulset" -}}
{{- if not (has $engine (list "statefulset" "migrating" "cnpg")) -}}
{{- fail (printf "databases.<name>.engine must be statefulset, migrating or cnpg, got %q" $engine) -}}
{{- end -}}
{{- $engine -}}
{{- end }}

{{/*
Whether a bundled database is deployed by this chart at all, independent of
which engine backs it. Gitea's and Keycloak's databases carry extra
prerequisites (their own service must be bundled too), and those conditions
are needed by four templates -- the StatefulSet, the Cluster, the credential
Secret and the PDB. Duplicating them is how they drift.
Usage: {{ include "srw.dbPrereqs" (dict "context" . "key" "postgres") }}
*/}}
{{- define "srw.dbPrereqs" -}}
{{- $ctx := .context -}}
{{- $db := index $ctx.Values.databases .key -}}
{{- $ok := and $db.enabled $db.internal -}}
{{- if eq .key "gitea" -}}
{{- $ok = and $ok $ctx.Values.gitea.enabled $ctx.Values.gitea.internal (include "srw.giteaUsesPostgres" $ctx) -}}
{{- else if eq .key "keycloak" -}}
{{- $ok = and $ok $ctx.Values.keycloak.enabled $ctx.Values.keycloak.internal -}}
{{- end -}}
{{- if $ok -}}true{{- end -}}
{{- end }}

{{/*
Whether a database renders its CloudNativePG Cluster (engine cnpg or, during
the transition, migrating). Empty string is falsey, so use it in an `if`.
Usage: {{ if (include "srw.dbRendersCnpg" (dict "context" . "key" "postgres")) }}
*/}}
{{- define "srw.dbRendersCnpg" -}}
{{- if include "srw.dbPrereqs" . -}}
{{- $engine := include "srw.dbEngine" (dict "context" .context "db" (index .context.Values.databases .key)) -}}
{{- if has $engine (list "migrating" "cnpg") -}}true{{- end -}}
{{- end -}}
{{- end }}

{{/*
Whether a database renders its bundled StatefulSet. `migrating` renders BOTH,
so Phase 4 can import from the live Service before the StatefulSet goes away.
Usage: {{ if (include "srw.dbRendersStatefulset" (dict "context" . "key" "postgres")) }}
*/}}
{{- define "srw.dbRendersStatefulset" -}}
{{- if include "srw.dbPrereqs" . -}}
{{- $engine := include "srw.dbEngine" (dict "context" .context "db" (index .context.Values.databases .key)) -}}
{{- if has $engine (list "statefulset" "migrating") -}}true{{- end -}}
{{- end -}}
{{- end }}
