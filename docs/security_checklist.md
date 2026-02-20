---
tags:
  - security
  - cloud-infrastructure
  - configuration
aliases:
  - container hardening
  - Docker security
  - agent security
related:
  - "[[sudo_approval_plugin]]"
  - "[[deployment]]"
  - "[[cloud_workspace]]"
---

# Container Security Checklist for Autonomous LLM Agents

Based on: *Security Architecture for Autonomous LLM Agents: A Comprehensive Analysis of Container Hardening*

---

## 1. Runtime Hardening and Kernel Isolation

### 1.1 Linux Capabilities

- [ ] Drop ALL capabilities: `cap_drop: [ALL]`
- [ ] Only add back capabilities that are strictly required (most Python/Node agents need zero)
- [ ] Verify CAP_SYS_ADMIN is never granted (primary container escape vector)
- [ ] Verify CAP_NET_RAW is dropped (prevents packet spoofing, ARP poisoning)
- [ ] Verify CAP_DAC_OVERRIDE is dropped (enforces file permission checks even for root)
- [ ] Verify CAP_SYS_PTRACE is dropped (prevents process injection and memory inspection)
- [ ] Verify CAP_NET_ADMIN is dropped (prevents routing/firewall modification)
- [ ] Verify CAP_MKNOD is dropped (prevents creation of block/character devices)
- [ ] If agent must install packages at runtime, consider CAP_CHOWN + CAP_SETUID (but prefer pre-baking)

### 1.2 Seccomp Profile

- [ ] Use a custom seccomp profile tailored to the agent workload (not just Docker's default)
- [ ] Profile the agent's syscalls using `strace` or `sysdig` during a comprehensive test run
- [ ] Block high-risk syscalls: `unshare`, `clone(CLONE_NEW*)`, `keyctl`, `mount`, `umount`, `ptrace`
- [ ] Set default action to `SCMP_ACT_ERRNO` (deny) or `SCMP_ACT_LOG` (audit mode)
- [ ] Apply via: `security_opt: [seccomp:./profiles/agent-strict.json]`
- [ ] Consider using [syscall2seccomp](https://github.com/antitree/syscall2seccomp) to auto-generate profiles

### 1.3 User Namespace Remapping

- [ ] Enable `userns-remap` in Docker daemon config (`/etc/docker/daemon.json`)
- [ ] Verify UID 0 inside container maps to a high-numbered unprivileged UID on host (e.g., 100000)
- [ ] Configure `/etc/subuid` and `/etc/subgid` for the remapped user
- [ ] Test that container escape yields no host privileges

### 1.4 Rootless Docker / Podman

- [ ] Consider running the container runtime itself as non-root (Rootless Docker or Podman rootless)
- [ ] Accept trade-offs: slirp4netns networking, cannot bind ports < 1024
- [ ] Verify `newuidmap` and `newgidmap` binaries are available on host

### 1.5 Alternative Runtimes

- [ ] **Development**: runc is acceptable with all other hardening applied
- [ ] **Production (recommended)**: gVisor (`runsc`) — syscall interception via user-space kernel, ~10-30% overhead
- [ ] **High security**: Kata Containers / Firecracker — hardware virtualization (microVM), each container gets its own kernel
- [ ] Apply via: `runtime: runsc` in docker-compose or `--runtime=runsc` on docker run

| Runtime | Isolation | Startup | Syscall Overhead | Security | Recommendation |
|---------|-----------|---------|-----------------|----------|---------------|
| runc | Namespaces + Cgroups | <100ms | Native | Low (shared kernel) | Dev only |
| gVisor | Syscall interception | ~200ms | 10-30% | High | Recommended |
| Kata/Firecracker | Hardware virtualization | ~300ms+ | Native (in VM) | Very high | High security |

---

## 2. Filesystem Security and Immutability

### 2.1 Immutable Root Filesystem

- [ ] Start container with `read_only: true`
- [ ] Mount tmpfs for directories that need writes:
  - [ ] `/tmp` — `tmpfs: /tmp:rw,noexec,nosuid,size=128m`
  - [ ] `/run` — `tmpfs: /run:rw,noexec,nosuid,size=8m`
- [ ] Use `noexec` flag on tmpfs mounts (prevents drop-and-execute attacks)
- [ ] Use `nosuid` flag on tmpfs mounts (prevents SUID escalation)
- [ ] Mount workspace as a dedicated volume with `read_only: false` scoped to `/app/workspace`

### 2.2 /proc Hardening

- [ ] Verify `MaskedPaths` are correctly configured (Docker default masks `/proc/kcore`, etc.)
- [ ] Verify `ReadonlyPaths` are correctly configured
- [ ] Keep container runtime patched (CVE-2025-31133 targeted masked path implementation)
- [ ] Consider custom masking of additional `/proc` entries not needed by the agent

### 2.3 Volume Security

- [ ] Mount secrets as read-only: `read_only: true` on secrets volume
- [ ] Never mount the Docker socket (`/var/run/docker.sock`) into agent containers
- [ ] Never mount host paths like `/etc`, `/usr`, `/proc`, `/sys` into agent containers
- [ ] Scope workspace volume to the minimum necessary path

---

## 3. Network Isolation Architecture

### 3.1 Network Segmentation

- [ ] Create a dedicated internal network: `docker network create --driver bridge --internal agent_internal_net`
- [ ] Set `internal: true` on the agent's network (no default gateway to outside world)
- [ ] Disable inter-container communication: `com.docker.network.bridge.enable_icc: "false"`
- [ ] Use service linking for database access (agent reaches DB on same internal network, neither reaches internet)

### 3.2 Egress Filtering

- [ ] Route all outbound traffic through an egress proxy (Squid, Envoy, Smokescreen)
- [ ] Whitelist only necessary domains (e.g., `pypi.org`, `github.com`, LLM API endpoints)
- [ ] Enable content inspection for data exfiltration patterns (large uploads, sensitive keywords)
- [ ] DNS filtering: prevent resolution of internal hostnames or malicious domains
- [ ] Set proxy environment variables:
  - [ ] `http_proxy=http://egress_proxy:3128`
  - [ ] `https_proxy=http://egress_proxy:3128`
  - [ ] `NO_PROXY=localhost,127.0.0.1`

### 3.3 Cloud Metadata Service (IMDS) Protection

- [ ] Block access to `169.254.169.254` (AWS/GCP/Azure metadata service)
- [ ] Host-level iptables rule: `iptables -I DOCKER-USER -d 169.254.169.254 -j DROP`
- [ ] Verify rule survives Docker daemon restarts (DOCKER-USER chain persists)

### 3.4 DNS Rebinding Defense

- [ ] Configure DNS resolver to reject answers pointing to private IP ranges (RFC1918)
  - Block: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
- [ ] Consider running a local DNS sidecar (dnsmasq) with `stop-dns-rebind` enabled
- [ ] Point container DNS to hardened resolver: `dns: [10.10.10.5]`

---

## 4. Resource Limits and DoS Protection

### 4.1 Memory

- [ ] Set hard memory limit: `deploy.resources.limits.memory: 512M` (adjust to workload)
- [ ] Never disable the OOM killer (`--oom-kill-disable`) without a memory limit
- [ ] Monitor for Exit Code 137 (OOM Killed) and implement backoff restart logic

### 4.2 CPU

- [ ] Set CPU limit: `deploy.resources.limits.cpus: '0.50'` (or pin to specific cores with `--cpuset-cpus`)
- [ ] Prevents CPU starvation of host and other containers

### 4.3 Process Limits (Fork Bomb Protection)

- [ ] Set PID limit: `deploy.resources.limits.pids: 100`
- [ ] An agent typically needs very few processes — 100 provides ample headroom
- [ ] Effectively neutralizes fork bombs (`:(){:|:&};:`)

### 4.4 File Descriptor Limits

- [ ] Set ulimits: `ulimits.nofile.soft: 1024`, `ulimits.nofile.hard: 2048`

### 4.5 Disk Quotas

- [ ] Configure overlay2 size limit: `--storage-opt size=1G` (requires XFS with pquota)
- [ ] Or use volume-level quotas on the workspace mount
- [ ] Monitor disk usage via the `/system/info` endpoint

---

## 5. Privilege Models and Identity Management

### 5.1 Non-Root Execution

- [ ] Dockerfile: `RUN groupadd -r agent && useradd -r -g agent agent` then `USER agent`
- [ ] Runtime enforcement: `user: "1000:1000"` in docker-compose
- [ ] Verify the application runs correctly as non-root

### 5.2 Package Installation Strategy

- [ ] **Preferred**: Pre-install all tools in the image ("fat but secured" container)
- [ ] **Alternative**: Configure pip/npm/cargo to install into user-owned directory (`/home/agent/.local/bin`) and add to `$PATH`
- [ ] Avoid granting sudo or root access at runtime

### 5.3 Preventing Privilege Escalation

- [ ] Set `security_opt: [no-new-privileges:true]`
- [ ] This prevents exploiting SUID binaries (passwd, sudo, ping) to gain root
- [ ] The `no_new_privs` kernel bit prevents privilege gain during `execve`, even with SUID bits set
- [ ] Low cost, high impact — should always be enabled

---

## 6. Secrets Management and Environment Hygiene

### 6.1 The `/proc/self/environ` Vulnerability

- [ ] Be aware: environment variables are readable by the agent via `env`, `printenv`, or `/proc/self/environ`
- [ ] A prompt injection attack can instruct the agent to dump all secrets

### 6.2 File-Based Secrets

- [ ] Mount secrets as files (Docker Secrets) into `/run/secrets/`
- [ ] Set permissions to 0400 (read-only by owner)
- [ ] Modify application code to read keys from files, not environment variables

### 6.3 Environment Scrubbing

- [ ] Use a wrapper entrypoint script that:
  1. Reads secrets from files or stdin
  2. Initializes the application context
  3. Launches the agent with a cleared environment: `exec env -i PATH="/bin:/usr/bin" HOME="/home/agent" python agent.py`
- [ ] Verify that `env` inside the running agent process does not show API keys
- [ ] Never pass secrets via `-e API_KEY=...` without scrubbing

---

## 7. Supply Chain Security

### 7.1 ToxicSkills Threat

- [ ] Do not allow agents to pull tools/skills/packages from the open internet at runtime
- [ ] Maintain a curated, vetted registry of approved tools
- [ ] Scan all agent code and skill definitions for obfuscated code or suspicious network calls

### 7.2 Image Provenance

- [ ] Use hardened base images (Docker Hardened Images / Wolfi) where possible
- [ ] Enable Docker Content Trust (Notary) or Sigstore for image signature verification
- [ ] Generate and maintain an SBOM (Software Bill of Materials) for the agent image
- [ ] Pin base image versions (never use `:latest` in production)

---

## 8. Monitoring and Anomaly Detection

### 8.1 Falco (Runtime Security)

- [ ] Deploy Falco (eBPF-based) to monitor kernel syscall stream
- [ ] Configure rules for agent-specific threats:

| Detection | Falco Rule Logic | Purpose |
|-----------|-----------------|---------|
| Reverse shells | `spawned_process` + `container` + `shell_procs` + `proc.pname in (netcat, bash)` | Detect agent spawning shell connected to network socket |
| Crypto mining | `proc.name in (miner_binaries)` or `proc.cmdline contains "stratum+tcp"` | Detect mining software signatures |
| Sensitive file access | `evt.type=open` + `fd.name startswith /etc/shadow` | Detect attempts to read host credentials |
| Unexpected outbound | `evt.type=connect` + `fd.sip != trusted_subnet` | Detect connections to non-whitelisted IPs (C2 servers) |
| Drift detection | `evt.type=open` + `fd.directory in (/bin, /usr/bin)` + `evt.dir=WRITE` | Detect modification of system binaries |

### 8.2 Application-Level Monitoring

- [ ] Use the `/system/info` endpoint for container visibility (CPU, memory, disk, processes, ports)
- [ ] Include `listening_ports` and `process_count` in agent heartbeat metrics
- [ ] Alert on unexpected listening ports or unusual process counts
- [ ] Log all `run_command` invocations with command, exit code, and duration

---

## 9. Reference: Hardened Docker Compose

```yaml
version: '3.8'

services:
  autonomous_agent:
    image: my-registry/hardened-agent:1.0.0
    container_name: secure_agent

    # 1. Identity & Privilege
    user: "1000:1000"
    security_opt:
      - no-new-privileges:true
      - seccomp:./profiles/agent.json
      # - apparmor:agent-profile

    # 2. Capabilities
    cap_drop:
      - ALL

    # 3. Filesystem Security
    read_only: true
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=64m
      - /run:rw,noexec,nosuid,size=8m
    volumes:
      - type: bind
        source: ./workspace
        target: /app/workspace
        read_only: false
      - type: bind
        source: ./secrets
        target: /run/secrets
        read_only: true

    # 4. Resource Quotas
    deploy:
      resources:
        limits:
          cpus: '0.50'
          memory: 512M
          pids: 100
    ulimits:
      nofile:
        soft: 1024
        hard: 2048

    # 5. Network Segmentation
    networks:
      - internal_net
    dns:
      - 10.10.10.5
    environment:
      - http_proxy=http://egress_proxy:3128
      - https_proxy=http://egress_proxy:3128
      - NO_PROXY=localhost,127.0.0.1

  # Egress Gateway
  egress_proxy:
    image: ubuntu/squid:latest
    networks:
      - internal_net
      - external_net
    volumes:
      - ./squid-whitelist.conf:/etc/squid/squid.conf:ro

networks:
  internal_net:
    internal: true
    driver_opts:
      com.docker.network.bridge.enable_icc: "false"
  external_net:
    driver: bridge
```

---

## 10. Trade-offs Summary

| Security Control | Operational Impact | Security Value |
|-----------------|-------------------|---------------|
| User namespaces | High complexity for volume permissions | **Critical**: Mitigates kernel escapes |
| Read-only root | Requires careful tmpfs configuration | **High**: Prevents persistence |
| Internal network | Breaks direct internet; requires proxy setup | **High**: Prevents C2 and exfiltration |
| gVisor runtime | 10-30% performance penalty on syscalls | **Critical**: Strongest isolation available |
| Cap drop ALL | May break debugging tools (ping, tcpdump) | **High**: Reduces attack surface |
| no-new-privileges | None for most workloads | **High**: Neutralizes SUID attacks |
| PID limit (100) | None for typical agents | **High**: Neutralizes fork bombs |
| Environment scrubbing | Requires entrypoint wrapper | **Critical**: Prevents secret leakage via prompt injection |

---

## Sources

Full bibliography (48 references) available in the source document: `docs/Secure LLM Agent Containers.pdf`

Key references:
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html)
- [Docker Engine Security](https://docs.docker.com/engine/security/)
- [Falco Rules Reference](https://falcosecurity.github.io/rules/)
- [runc Container Breakout Vulnerabilities (CNCF)](https://www.cncf.io/blog/2025/11/28/runc-container-breakout-vulnerabilities-a-technical-overview/)
- [Render: Security Best Practices for AI Agents](https://render.com/articles/security-best-practices-when-building-ai-agents)
- [Snyk: ToxicSkills Study](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/)
- [syscall2seccomp (Auto-generate seccomp profiles)](https://github.com/antitree/syscall2seccomp)

## Related

- [[sudo_approval_plugin]] - Human-in-the-loop privilege escalation for agents
- [[deployment]] - Deployment configuration and infrastructure
- [[cloud_workspace]] - Cloud infrastructure and workspace setup
