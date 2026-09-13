{{/* One public capability map shared by admission and the VM controller. */}}
{{- define "srw.vmPreparationEnvironment" -}}
{{- $p := .Values.vmController.preparation -}}
{{- if $p.enabled -}}
{{- if not (include "srw.vmSameCluster" .) -}}
{{- fail "vmController.preparation requires vm.mode=same-cluster" -}}
{{- end -}}
{{- if not .Values.vmController.persistentRootdisk.enabled -}}
{{- fail "vmController.preparation requires persistentRootdisk.enabled" -}}
{{- end -}}
{{- if not (include "srw.vmLifecycleAuthSecretName" .) -}}
{{- fail "vmController.preparation requires lifecycle authentication" -}}
{{- end -}}
{{- if and $p.network.enabled (not $p.network.enforcementVerified) -}}
{{- fail "Online VM preparation requires network.enforcementVerified after testing the CNI policy" -}}
{{- end -}}
{{- end -}}
{{- $image := printf "%s:%s" $p.image.repository $p.image.tag -}}
{{- if $p.image.digest -}}{{- $image = printf "%s@%s" $p.image.repository $p.image.digest -}}{{- end -}}
{{- $pullSecrets := list -}}
{{- range .Values.global.imagePullSecrets -}}{{- $pullSecrets = append $pullSecrets .name -}}{{- end -}}
VM_PREPARATION_ENABLED: {{ $p.enabled | quote }}
VM_PREPARATION_IMAGE: {{ $image | quote }}
VM_PREPARATION_DISK_SIZE: {{ $p.diskSize | quote }}
VM_PREPARATION_MAX_CONCURRENT: {{ $p.maxConcurrent | quote }}
VM_PREPARATION_MAX_CACHE_ENTRIES: {{ $p.maxCacheEntries | quote }}
VM_PREPARATION_TIMEOUT: {{ $p.timeoutSeconds | quote }}
VM_PREPARATION_IMPORT_TIMEOUT: {{ $p.importTimeoutSeconds | quote }}
VM_PREPARATION_CACHE_TTL: {{ $p.cacheTtlSeconds | quote }}
VM_PREPARATION_REGISTRY_HOSTS: {{ $p.registryHosts | toJson | quote }}
VM_PREPARATION_INSECURE_REGISTRY_HOSTS: {{ $p.insecureRegistryHosts | toJson | quote }}
VM_PREPARATION_TOKEN_HOSTS: {{ $p.tokenHosts | toJson | quote }}
VM_PREPARATION_IMAGE_PULL_SECRETS: {{ $pullSecrets | toJson | quote }}
VM_PREPARATION_NETWORK_ENABLED: {{ $p.network.enabled | quote }}
VM_PREPARATION_NETWORK_ISOLATION_VERIFIED: {{ $p.network.enforcementVerified | quote }}
VM_PREPARATION_NETWORK_POLICY_REVISION: {{ $p.network | toJson | sha256sum | quote }}
{{- end -}}
