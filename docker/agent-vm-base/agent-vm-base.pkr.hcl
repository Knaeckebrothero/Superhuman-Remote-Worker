# =============================================================================
# Agent VM Base Image — Packer Template
# =============================================================================
#
# Builds a KubeVirt-compatible containerDisk image from Ubuntu 24.04 cloud.
#
# Prerequisites:
#   - packer, qemu-system-x86_64, genisoimage (or cloud-localds)
#   - Download the Ubuntu cloud image first:
#     wget https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img \
#       -O input/noble-server-cloudimg-amd64.img
#
# Build:
#   cd docker/agent-vm-base
#   packer init agent-vm-base.pkr.hcl
#   packer build agent-vm-base.pkr.hcl
#
# Output:
#   output/agent-vm-base.qcow2
#
# Then wrap for KubeVirt containerDisk:
#   podman build -t ghcr.io/knaeckebrothero/agent-vm-base:latest -f Dockerfile.containerDisk .
#   podman push ghcr.io/knaeckebrothero/agent-vm-base:latest
# =============================================================================

packer {
  required_plugins {
    qemu = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

# -----------------------------------------------------------------------------
# Variables
# -----------------------------------------------------------------------------

variable "ubuntu_image" {
  type        = string
  default     = "input/noble-server-cloudimg-amd64.img"
  description = "Path to the Ubuntu 24.04 cloud image (qcow2)"
}

variable "ssh_username" {
  type    = string
  default = "packer"
}

variable "ssh_password" {
  type    = string
  default = "packer"
}

variable "disk_size" {
  type        = string
  default     = "20G"
  description = "VM disk size"
}

variable "memory" {
  type    = number
  default = 4096
}

variable "cpus" {
  type    = number
  default = 4
}

# -----------------------------------------------------------------------------
# Source: QEMU builder with Ubuntu cloud image + cloud-init seed
# -----------------------------------------------------------------------------

source "qemu" "agent-vm-base" {
  iso_url      = var.ubuntu_image
  iso_checksum = "none"
  disk_image   = true

  output_directory = "output"
  vm_name          = "agent-vm-base.qcow2"
  format           = "qcow2"
  disk_size        = var.disk_size

  headless    = true
  accelerator = "kvm"
  memory      = var.memory
  cpus        = var.cpus

  ssh_username           = var.ssh_username
  ssh_password           = var.ssh_password
  ssh_timeout            = "15m"
  ssh_handshake_attempts = 200

  # Shutdown is handled by cleanup.sh (must happen before packer user is deleted)
  shutdown_command = ""
  shutdown_timeout = "2m"

  # Attach cloud-init seed ISO for first-boot configuration
  cd_files = ["cloud-init/*"]
  cd_label = "cidata"

  # Network: user-mode (SLIRP) with virtio — provides NAT + DHCP to guest
  net_device = "virtio-net"
}

# -----------------------------------------------------------------------------
# Build
# -----------------------------------------------------------------------------

build {
  sources = ["source.qemu.agent-vm-base"]

  # Wait for cloud-init to finish and diagnose issues
  provisioner "shell" {
    inline = [
      "echo 'Waiting for cloud-init to complete...'",
      "sudo cloud-init status --wait || true",
      "echo '--- cloud-init status ---'",
      "sudo cloud-init status --long || true",
      "echo '--- cloud-init log (last 30 lines) ---'",
      "sudo tail -30 /var/log/cloud-init.log || true",
      "echo '--- network interfaces ---'",
      "ip addr show || true",
      "echo '--- DNS config ---'",
      "cat /etc/resolv.conf || true",
      "echo '--- testing network ---'",
      "wget -q --spider --timeout=10 http://archive.ubuntu.com && echo 'Network OK' || echo 'Network unreachable (non-fatal)'",
    ]
  }

  # Copy management daemon files
  provisioner "file" {
    source      = "files/management-daemon.py"
    destination = "/tmp/management-daemon.py"
  }

  provisioner "file" {
    source      = "files/management-daemon.service"
    destination = "/tmp/management-daemon.service"
  }

  provisioner "file" {
    source      = "files/code-server-config.yaml"
    destination = "/tmp/code-server-config.yaml"
  }

  # Main provisioning script
  provisioner "shell" {
    script = "scripts/provision.sh"
    environment_vars = [
      "DEBIAN_FRONTEND=noninteractive",
    ]
  }

  # Clean up for minimal image size
  provisioner "shell" {
    script = "scripts/cleanup.sh"
  }
}
