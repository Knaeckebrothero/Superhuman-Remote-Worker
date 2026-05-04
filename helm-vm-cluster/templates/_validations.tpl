{{/*
Render-time validations. Called from license-gate.yaml so they fire
before any resource is rendered.
*/}}
{{- define "srwvm.validate" -}}

{{- /* nats.hubUrl is required — the controller can't function without a hub */ -}}
{{- if not .Values.nats.hubUrl -}}
{{- fail "nats.hubUrl is required — the cross-cluster vm-controller dials this URL to reach the orchestrator's NATS hub. Example: nats://10.0.50.101:30743 (the NodePort exposed by the main chart's nats.leafNodePort)." -}}
{{- end -}}

{{- /* Headscale: pick exactly one credential source */ -}}
{{- if and .Values.headscale.apiKeySecret .Values.headscale.apiKeyVaultPath -}}
{{- fail "headscale.apiKeySecret and headscale.apiKeyVaultPath are mutually exclusive — set exactly one." -}}
{{- end -}}
{{- if and (not .Values.headscale.apiKeySecret) (not .Values.headscale.apiKeyVaultPath) -}}
{{- fail "Provide one of headscale.apiKeySecret (pre-existing K8s Secret) or headscale.apiKeyVaultPath (Vault path, requires externalSecrets.enabled=true). The vm-controller calls headscale to register provisioned VMs." -}}
{{- end -}}
{{- if and .Values.headscale.apiKeyVaultPath (not .Values.externalSecrets.enabled) -}}
{{- fail "headscale.apiKeyVaultPath requires externalSecrets.enabled=true (the chart renders an ExternalSecret to sync the API key from Vault). Either flip externalSecrets.enabled, or switch to headscale.apiKeySecret (a pre-created K8s Secret)." -}}
{{- end -}}

{{- /* SSH public key: pick exactly one source */ -}}
{{- if and .Values.ssh.publicKey .Values.ssh.publicKeyVaultPath -}}
{{- fail "ssh.publicKey and ssh.publicKeyVaultPath are mutually exclusive — set exactly one." -}}
{{- end -}}
{{- if and (not .Values.ssh.publicKey) (not .Values.ssh.publicKeyVaultPath) -}}
{{- fail "Provide one of ssh.publicKey (inline ssh-ed25519/ssh-rsa string) or ssh.publicKeyVaultPath (Vault path with key ssh-publickey). This key is authorized into the VMs' authorized_keys at boot — must match the private key the orchestrator (chart 1) holds." -}}
{{- end -}}
{{- if and .Values.ssh.publicKeyVaultPath (not .Values.externalSecrets.enabled) -}}
{{- fail "ssh.publicKeyVaultPath requires externalSecrets.enabled=true. Either flip externalSecrets.enabled, or switch to ssh.publicKey (inline)." -}}
{{- end -}}

{{- /* Tailscale: pick exactly one credential source when enabled */ -}}
{{- if .Values.tailscale.enabled -}}
{{- if and .Values.tailscale.authKeySecret .Values.tailscale.authKeyVaultPath -}}
{{- fail "tailscale.authKeySecret and tailscale.authKeyVaultPath are mutually exclusive — set exactly one when tailscale.enabled=true." -}}
{{- end -}}
{{- if and (not .Values.tailscale.authKeySecret) (not .Values.tailscale.authKeyVaultPath) -}}
{{- fail "tailscale.enabled=true requires either tailscale.authKeySecret or tailscale.authKeyVaultPath. The auth key is injected into VMs at boot so they join the headscale tailnet." -}}
{{- end -}}
{{- if and .Values.tailscale.authKeyVaultPath (not .Values.externalSecrets.enabled) -}}
{{- fail "tailscale.authKeyVaultPath requires externalSecrets.enabled=true. Either flip externalSecrets.enabled, or switch to tailscale.authKeySecret." -}}
{{- end -}}
{{- end -}}

{{- end -}}
