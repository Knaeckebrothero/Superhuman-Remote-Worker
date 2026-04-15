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
Domain-derived URLs. All subdomains are derived from global.domain.
*/}}
{{- define "srw.cockpitUrl" -}}
{{- printf "https://%s" (required "global.domain is required" .Values.global.domain) }}
{{- end }}

{{- define "srw.apiUrl" -}}
{{- printf "https://api.%s" .Values.global.domain }}
{{- end }}

{{- define "srw.authUrl" -}}
{{- if and .Values.keycloak.enabled (not .Values.keycloak.internal) .Values.keycloak.externalIssuerUrl }}
{{- .Values.keycloak.externalIssuerUrl }}
{{- else }}
{{- printf "https://auth.%s" .Values.global.domain }}
{{- end }}
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

{{- define "srw.gitUrl" -}}
{{- if and .Values.gitea.enabled (not .Values.gitea.internal) .Values.gitea.externalUrl }}
{{- .Values.gitea.externalUrl }}
{{- else }}
{{- printf "https://git.%s" .Values.global.domain }}
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
{{- printf "https://cloud.%s" .Values.global.domain }}
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
{{- printf "https://mcp.%s" .Values.global.domain }}
{{- end }}

{{- define "srw.headscaleUrl" -}}
{{- printf "https://headscale.%s" .Values.global.domain }}
{{- end }}

{{- define "srw.neo4jBoltHost" -}}
{{- printf "bolt-neo4j.%s" .Values.global.domain }}
{{- end }}

{{/*
Database connection URLs — internal cluster service or external URL.
For postgres + vector, the URL is fully read from secrets when internal=true, since
credentials are part of the URL. The configmap values are only the *non-credential*
portion (host, port, dbname). For external mode, the full URL is provided in values.
*/}}

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
