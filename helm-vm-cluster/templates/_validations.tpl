{{/*
Render-time validations. Called from license-gate.yaml so they fire
before any resource is rendered.
*/}}
{{- define "srwvm.validate" -}}
{{- $metering := .Values.infrastructureMetering | default dict -}}
{{- $meteringNetworkPolicy := $metering.networkPolicy | default dict -}}
{{- $vmInventory := $metering.vmInventoryEnabled | default false -}}
{{- $pvcInventory := $metering.pvcInventoryEnabled | default false -}}
{{- $pvInventory := $metering.pvInventoryEnabled | default false -}}
{{- $storageInventory := or $pvcInventory $pvInventory -}}
{{- $remoteInventory := or $vmInventory $pvcInventory $pvInventory -}}
{{- $meteringExternalSecrets := $metering.externalSecrets | default dict -}}
{{- $orchestratorHostAliases := list -}}
{{- if hasKey $metering "orchestratorHostAliases" -}}
{{- $orchestratorHostAliases = get $metering "orchestratorHostAliases" -}}
{{- end -}}

{{- if ($meteringExternalSecrets.enabled | default false) -}}
{{- if not .Values.externalSecrets.enabled -}}
{{- fail "infrastructureMetering.externalSecrets.enabled requires externalSecrets.enabled" -}}
{{- end -}}
{{- if not ($meteringExternalSecrets.vaultPath | default "") -}}
{{- fail "infrastructureMetering.externalSecrets.vaultPath is required when metering secret sync is enabled" -}}
{{- end -}}
{{- $meteringSecretTargets := list
      ($metering.ingestionSecretName | default "")
      ($metering.storageIngestionSecretName | default "")
      ($metering.volumeIdentitySecretName | default "")
      (.Values.vmController.lifecycleAuthSecretName | default "")
-}}
{{- $seenMeteringSecretTargets := dict -}}
{{- range $target := $meteringSecretTargets -}}
{{- if $target -}}
{{- if hasKey $seenMeteringSecretTargets $target -}}
{{- fail (printf "infrastructureMetering external secret targets must be distinct; %s is reused" $target) -}}
{{- end -}}
{{- $_ := set $seenMeteringSecretTargets $target true -}}
{{- end -}}
{{- end -}}
{{- if eq (len $seenMeteringSecretTargets) 0 -}}
{{- fail "infrastructureMetering.externalSecrets.enabled requires at least one metering Secret name" -}}
{{- end -}}
{{- $meteringVaultProperties := list
      ($meteringExternalSecrets.vmiIngestionProperty | default "")
      ($meteringExternalSecrets.vmStorageIngestionProperty | default "")
      ($meteringExternalSecrets.volumeIdentityProperty | default "")
      ($meteringExternalSecrets.vmLifecycleAuthProperty | default "")
-}}
{{- $seenMeteringVaultProperties := dict -}}
{{- range $property := $meteringVaultProperties -}}
{{- if not (regexMatch "^[-._A-Za-z0-9]+$" $property) -}}
{{- fail "infrastructureMetering external secret property names must use only letters, digits, '.', '_', or '-'" -}}
{{- end -}}
{{- if hasKey $seenMeteringVaultProperties $property -}}
{{- fail (printf "infrastructureMetering external secret properties must be distinct; %s is reused" $property) -}}
{{- end -}}
{{- $_ := set $seenMeteringVaultProperties $property true -}}
{{- end -}}
{{- end -}}

{{- /* Static collector-only DNS overrides are security-sensitive. Validate
      them even while collectors are dark so a later gate cannot activate a
      malformed or unexpectedly broad /etc/hosts entry. */ -}}
{{- if not (kindIs "slice" $orchestratorHostAliases) -}}
{{- fail "infrastructureMetering.orchestratorHostAliases must be a list" -}}
{{- end -}}
{{- if gt (len $orchestratorHostAliases) 16 -}}
{{- fail "infrastructureMetering.orchestratorHostAliases supports at most 16 entries" -}}
{{- end -}}
{{- range $aliasIndex, $alias := $orchestratorHostAliases -}}
{{- if not (kindIs "map" $alias) -}}
{{- fail (printf "infrastructureMetering.orchestratorHostAliases[%d] must be a map" $aliasIndex) -}}
{{- end -}}
{{- if or (not (hasKey $alias "ip")) (not (hasKey $alias "hostnames")) (ne (len $alias) 2) -}}
{{- fail (printf "infrastructureMetering.orchestratorHostAliases[%d] must contain only ip and hostnames" $aliasIndex) -}}
{{- end -}}
{{- $aliasIP := get $alias "ip" -}}
{{- if not (kindIs "string" $aliasIP) -}}
{{- fail (printf "infrastructureMetering.orchestratorHostAliases[%d].ip must be a canonical IPv4 string" $aliasIndex) -}}
{{- end -}}
{{- if not (regexMatch "^(0|[1-9][0-9]{0,2})([.](0|[1-9][0-9]{0,2})){3}$" $aliasIP) -}}
{{- fail (printf "infrastructureMetering.orchestratorHostAliases[%d].ip must be a canonical IPv4 address" $aliasIndex) -}}
{{- end -}}
{{- range $octet := splitList "." $aliasIP -}}
{{- if gt (int $octet) 255 -}}
{{- fail (printf "infrastructureMetering.orchestratorHostAliases[%d].ip must be a canonical IPv4 address" $aliasIndex) -}}
{{- end -}}
{{- end -}}
{{- $aliasHostnames := get $alias "hostnames" -}}
{{- if not (kindIs "slice" $aliasHostnames) -}}
{{- fail (printf "infrastructureMetering.orchestratorHostAliases[%d].hostnames must be a non-empty list" $aliasIndex) -}}
{{- end -}}
{{- if or (eq (len $aliasHostnames) 0) (gt (len $aliasHostnames) 16) -}}
{{- fail (printf "infrastructureMetering.orchestratorHostAliases[%d].hostnames must contain 1 to 16 entries" $aliasIndex) -}}
{{- end -}}
{{- $seenAliasHostnames := dict -}}
{{- range $hostnameIndex, $hostname := $aliasHostnames -}}
{{- if not (kindIs "string" $hostname) -}}
{{- fail (printf "infrastructureMetering.orchestratorHostAliases[%d].hostnames[%d] must be a lowercase DNS hostname" $aliasIndex $hostnameIndex) -}}
{{- end -}}
{{- if or (gt (len $hostname) 253) (not (regexMatch "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?([.][a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$" $hostname)) -}}
{{- fail (printf "infrastructureMetering.orchestratorHostAliases[%d].hostnames[%d] must be a lowercase DNS hostname" $aliasIndex $hostnameIndex) -}}
{{- end -}}
{{- if hasKey $seenAliasHostnames $hostname -}}
{{- fail (printf "infrastructureMetering.orchestratorHostAliases[%d].hostnames must not contain duplicates" $aliasIndex) -}}
{{- end -}}
{{- $_ := set $seenAliasHostnames $hostname true -}}
{{- end -}}
{{- end -}}

{{- /* nats.hubUrl is required — the controller can't function without a hub */ -}}
{{- if not .Values.nats.hubUrl -}}
{{- fail "nats.hubUrl is required — the cross-cluster vm-controller dials this URL to reach the orchestrator's NATS hub. Example: nats://10.0.50.101:30743 (the NodePort exposed by the main chart's nats.leafNodePort)." -}}
{{- end -}}

{{- $bundle := and .Values.externalSecrets.enabled .Values.externalSecrets.vaultPath -}}
{{- $lifecycleSecretName := .Values.vmController.lifecycleAuthSecretName | default "" -}}
{{- if $lifecycleSecretName -}}
{{- if ne (int .Values.vmController.replicas) 1 -}}
{{- fail "vmController lifecycle authentication currently requires replicas=1 so per-entity create/delete fencing cannot cross controller replicas" -}}
{{- end -}}
{{- if or (gt (len $lifecycleSecretName) 253) (not (regexMatch "^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$" $lifecycleSecretName)) -}}
{{- fail "vmController.lifecycleAuthSecretName must be a valid Kubernetes Secret name" -}}
{{- end -}}
{{- if eq $lifecycleSecretName (include "srwvm.fullname" .) -}}
{{- fail "vmController.lifecycleAuthSecretName must be a dedicated Secret, not the controller bundle" -}}
{{- end -}}
{{- if and $metering.ingestionSecretName (eq $lifecycleSecretName $metering.ingestionSecretName) -}}
{{- fail "vmController.lifecycleAuthSecretName must be separate from infrastructureMetering.ingestionSecretName" -}}
{{- end -}}
{{- if and $metering.storageIngestionSecretName (eq $lifecycleSecretName $metering.storageIngestionSecretName) -}}
{{- fail "vmController.lifecycleAuthSecretName must be separate from infrastructureMetering.storageIngestionSecretName" -}}
{{- end -}}
{{- if and $metering.volumeIdentitySecretName (eq $lifecycleSecretName $metering.volumeIdentitySecretName) -}}
{{- fail "vmController.lifecycleAuthSecretName must be separate from infrastructureMetering.volumeIdentitySecretName" -}}
{{- end -}}
{{- end -}}

{{- /* Headscale: pick exactly one credential source (or rely on the bundle) */ -}}
{{- if and .Values.headscale.apiKeySecret .Values.headscale.apiKeyVaultPath -}}
{{- fail "headscale.apiKeySecret and headscale.apiKeyVaultPath are mutually exclusive — set exactly one (or neither, to pull HEADSCALE_API_KEY from externalSecrets.vaultPath)." -}}
{{- end -}}
{{- if and (not .Values.headscale.apiKeySecret) (not .Values.headscale.apiKeyVaultPath) (not $bundle) -}}
{{- fail "Provide a headscale API key source: headscale.apiKeySecret (pre-existing K8s Secret), headscale.apiKeyVaultPath (dedicated Vault path), or set externalSecrets.vaultPath to a bundle holding HEADSCALE_API_KEY. The vm-controller calls headscale to register provisioned VMs." -}}
{{- end -}}
{{- if and .Values.headscale.apiKeyVaultPath (not .Values.externalSecrets.enabled) -}}
{{- fail "headscale.apiKeyVaultPath requires externalSecrets.enabled=true (the chart renders an ExternalSecret to sync the API key from Vault). Either flip externalSecrets.enabled, or switch to headscale.apiKeySecret (a pre-created K8s Secret)." -}}
{{- end -}}

{{- /* SSH public key: pick exactly one source (inline / dedicated / bundle) */ -}}
{{- if and .Values.ssh.publicKey .Values.ssh.publicKeyVaultPath -}}
{{- fail "ssh.publicKey and ssh.publicKeyVaultPath are mutually exclusive — set exactly one (or neither, to pull VM_SSH_PUBLIC_KEY from externalSecrets.vaultPath)." -}}
{{- end -}}
{{- if and (not .Values.ssh.publicKey) (not .Values.ssh.publicKeyVaultPath) (not $bundle) -}}
{{- fail "Provide an SSH public-key source: ssh.publicKey (inline ssh-ed25519/ssh-rsa string), ssh.publicKeyVaultPath (dedicated Vault path with key ssh-publickey), or set externalSecrets.vaultPath to a bundle holding VM_SSH_PUBLIC_KEY. This key is authorized into the VMs' authorized_keys at boot — must match the private key the orchestrator (chart 1) holds." -}}
{{- end -}}
{{- if and .Values.ssh.publicKeyVaultPath (not .Values.externalSecrets.enabled) -}}
{{- fail "ssh.publicKeyVaultPath requires externalSecrets.enabled=true. Either flip externalSecrets.enabled, or switch to ssh.publicKey (inline)." -}}
{{- end -}}

{{- /* Tailscale: pick exactly one credential source when enabled */ -}}
{{- if .Values.tailscale.enabled -}}
{{- if and .Values.tailscale.authKeySecret .Values.tailscale.authKeyVaultPath -}}
{{- fail "tailscale.authKeySecret and tailscale.authKeyVaultPath are mutually exclusive — set exactly one (or neither, to pull TAILSCALE_AUTH_KEY from externalSecrets.vaultPath) when tailscale.enabled=true." -}}
{{- end -}}
{{- if and (not .Values.tailscale.authKeySecret) (not .Values.tailscale.authKeyVaultPath) (not $bundle) -}}
{{- fail "tailscale.enabled=true requires a credential source: tailscale.authKeySecret (pre-existing K8s Secret), tailscale.authKeyVaultPath (dedicated Vault path), or externalSecrets.vaultPath holding TAILSCALE_AUTH_KEY. The auth key is injected into VMs at boot so they join the headscale tailnet." -}}
{{- end -}}
{{- if and .Values.tailscale.authKeyVaultPath (not .Values.externalSecrets.enabled) -}}
{{- fail "tailscale.authKeyVaultPath requires externalSecrets.enabled=true. Either flip externalSecrets.enabled, or switch to tailscale.authKeySecret." -}}
{{- end -}}
{{- end -}}

{{- /* Remote metering collectors: fail closed on incomplete identity/scope. */ -}}
{{- if and ($metering.vmShadowEnabled | default false) (not $vmInventory) -}}
{{- fail "infrastructureMetering.vmShadowEnabled requires vmInventoryEnabled=true" -}}
{{- end -}}
{{- if and ($metering.pvcShadowEnabled | default false) (not $pvcInventory) -}}
{{- fail "infrastructureMetering.pvcShadowEnabled requires pvcInventoryEnabled=true" -}}
{{- end -}}
{{- if and ($metering.pvShadowEnabled | default false) (not $pvInventory) -}}
{{- fail "infrastructureMetering.pvShadowEnabled requires pvInventoryEnabled=true" -}}
{{- end -}}
{{- if $remoteInventory -}}
{{- $orchestratorImage := .Values.image.orchestrator | default dict -}}
{{- if not $orchestratorImage.repository -}}
{{- fail "remote infrastructure metering requires image.orchestrator.repository" -}}
{{- end -}}
{{- if and (not $orchestratorImage.tag) (not $orchestratorImage.digest) -}}
{{- fail "remote infrastructure metering requires an explicit image.orchestrator.tag or image.orchestrator.digest" -}}
{{- end -}}
{{- if and $orchestratorImage.tag (not (regexMatch "^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$" $orchestratorImage.tag)) -}}
{{- fail "image.orchestrator.tag is not a valid OCI image tag" -}}
{{- end -}}
{{- if and $orchestratorImage.digest (not (regexMatch "^sha256:[0-9a-f]{64}$" $orchestratorImage.digest)) -}}
{{- fail "image.orchestrator.digest must be a sha256 OCI digest" -}}
{{- end -}}
{{- if not $metering.vmStableClusterId -}}
{{- fail "remote infrastructure metering requires vmStableClusterId" -}}
{{- end -}}
{{- if not (regexMatch "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$" $metering.vmStableClusterId) -}}
{{- fail "infrastructureMetering.vmStableClusterId must be a stable bounded cluster identifier" -}}
{{- end -}}
{{- if not $metering.orchestratorUrl -}}
{{- fail "remote infrastructure metering requires orchestratorUrl" -}}
{{- end -}}
{{- if not (regexMatch "^https://.+" $metering.orchestratorUrl) -}}
{{- fail "infrastructureMetering.orchestratorUrl must use https" -}}
{{- end -}}
{{- $vmNamespace := $metering.vmNamespace | default .Release.Namespace -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$" $vmNamespace) -}}
{{- fail "infrastructureMetering.vmNamespace must be a valid Kubernetes namespace" -}}
{{- end -}}
{{- if and (not ($meteringNetworkPolicy.enabled | default false)) (not ($meteringNetworkPolicy.allowUnrestrictedEgress | default false)) -}}
{{- fail "remote infrastructure metering requires networkPolicy.enabled or explicit networkPolicy.allowUnrestrictedEgress=true" -}}
{{- end -}}
{{- if ($meteringNetworkPolicy.enabled | default false) -}}
{{- if not $meteringNetworkPolicy.apiServerCidrs -}}
{{- fail "infrastructureMetering.networkPolicy.enabled requires apiServerCidrs" -}}
{{- end -}}
{{- if not $meteringNetworkPolicy.orchestratorCidrs -}}
{{- fail "infrastructureMetering.networkPolicy.enabled requires orchestratorCidrs" -}}
{{- end -}}
{{- if not $meteringNetworkPolicy.orchestratorPorts -}}
{{- fail "infrastructureMetering.networkPolicy.enabled requires orchestratorPorts" -}}
{{- end -}}
{{- end -}}
{{- if lt (int $metering.staleAfterSeconds) (int $metering.relistIntervalSeconds) -}}
{{- fail "infrastructureMetering.staleAfterSeconds must be greater than or equal to relistIntervalSeconds" -}}
{{- end -}}
{{- end -}}

{{- if $vmInventory -}}
{{- if not $metering.ingestionSecretName -}}
{{- fail "infrastructureMetering.vmInventoryEnabled requires a distinct pre-existing ingestionSecretName" -}}
{{- end -}}
{{- if not $metering.ingestionSecretKey -}}
{{- fail "infrastructureMetering.vmInventoryEnabled requires ingestionSecretKey" -}}
{{- end -}}
{{- if or (gt (len $metering.ingestionSecretName) 253) (not (regexMatch "^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$" $metering.ingestionSecretName)) -}}
{{- fail "infrastructureMetering.ingestionSecretName must be a valid Kubernetes Secret name" -}}
{{- end -}}
{{- if not (regexMatch "^[-._A-Za-z0-9]+$" $metering.ingestionSecretKey) -}}
{{- fail "infrastructureMetering.ingestionSecretKey must be a valid Secret data key" -}}
{{- end -}}
{{- end -}}

{{- if $storageInventory -}}
{{- if not $metering.storageIngestionSecretName -}}
{{- fail "remote PVC/PV inventory requires a distinct pre-existing storageIngestionSecretName" -}}
{{- end -}}
{{- if not $metering.storageIngestionSecretKey -}}
{{- fail "remote PVC/PV inventory requires storageIngestionSecretKey" -}}
{{- end -}}
{{- if or (gt (len $metering.storageIngestionSecretName) 253) (not (regexMatch "^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$" $metering.storageIngestionSecretName)) -}}
{{- fail "infrastructureMetering.storageIngestionSecretName must be a valid Kubernetes Secret name" -}}
{{- end -}}
{{- if not (regexMatch "^[-._A-Za-z0-9]+$" $metering.storageIngestionSecretKey) -}}
{{- fail "infrastructureMetering.storageIngestionSecretKey must be a valid Secret data key" -}}
{{- end -}}
{{- if and $metering.ingestionSecretName (eq $metering.storageIngestionSecretName $metering.ingestionSecretName) -}}
{{- fail "infrastructureMetering.storageIngestionSecretName must be separate from ingestionSecretName" -}}
{{- end -}}
{{- end -}}

{{- if $pvInventory -}}
{{- if not ($metering.pvClusterWideRbacAcknowledged | default false) -}}
{{- fail "infrastructureMetering.pvInventoryEnabled requires pvClusterWideRbacAcknowledged=true because Kubernetes PV reads are cluster-wide" -}}
{{- end -}}
{{- if not $metering.volumeIdentitySecretName -}}
{{- fail "infrastructureMetering.pvInventoryEnabled requires a pre-existing volumeIdentitySecretName" -}}
{{- end -}}
{{- if not $metering.volumeIdentitySecretKey -}}
{{- fail "infrastructureMetering.pvInventoryEnabled requires volumeIdentitySecretKey" -}}
{{- end -}}
{{- if not $metering.volumeIdentityKeyVersion -}}
{{- fail "infrastructureMetering.pvInventoryEnabled requires volumeIdentityKeyVersion" -}}
{{- end -}}
{{- if not (regexMatch "^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$" $metering.volumeIdentityKeyVersion) -}}
{{- fail "infrastructureMetering.volumeIdentityKeyVersion must be a stable bounded key version" -}}
{{- end -}}
{{- if or (gt (len $metering.volumeIdentitySecretName) 253) (not (regexMatch "^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$" $metering.volumeIdentitySecretName)) -}}
{{- fail "infrastructureMetering.volumeIdentitySecretName must be a valid Kubernetes Secret name" -}}
{{- end -}}
{{- if not (regexMatch "^[-._A-Za-z0-9]+$" $metering.volumeIdentitySecretKey) -}}
{{- fail "infrastructureMetering.volumeIdentitySecretKey must be a valid Secret data key" -}}
{{- end -}}
{{- if eq $metering.volumeIdentitySecretName $metering.storageIngestionSecretName -}}
{{- fail "infrastructureMetering.volumeIdentitySecretName must be separate from storageIngestionSecretName" -}}
{{- end -}}
{{- if and $metering.ingestionSecretName (eq $metering.volumeIdentitySecretName $metering.ingestionSecretName) -}}
{{- fail "infrastructureMetering.volumeIdentitySecretName must be separate from ingestionSecretName" -}}
{{- end -}}
{{- end -}}

{{- end -}}
