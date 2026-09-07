#!/usr/bin/env bash
# scripts/local-kubevirt-up.sh — install KubeVirt + CDI on the local k3d cluster so the
# same-cluster VM tier (vm.mode=same-cluster) can be exercised locally.
#
# Idempotent: re-running re-applies the operators/CRs and re-patches config. Nothing here touches
# the srw Helm release. Prerequisites (see helm/README.md "VM workspaces on your cluster"):
#   - the k3d cluster from scripts/local-dev-up.sh (node containers are privileged, so the host's
#     /dev/kvm, /dev/vhost-net and /dev/net/tun are visible inside the node when the kernel
#     modules were loaded BEFORE `k3d cluster create`);
#   - hardware virtualization on the host (`egrep -c '(vmx|svm)' /proc/cpuinfo`, `test -c /dev/kvm`).
#     Without it the script falls back to software emulation (USE_EMULATION=auto), which only
#     proves the wiring — the real agent VM image is unusable under TCG.
#
# The KubeVirt line follows the cluster's Kubernetes minor (KubeVirt supports the three newest
# k8s releases at its release time): 1.31/1.32 -> v1.6.x, 1.33 -> v1.8.x, >=1.34 -> v1.9.x.
# Override with KUBEVIRT_VERSION / CDI_VERSION.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-srw}"
KUBE_CONTEXT="${KUBE_CONTEXT:-k3d-$CLUSTER_NAME}"
VM_STORAGE_CLASS="${VM_STORAGE_CLASS:-local-path}"
USE_EMULATION="${USE_EMULATION:-auto}"          # auto | true | false
CPU_ALLOCATION_RATIO="${CPU_ALLOCATION_RATIO:-}" # e.g. 4; empty keeps KubeVirt's default (10)
CDI_VERSION="${CDI_VERSION:-v1.66.0}"
SMOKE="${SMOKE:-1}"                              # 1 = boot a cirros VMI + import a DataVolume
WAIT_KUBEVIRT="${WAIT_KUBEVIRT:-10m}"
WAIT_CDI="${WAIT_CDI:-5m}"
WAIT_SMOKE="${WAIT_SMOKE:-8m}"

KCTL="kubectl --context=$KUBE_CONTEXT"

step() { printf '==> %s\n' "$*"; }
ok()   { printf 'OK  %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*" >&2; }
die()  { printf 'ERROR %s\n' "$*" >&2; exit 1; }

command -v kubectl >/dev/null || die "kubectl not found"
command -v docker  >/dev/null || warn "docker not found — cannot inspect the k3d node for /dev/kvm"
[ "$KUBE_CONTEXT" = "k3d-$CLUSTER_NAME" ] \
  || die "local bootstrap requires context k3d-$CLUSTER_NAME (got $KUBE_CONTEXT)"

# --- 0. cluster + version ---------------------------------------------------------------------
step "cluster $KUBE_CONTEXT"
$KCTL get nodes >/dev/null 2>&1 || die "cluster $KUBE_CONTEXT not reachable (run scripts/local-dev-up.sh first)"
server_minor=$($KCTL version -o json 2>/dev/null | python3 -c 'import sys,json; v=json.load(sys.stdin)["serverVersion"]; print(int("".join(ch for ch in v["minor"] if ch.isdigit())))')
default_kubevirt_version() {
  local minor="$1"
  if   [ "$minor" -ge 34 ]; then echo v1.9.0
  elif [ "$minor" -eq 33 ]; then echo v1.8.4
  else                           echo v1.6.6
  fi
}
KUBEVIRT_VERSION="${KUBEVIRT_VERSION:-$(default_kubevirt_version "$server_minor")}"
ok "kubernetes 1.$server_minor -> KubeVirt $KUBEVIRT_VERSION, CDI $CDI_VERSION"

# --- 1. KVM availability inside the node ------------------------------------------------------
step "hardware virtualization"
node_container="k3d-${CLUSTER_NAME}-server-0"
have_kvm=0
if command -v docker >/dev/null && docker exec "$node_container" test -c /dev/kvm 2>/dev/null; then
  have_kvm=1
  ok "/dev/kvm present inside $node_container"
  docker exec "$node_container" test -c /dev/vhost-net 2>/dev/null \
    && ok "/dev/vhost-net present inside $node_container" \
    || warn "/dev/vhost-net missing inside the node — run 'sudo modprobe vhost_net' on the host and recreate the cluster, or VMs with virtio NICs will not schedule"
else
  warn "no /dev/kvm inside $node_container (host has $(grep -c -E '(vmx|svm)' /proc/cpuinfo 2>/dev/null || echo 0) virt-capable threads)"
fi
case "$USE_EMULATION" in
  auto)  if [ "$have_kvm" = 1 ]; then use_emulation=false; else use_emulation=true; fi ;;
  true|false) use_emulation="$USE_EMULATION" ;;
  *) die "USE_EMULATION must be auto|true|false" ;;
esac
if [ "$use_emulation" = true ]; then
  warn "software emulation (TCG) enabled — fine for this script's smoke test, unusable for the real agent VM image"
fi

# --- 2. KubeVirt ------------------------------------------------------------------------------
step "KubeVirt $KUBEVIRT_VERSION operator + CR"
kv_base="https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}"
$KCTL apply -f "${kv_base}/kubevirt-operator.yaml" >/dev/null
$KCTL apply -f "${kv_base}/kubevirt-cr.yaml" >/dev/null
kv_patch=$(python3 - "$use_emulation" "$CPU_ALLOCATION_RATIO" <<'PY'
import json, sys
use_emulation = sys.argv[1] == "true"
ratio = sys.argv[2]
dev = {"useEmulation": use_emulation}
if ratio:
    dev["cpuAllocationRatio"] = int(ratio)
print(json.dumps({"spec": {"infra": {"replicas": 1}, "configuration": {"developerConfiguration": dev}}}))
PY
)
$KCTL -n kubevirt patch kubevirt kubevirt --type merge -p "$kv_patch" >/dev/null
ok "KubeVirt CR patched (infra.replicas=1, useEmulation=$use_emulation${CPU_ALLOCATION_RATIO:+, cpuAllocationRatio=$CPU_ALLOCATION_RATIO})"
step "waiting for KubeVirt to become Available (<= $WAIT_KUBEVIRT)"
$KCTL -n kubevirt wait kv kubevirt --for condition=Available --timeout="$WAIT_KUBEVIRT" >/dev/null \
  || die "KubeVirt did not become Available: $($KCTL -n kubevirt get kv kubevirt -o jsonpath='{.status.conditions}')"
ok "KubeVirt Available"

# --- 3. CDI -----------------------------------------------------------------------------------
step "CDI $CDI_VERSION operator + CR"
cdi_base="https://github.com/kubevirt/containerized-data-importer/releases/download/${CDI_VERSION}"
# server-side apply: the CDI CRDs exceed the last-applied annotation limit of client-side apply
$KCTL apply --server-side --force-conflicts -f "${cdi_base}/cdi-operator.yaml" >/dev/null
$KCTL apply --server-side --force-conflicts -f "${cdi_base}/cdi-cr.yaml" >/dev/null
# CDI pulls containerDisks with its own client; k3d's containerd registry
# configuration does not apply. Trust HTTP only for this cluster's local
# registry, preserving any other registry entries already configured in CDI.
cdi_patch=$($KCTL get cdi cdi -o json | python3 -c '
import json, sys
config = json.load(sys.stdin).get("spec", {}).get("config", {}) or {}
registries = list(config.get("insecureRegistries") or [])
registry = sys.argv[2]
if registry not in registries:
    registries.append(registry)
print(json.dumps({"spec": {"config": {
    "featureGates": ["HonorWaitForFirstConsumer"],
    "scratchSpaceStorageClass": sys.argv[1],
    "insecureRegistries": registries,
}}}))
' "$VM_STORAGE_CLASS" "${CLUSTER_NAME}-registry:5000")
$KCTL patch cdi cdi --type merge -p "$cdi_patch" >/dev/null
ok "CDI CR patched (HonorWaitForFirstConsumer, scratchSpaceStorageClass=$VM_STORAGE_CLASS, HTTP registry=${CLUSTER_NAME}-registry:5000)"
step "waiting for CDI to become Available (<= $WAIT_CDI)"
$KCTL wait cdi cdi --for condition=Available --timeout="$WAIT_CDI" >/dev/null \
  || die "CDI did not become Available: $($KCTL get cdi cdi -o jsonpath='{.status.conditions}')"
ok "CDI Available"

# --- 4. StorageProfile for the VM storage class -----------------------------------------------
# rancher.io/local-path is not in CDI's capabilities table, so its StorageProfile has no
# claimPropertySets and a DataVolume that omits accessModes/volumeMode is rejected. The chart's
# templates set both explicitly; this patch makes hand-written DataVolumes work too.
step "StorageProfile $VM_STORAGE_CLASS"
for _ in $(seq 1 30); do
  $KCTL get storageprofile "$VM_STORAGE_CLASS" >/dev/null 2>&1 && break
  sleep 2
done
$KCTL get storageprofile "$VM_STORAGE_CLASS" >/dev/null 2>&1 || die "StorageProfile $VM_STORAGE_CLASS never appeared (is the StorageClass present?)"
$KCTL patch storageprofile "$VM_STORAGE_CLASS" --type merge \
  -p '{"spec":{"claimPropertySets":[{"accessModes":["ReadWriteOnce"],"volumeMode":"Filesystem"}]}}' >/dev/null
ok "StorageProfile $VM_STORAGE_CLASS: RWO/Filesystem"

# --- 5. node readiness ------------------------------------------------------------------------
step "node readiness"
schedulable=$($KCTL get nodes -l kubevirt.io/schedulable=true -o name | wc -l)
[ "$schedulable" -ge 1 ] || die "no node carries kubevirt.io/schedulable=true (virt-handler not ready?)"
kvm_alloc=$($KCTL get nodes -o jsonpath='{range .items[*]}{.status.allocatable.devices\.kubevirt\.io/kvm}{" "}{end}')
ok "schedulable nodes: $schedulable; allocatable devices.kubevirt.io/kvm: ${kvm_alloc:-none}"

# --- 6. smoke ---------------------------------------------------------------------------------
if [ "$SMOKE" = 1 ]; then
  step "smoke 1/2: boot a cirros VMI (masquerade, pod network)"
  $KCTL delete vmi srw-kubevirt-smoke -n default --ignore-not-found >/dev/null
  $KCTL apply -n default -f - >/dev/null <<EOF
apiVersion: kubevirt.io/v1
kind: VirtualMachineInstance
metadata:
  name: srw-kubevirt-smoke
spec:
  domain:
    devices:
      disks:
        - name: containerdisk
          disk:
            bus: virtio
      interfaces:
        - name: default
          masquerade: {}
    resources:
      requests:
        memory: 128Mi
  networks:
    - name: default
      pod: {}
  volumes:
    - name: containerdisk
      containerDisk:
        image: quay.io/kubevirt/cirros-container-disk-demo:${KUBEVIRT_VERSION}
EOF
  $KCTL wait vmi srw-kubevirt-smoke -n default --for=jsonpath='{.status.phase}'=Running --timeout="$WAIT_SMOKE" >/dev/null \
    || die "smoke VMI did not reach Running: $($KCTL get vmi srw-kubevirt-smoke -n default -o jsonpath='{.status.phase} {.status.conditions}')"
  vmi_ip=$($KCTL get vmi srw-kubevirt-smoke -n default -o jsonpath='{.status.interfaces[0].ipAddress}')
  pod_ip=$($KCTL get pod -n default -l vm.kubevirt.io/name=srw-kubevirt-smoke -o jsonpath='{.items[0].status.podIP}')
  [ -n "$vmi_ip" ] && [ "$vmi_ip" = "$pod_ip" ] && ok "VMI Running; status.interfaces[0].ipAddress == launcher pod IP ($vmi_ip)" \
    || warn "VMI Running but interface IP ($vmi_ip) != pod IP ($pod_ip)"
  $KCTL delete vmi srw-kubevirt-smoke -n default --wait=false >/dev/null

  step "smoke 2/2: CDI registry import into a $VM_STORAGE_CLASS DataVolume (WFFC + scratch space)"
  $KCTL delete dv srw-cdi-smoke -n default --ignore-not-found >/dev/null
  $KCTL apply -n default -f - >/dev/null <<EOF
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: srw-cdi-smoke
  annotations:
    cdi.kubevirt.io/storage.bind.immediate.requested: "true"
spec:
  source:
    registry:
      url: docker://quay.io/kubevirt/cirros-container-disk-demo:${KUBEVIRT_VERSION}
  storage:
    accessModes:
      - ReadWriteOnce
    volumeMode: Filesystem
    storageClassName: ${VM_STORAGE_CLASS}
    resources:
      requests:
        storage: 1Gi
EOF
  $KCTL wait dv srw-cdi-smoke -n default --for=jsonpath='{.status.phase}'=Succeeded --timeout="$WAIT_SMOKE" >/dev/null \
    || die "smoke DataVolume did not reach Succeeded: $($KCTL get dv srw-cdi-smoke -n default -o jsonpath='{.status.phase} {.status.conditions}')"
  ok "DataVolume import Succeeded"
  $KCTL delete dv srw-cdi-smoke -n default --wait=false >/dev/null
fi

step "done"
ok "KubeVirt $KUBEVIRT_VERSION + CDI $CDI_VERSION ready on $KUBE_CONTEXT (emulation=$use_emulation). Next: set vm.mode=same-cluster in deployment/values-local.yaml."
