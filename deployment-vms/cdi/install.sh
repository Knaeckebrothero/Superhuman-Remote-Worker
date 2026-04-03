#!/usr/bin/env bash
# Install CDI (Containerized Data Importer) on the VM cluster.
#
# CDI enables KubeVirt to import container images into PVCs via
# DataVolume resources. This makes VM disks persistent — if a VMI
# pod crashes, KubeVirt restarts it against the same PVC.
#
# Run this once on the VM cluster (requires kubectl access):
#   bash deployment-vms/cdi/install.sh
#
# Verify:
#   kubectl get pods -n cdi
#   kubectl get cdi cdi -o jsonpath='{.status.phase}'  # should be "Deployed"

set -euo pipefail

CDI_VERSION="${1:-$(curl -s https://api.github.com/repos/kubevirt/containerized-data-importer/releases/latest | grep tag_name | cut -d '"' -f 4)}"

echo "Installing CDI ${CDI_VERSION}..."

kubectl apply -f "https://github.com/kubevirt/containerized-data-importer/releases/download/${CDI_VERSION}/cdi-operator.yaml"
kubectl apply -f "https://github.com/kubevirt/containerized-data-importer/releases/download/${CDI_VERSION}/cdi-cr.yaml"

echo "Waiting for CDI to become ready..."
kubectl wait --for=condition=Available --timeout=300s -n cdi deployment/cdi-operator

echo "CDI ${CDI_VERSION} installed successfully."
echo "Verify: kubectl get cdi cdi -o jsonpath='{.status.phase}'"
