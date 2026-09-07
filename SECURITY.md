# Security policy

## Supported versions

Superhuman Remote Worker is under active development. Security reports are
evaluated against the current `main` branch and the latest published tag. If a
report affects an older release, maintainers may ask the reporter to confirm
whether the issue is still present in a current version.

Deployments should use matching, immutable chart and component versions rather
than a moving development branch. Operators remain responsible for Kubernetes,
container-runtime, hypervisor, identity-provider, database, and bundled-service
updates.

## Report a vulnerability

Do not open a public issue for a suspected vulnerability.

Use
[GitHub Private Vulnerability Reporting](https://github.com/Knaeckebrothero/Superhuman-Remote-Worker/security/advisories/new)
and include:

- a description of the vulnerability and likely impact;
- the affected component and release or commit;
- deployment assumptions needed to reproduce it;
- reproduction steps or a proof of concept; and
- any known mitigation or evidence that the issue is being exploited.

You can expect an initial response within seven days. Please keep the report and
proof of concept private until maintainers have coordinated a fix and
disclosure.

## Scope

Security reports may include:

| Area | Examples |
|---|---|
| Authentication and authorization | OIDC/BFF bypass, role or grant bypass, IDOR, token confusion |
| Workspace isolation | Container or VM escape, harness/workspace boundary bypass, cross-job access |
| Agent execution | Prompt injection that crosses an enforced capability boundary, privileged-command gate bypass |
| APIs and realtime transport | Injection, cross-user WebSocket access, unsafe dispatch or lifecycle mutation |
| Data stores | Query injection, tenant-scope bypass, unauthorized knowledge or audit access |
| Secrets | Credential exposure in workspaces, logs, images, APIs, or rendered manifests |
| Supply chain | Dependency, image, build, release, or CI/CD compromise |

Ordinary model mistakes, incorrect answers, or prompt injection that causes only
actions already and intentionally authorized for that user are generally
product-safety issues rather than security-boundary vulnerabilities. Reports
that demonstrate a bypass of an enforced boundary are in scope.

The following are normally out of scope:

- vulnerabilities wholly inside an unmodified third-party service that should
  be reported to its upstream project;
- denial of service against a disposable local development deployment;
- social engineering without a product vulnerability; and
- reports produced only by automated scanners without a reproducible impact.

## Security architecture

SRW treats agent inputs, generated code, repositories, and shell-capable
workspaces as potentially hostile. The orchestrator and agent harness are kept
outside those workspaces, and the orchestrator remains authoritative for final
job state. Containers provide a weaker boundary than dedicated VMs, especially
when privileged FUSE support is enabled.

Read the public [security model](docs/security-model.md) for trust assumptions,
defense in depth, known limitations, and deployment guidance.
