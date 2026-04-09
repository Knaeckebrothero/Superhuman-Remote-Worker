# Security Policy

## Deployment Model

This project uses continuous deployment with SHA-based image tags — there are no versioned releases. The `main` branch is always the supported version. Kubernetes manifests are synced via Fleet, and secrets are managed through Vault/ESO.

**Keep your deployment up to date by pulling the latest images.**

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do not open a public issue.**
2. Use [GitHub Private Vulnerability Reporting](https://github.com/Knaeckebrothero/Uni-Projekt-Graph-RAG/security/advisories/new) to submit a report.
3. Include:
   - A description of the vulnerability and its potential impact
   - Steps to reproduce or a proof of concept
   - The component affected (Orchestrator, Agent, Cockpit, MCP Server, etc.)

You can expect an initial response within **7 days**. Accepted vulnerabilities will be patched on `main` and deployed as soon as possible.

## Scope

The following areas are considered in scope for security reports:

| Area | Examples |
|------|----------|
| **Authentication & Authorization** | Keycloak OIDC bypass, MCP token leaks, privilege escalation between roles |
| **Workspace Isolation** | Container/VM escape, cross-job data access, SSH credential exposure |
| **Agent Execution** | Prompt injection leading to unauthorized tool use, sudo gate bypass |
| **API Security** | Injection via job descriptions or config overrides, IDOR on REST endpoints |
| **Data Stores** | SQL injection (PostgreSQL), NoSQL injection (MongoDB), unauthorized vector store access |
| **Secrets Management** | Leaked API keys, Vault/ESO misconfigurations, credentials in logs or artifacts |
| **Supply Chain** | Compromised dependencies, CI/CD pipeline tampering |

### Out of Scope

- Vulnerabilities in third-party services (Keycloak, Neo4j, etc.) that are not caused by this project's configuration
- Denial-of-service attacks against development environments
- Social engineering

## Security Architecture Overview

- **Auth**: Keycloak OIDC with Bearer tokens for the Cockpit and REST API; `X-MCP-Token` header for Claude Code MCP integration
- **Workspace Isolation**: Agents execute in isolated Docker containers, Kubernetes pods, or QEMU VMs — never on the host
- **Secrets**: Injected at runtime via Vault/ESO; never stored in git or image layers
- **Network**: Internal services are not exposed externally; the Orchestrator API is the single entry point
- **Autonomy Controls**: Configurable autonomy levels (`full` → `dependent`) gate agent actions with human review
