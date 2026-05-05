# =============================================================================
# Agent VM Base Image — Stage 1 Packer Template
# =============================================================================
#
# Builds a "stage1" qcow2 with all the heavy, stable bits baked in: apt
# packages, datasource clients, Tailscale, Playwright Chromium, Node.js +
# minimal npm globals. Published to GHCR as a containerDisk. Stage 2 boots
# this qcow2 and layers per-commit config (user, daemon, sudo gate) on top.
#
# Rebuild trigger: changes to scripts/provision-stage1.sh, scripts/cleanup-
# stage1.sh, .playwright-version, cloud-init/, or this template. Otherwise
# the existing stage1 image is reused and stage2 finishes in ~5 minutes.
#
# Build:
#   packer init stage1.pkr.hcl
#   packer build -var "playwright_version=$(cat ../../.playwright-version)" stage1.pkr.hcl
#
# Output: output-stage1/agent-vm-base-stage1.qcow2
# =============================================================================

packer {
  required_plugins {
    qemu = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

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
  type    = string
  default = "20G"
}

variable "memory" {
  type    = number
  default = 4096
}

variable "cpus" {
  type    = number
  default = 4
}

variable "playwright_version" {
  type        = string
  description = "Playwright Python package version (read from .playwright-version at repo root). No default — Packer refuses to build without it, preventing silent drift."
}

source "qemu" "agent-vm-base-stage1" {
  iso_url      = var.ubuntu_image
  iso_checksum = "none"
  disk_image   = true

  output_directory = "output-stage1"
  vm_name          = "agent-vm-base-stage1.qcow2"
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

  # Shutdown is handled inside cleanup-stage1.sh
  shutdown_command = ""
  shutdown_timeout = "2m"

  cd_files = ["cloud-init/*"]
  cd_label = "cidata"

  net_device = "virtio-net"
}

build {
  sources = ["source.qemu.agent-vm-base-stage1"]

  # Wait for cloud-init to finish and capture diagnostics
  provisioner "shell" {
    inline = [
      "echo 'Waiting for cloud-init to complete...'",
      "sudo cloud-init status --wait || true",
      "echo '--- cloud-init status ---'",
      "sudo cloud-init status --long || true",
      "echo '--- network interfaces ---'",
      "ip addr show || true",
      "echo '--- DNS config ---'",
      "cat /etc/resolv.conf || true",
      "echo '--- testing network ---'",
      "wget -q --spider --timeout=10 http://archive.ubuntu.com && echo 'Network OK' || echo 'Network unreachable (non-fatal)'",
    ]
  }

  # Heavy provisioning: apt packages, datasource clients, Tailscale,
  # Python deps, Playwright Chromium, Node.js + minimal npm globals.
  provisioner "shell" {
    script = "scripts/provision-stage1.sh"
    environment_vars = [
      "DEBIAN_FRONTEND=noninteractive",
      "PLAYWRIGHT_VERSION=${var.playwright_version}",
    ]
  }

  # Light cleanup — preserve packer user, cloud-init state, machine-id, and
  # sshd PasswordAuthentication (stage 2 SSHs in as packer when it boots).
  provisioner "shell" {
    script = "scripts/cleanup-stage1.sh"
  }
}
