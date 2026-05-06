{{/*
Chart name, truncated to 63 chars.
*/}}
{{- define "srwvm.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name: release-name + chart-name, truncated to 63 chars.
If release name contains chart name, don't repeat it.
*/}}
{{- define "srwvm.fullname" -}}
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
Chart label value.
*/}}
{{- define "srwvm.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to all resources.
*/}}
{{- define "srwvm.labels" -}}
helm.sh/chart: {{ include "srwvm.chart" . }}
{{ include "srwvm.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels (subset of common labels — used in matchLabels).
*/}}
{{- define "srwvm.selectorLabels" -}}
app.kubernetes.io/name: {{ include "srwvm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Component labels — extends srwvm.labels with app.kubernetes.io/component.
Usage: {{- include "srwvm.componentLabels" (dict "context" . "component" "vm-controller") | nindent 4 }}
*/}}
{{- define "srwvm.componentLabels" -}}
{{ include "srwvm.labels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Component selector labels — extends srwvm.selectorLabels with component.
*/}}
{{- define "srwvm.componentSelectorLabels" -}}
{{ include "srwvm.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Resource name helpers.
*/}}
{{- define "srwvm.vmControllerName" -}}
{{- printf "%s-vm-controller" (include "srwvm.fullname" .) -}}
{{- end }}

{{- define "srwvm.natsLeafName" -}}
{{- printf "%s-nats-leaf" (include "srwvm.fullname" .) -}}
{{- end }}

{{/*
Effective image tag — defaults to .Chart.AppVersion when unset.
Usage: {{ include "srwvm.imageTag" (dict "tag" .Values.image.vmController.tag "context" .) }}
*/}}
{{- define "srwvm.imageTag" -}}
{{- if .tag -}}
{{- .tag -}}
{{- else -}}
{{- .context.Chart.AppVersion -}}
{{- end -}}
{{- end }}

{{/*
Default VM image — defaults to ghcr.io/.../agent-vm-base:v<chart.AppVersion>
when .Values.vmController.defaultVmImage is empty.
*/}}
{{- define "srwvm.defaultVmImage" -}}
{{- if .Values.vmController.defaultVmImage -}}
{{- .Values.vmController.defaultVmImage -}}
{{- else -}}
{{- printf "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent-vm-base:v%s" .Chart.AppVersion -}}
{{- end -}}
{{- end }}

{{/*
Headscale API key Secret name. Returns the chart-managed Secret name when
either the dedicated Vault path is set OR the bundle path provides it
(externalSecrets.vaultPath with the HEADSCALE_API_KEY field). Falls back
to the user-provided pre-existing Secret name.
*/}}
{{- define "srwvm.headscaleApiKeySecretName" -}}
{{- if or .Values.headscale.apiKeyVaultPath (and .Values.externalSecrets.enabled .Values.externalSecrets.vaultPath) -}}
{{- printf "%s-headscale-api-key" (include "srwvm.fullname" .) -}}
{{- else -}}
{{- .Values.headscale.apiKeySecret -}}
{{- end -}}
{{- end }}

{{/*
SSH public key Secret name (used when the public key is sourced via Vault —
either the dedicated path or the bundle path). When ssh.publicKey is inline,
the deployment templates use the inline value directly; this name is unused.
*/}}
{{- define "srwvm.sshPubKeySecretName" -}}
{{- printf "%s-ssh-pubkey" (include "srwvm.fullname" .) -}}
{{- end }}

{{/*
Whether the SSH public key is provided via a chart-managed Secret (ESO sync
from either dedicated or bundle path). When false, ssh.publicKey is inline.
*/}}
{{- define "srwvm.sshPubKeyFromSecret" -}}
{{- if .Values.ssh.publicKey -}}
{{- /* inline */ -}}
{{- else if or .Values.ssh.publicKeyVaultPath (and .Values.externalSecrets.enabled .Values.externalSecrets.vaultPath) -}}
true
{{- end -}}
{{- end }}

{{/*
Tailscale auth key Secret name. Same dedicated-or-bundle resolution.
*/}}
{{- define "srwvm.tailscaleAuthKeySecretName" -}}
{{- if or .Values.tailscale.authKeyVaultPath (and .Values.externalSecrets.enabled .Values.externalSecrets.vaultPath) -}}
{{- printf "%s-tailscale-auth-key" (include "srwvm.fullname" .) -}}
{{- else -}}
{{- .Values.tailscale.authKeySecret -}}
{{- end -}}
{{- end }}
