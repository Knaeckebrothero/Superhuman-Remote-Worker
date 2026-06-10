---
tags:
  - strategy
  - release
  - licensing
  - open-source
  - product
related:
  - "[[2026-06-09-roadmap-priorities]]"
  - "[[2026-06-04-strategy-funding-and-next-steps]]"
  - "[[agent_open_source_split]]"
---

# Release Package And Licensing Strategy

**Date:** 2026-06-09  
**Status:** Decision draft, not legal advice. Review with a lawyer before a
public release or before accepting outside contributions.

## Short Recommendation

Do **not** publish the whole current repository as-is under MIT or another
permissive open-source license.

Recommended path:

1. **Create a clean public release package/repo**, not a dump of this private
   working tree.
2. **Open-source the orchestration core under `AGPL-3.0-or-later`:**
   - agent runtime
   - orchestrator/control-plane APIs
   - MCP server
   - workspace provisioning basics
   - config/expert/tool framework
   - CLI or minimal CRUD UI for jobs/sessions/review
3. **Keep the polished Cockpit and managed distribution as the official product
   layer**, at least initially.
4. **Use trademark/brand policy** so nobody can sell "Superhuman Remote Worker"
   as if it were your official hosted service.
5. **Require contributor terms** before friends, a professor, or external users
   contribute meaningful code.

This gives the practical benefits the project needs:

- source is visible, inspectable, forkable, and credible
- the public package is useful without giving away every polished product
  surface
- pilot customers can self-host
- companies can build real products on top of it when your system is supporting
  infrastructure rather than the product being sold
- contributors know what they are contributing to
- you preserve the right to sell official hosting, support, enterprise
  packaging, and a polished Cockpit experience
- you avoid pretending that a no-SaaS license is "open source"

## Important Current Fact: The Repo Has MIT Text

The root `LICENSE.txt` is currently MIT. MIT permits broad use, copying,
modification, distribution, sublicensing, and selling copies.

Implications:

- If this repo or any copy has already been distributed publicly under MIT,
  recipients likely keep those MIT rights for the versions they received.
- You can change the license for future versions only if you control the
  copyright to the code being relicensed.
- If other people have already contributed copyrightable code, you need their
  permission to relicense their contributions.

Before any release:

- audit whether this repository was ever public under MIT
- identify non-you contributors and copied-in code
- decide whether to preserve MIT for old code, relicense only new code, or get
  written contributor permission

## Vocabulary: Open Source vs Source Available

This distinction matters.

True open-source licenses cannot say "you may not sell this" or "you may not
use this to run a competing SaaS." The Open Source Definition requires free
redistribution and forbids field-of-endeavor restrictions.

So:

- **AGPL/GPL/Apache/MIT** = open source
- **BUSL/FSL/SSPL/Elastic-style no-SaaS terms** = source available / fair source,
  not OSI open source

If the product needs a no-resale or no-competing-hosting clause, call it
source-available or fair-source. That is still legitimate; it is just not open
source.

## License Options

### Option A: Whole Project AGPL

Use `AGPL-3.0-or-later` for the full release.

Pros:

- real open source
- strong credibility with developers
- network copyleft requires modified hosted versions to offer source
- simple story: "self-host it, modify it, contribute back"

Cons:

- does **not** stop someone from hosting and selling the unmodified system
- does **not** make you the only legal SaaS provider
- enterprise legal teams sometimes dislike AGPL
- the full product surface becomes available to competitors

Best if the goal is maximum trust/community and you accept competition on
hosting.

### Option B: Whole Project Source-Available

Use `BUSL-1.1`, `FSL-1.1`, or a similar source-available license for the whole
release.

Pros:

- source is public
- self-hosted use can be permitted
- competing hosted/resale use can be prohibited
- easier to preserve managed-hosting revenue

Cons:

- not open source
- some developers will reject it on principle
- contribution story is weaker unless contributor terms are very clear
- license language must be drafted carefully to avoid ambiguity

Best if the immediate product is commercial single-tenant hosting and the main
risk is someone reselling the whole system.

### Option B2: Timescale-Style Value-Added Use Grant

This is the most precise fit for the desired commercial posture.

The principle:

- allowed: internal use, self-hosting, consulting installs, evaluation, research
- allowed: using the system as infrastructure inside a larger value-added
  product or service
- allowed: distributing an image, appliance, or installer where the customer
  operates the instance for themselves
- prohibited without a commercial license: offering the system's core
  functionality to third parties as a managed/hosted service where the agent
  platform itself is the product
- prohibited without a commercial license: selling a lightly rebranded hosted
  "managed SRW" or "agent worker cloud" that substantially overlaps with your
  paid cloud/managed offering

This is similar in spirit to TimescaleDB's split: Apache-licensed open-source
parts plus source-available community features. Their public summary says
Community features are generally free as long as the user is not offering
TimescaleDB as a hosted DBaaS. The license then separately permits "Value Added
Products or Services" while prohibiting DBaaS/time-sharing/service offerings
where the licensed database functionality is exposed as the service.

For this project, the analogous boundary is:

> You may use the system to power your own business workflows or a larger
> application. You may not sell access to the system itself as a managed
> autonomous-agent/workspace platform that competes with the official hosted or
> managed offering.

This is better than an "as-is resale only" restriction. If the restriction only
blocks exact unmodified resale, a competitor can add a thin wrapper, rename it,
and argue they are no longer selling it "as-is." The protected category should
be "substantially similar managed agent-platform functionality," not only exact
copies.

### Option C: AGPL Orchestration Core + Official Cockpit

Open-source the useful backend/orchestration product, but do not make the
polished Cockpit the public project's required UI surface.

Suggested split:

| Component | Proposed license | Reason |
|---|---|---|
| Agent runtime, tool registry, core config examples | `AGPL-3.0-or-later` | Real open-source core; useful on its own; encourages trust and contributions |
| Orchestrator control plane | `AGPL-3.0-or-later` | The API surface is already the real backend product: jobs, sessions, agents, review, MCP |
| MCP server | `AGPL-3.0-or-later` | Makes the system immediately useful to coding-agent users |
| Minimal UI / CLI | `AGPL-3.0-or-later` | Required so the public package is runnable without the full Cockpit |
| Full Cockpit UI | official distribution, decide later | Commercial/polished client; not needed for core usefulness |
| Managed hosting / hardened Helm / enterprise packaging | commercial terms | Paid packaging, support, operations, upgrades |
| Docs | mixed: public docs under CC BY / product docs under repo license | Keep public onboarding reusable |
| Brand/logos/name | trademark policy, not software license | Prevent confusing unofficial hosted offerings |

This is the recommended model if the project wants to be "Nextcloud for AI
harnesses" rather than a source-available commercial product.

It is honest: the open-source release is useful and operational, but the
official polished experience and managed operations remain paid advantages. It
also matches the actual architecture: jobs, experts, sessions, result download,
approve/resume, and status views are API-backed CRUD/control-plane flows. A
basic Streamlit, CLI, or stripped-down Cockpit client can cover the community
surface without carrying the whole product UI.

Minimum public UI:

- create a job from JSON/YAML
- list jobs and statuses
- inspect job logs/audit summary
- download/open outputs
- approve or resume a pending-review job
- create/open a persistent session
- send/receive text in a basic session view
- manage expert config files at a simple JSON/YAML level

The full Cockpit can remain the official client with better UX, persistent chat
polish, admin pages, settings, design system, automations, usage dashboards,
cloud integration, and managed-instance operations.

## BUSL vs FSL vs SSPL

### BUSL-1.1

Business Source License is common in infrastructure products. It is
source-available and converts to an open-source "Change License" after a change
date, usually no later than four years. It supports an "Additional Use Grant"
where you define allowed production use.

Good fit if you want:

- self-hosted/internal production use allowed
- no competing hosted or embedded offering
- a familiar source-available pattern for infra buyers

Draft shape:

> You may use the Licensed Work for internal business purposes, evaluation,
> development, testing, research, self-hosted deployments, and value-added
> products or services, provided that the Licensed Work is not the primary
> autonomous-agent platform being offered to third parties and provided you do
> not offer the Licensed Work or substantially similar functionality as a
> hosted, managed, embedded, or resale service that competes with the Licensor's
> paid offerings.

Use a lawyer for the final wording.

### FSL-1.1

Functional Source License is a newer "fair source" license popularized by
Sentry. It usually converts to Apache or MIT after two years and uses a
non-compete style restriction.

Good fit if you want:

- simpler time-delayed source-available story
- shorter conversion window
- a clearer "fair source" label

Potential drawback: it is newer and may be less familiar to enterprise buyers
than BUSL.

### SSPL

SSPL is designed to target third-party service offerings. It is not OSI-approved
and tends to be controversial. For this project, it is probably heavier than
needed and may create more legal review friction than value.

## IP And Founder/Professor/Friend Contributions

Open-sourcing does not automatically solve IP ownership.

What it does help with:

- establishes public timestamped evidence of what existed before others joined
- lets outsiders inspect and run the code without private access
- reduces ambiguity around permitted use if the license is clear

What it does **not** solve:

- equity splits
- copyright assignment to a future company
- university IP claims if university resources/employment are involved
- rights to relicense future contributions
- whether a professor/friend owns code they contribute

Before accepting substantial code from friends, a professor, pilot customers, or
community contributors, choose one:

1. **DCO only:** contributors certify they have the right to submit code under
   the project's license. Simpler, good for pure open source.
2. **CLA:** contributors grant you/company broad rights to relicense and sell.
   Better for open-core/source-available/commercial licensing.
3. **Employment/contract assignment:** required for founders/employees if the
   future company must own the product.

For this project, use a CLA or assignment for anyone who might later claim
founder-level ownership.

## Release Package Scope

Do not publish the private working tree directly.

### Public v0.1 Scope

The public release should expose the system underneath the product UI:

- `agent.py` and `src/` agent runtime
- `orchestrator/` control plane, including job/session/review APIs
- `orchestrator/mcp/` MCP server
- `config/` expert, prompt, tool, and model configuration framework
- basic workspace provisioning and lifecycle code
- Helm/Compose evaluation deployment assets
- OpenAPI/protocol documentation
- a minimal operator UI or CLI

This is enough for a real open-source project. Jobs and experts are already API
objects: create endpoints accept JSON/YAML, status endpoints read the database,
and review flows are approve/resume/download operations. The public package
does not need the current full Cockpit to be useful.

### UI Policy

Do **not** publish the current Cockpit as the required public UI in v0.1.

The current Angular app is valuable but too broad for an initial public release:
persistent chat polish, settings, admin surfaces, automations, design-system
work, mobile layout, and product-specific flows make it a maintenance burden.

Preferred public UI path:

1. Ship the backend and APIs.
2. Add a deliberately small operator UI served by the orchestrator, or a small
   standalone client, covering only:
   - create job from JSON/YAML
   - list jobs and statuses
   - inspect job details/log/audit summary
   - download/open outputs
   - approve/resume pending-review jobs
   - create/open a basic persistent session
   - send/receive plain text in a session
   - view/edit expert config JSON/YAML
3. Keep the full Cockpit as the official polished client and managed-instance
   product surface until it is cleaned up enough to publish deliberately.

Implementation options:

- **Embedded operator UI:** small static/templated UI mounted by FastAPI. Best
  for one-command install and low maintenance.
- **Streamlit/quick client:** fastest prototype, acceptable for an evaluation
  release, but probably not the long-term public UI.
- **Shrunk Angular app:** reuses existing work, but risks dragging the current
  frontend complexity into the public release.

Recommendation: start with an embedded operator UI or tiny standalone client.
Avoid turning the public release into a frontend cleanup project.

### Docs Policy

Do **not** publish `docs/` wholesale.

The current docs folder is a private lab notebook: sprint notes, strategy,
issue analysis, design drafts, personal context, and idea collection. That is
useful internally but not appropriate as public project documentation.

Public docs should be curated from it:

- `README.md`
- `docs/quickstart.md`
- `docs/install.md`
- `docs/architecture.md`
- `docs/protocol/agent-http.md`
- `docs/protocol/orchestrator-callbacks.md`
- `docs/protocol/session-events.md`
- `docs/configuration.md`
- `docs/deployment.md`
- `docs/security.md`
- `docs/contributing.md`
- `docs/roadmap.md`
- `docs/known-limitations.md`

Internal docs should stay private unless rewritten:

- personal strategy/funding notes
- pilot/customer-specific notes
- raw issue investigations with private logs or IDs
- infrastructure details from `HomeLab/`
- unfinished idea dumps
- old sprint notes

After public release, GitHub Issues/Projects should become the public issue
tracker. Internal docs can remain the private scratchpad for messy thinking.

Exclude or sanitize:

- `.env`, local overlays, kube secrets, private keys
- `HomeLab/` and private infrastructure details
- personal strategy notes and financial/legal memos
- nested repos unless intentionally included
- generated caches, `.venv`, node modules, coverage files
- private customer/pilot references
- screenshots/logs containing secrets or customer data

Include:

- product README with one supported install path
- license files and component license map
- security policy
- contributing guide and CLA/DCO choice
- clean `.env.example`
- install/smoke-test runbook
- architecture overview
- protocol docs for agent/orchestrator boundary
- known limitations and roadmap
- third-party dependency/license notes

## Proposed Release Artifacts

Minimum release package:

```text
README.md
LICENSE.md
LICENSES/
  AGPL-3.0.txt
  BUSL-1.1.txt or FSL-1.1.txt
NOTICE.md
CONTRIBUTING.md
SECURITY.md
TRADEMARKS.md
docs/
  install.md
  quickstart.md
  architecture.md
  protocol/
    agent-http.md
    orchestrator-callbacks.md
    websocket.md
  release-notes/
    v0.1.0.md
examples/
  values.single-tenant.example.yaml
  docker-compose.eval.yaml
```

## Release Sequence

### Phase 0: Legal/ownership freeze

- stop treating MIT as the default
- decide whether old MIT distribution exists
- identify contributors
- decide CLA vs DCO
- choose provisional package name
- reserve trademark/domain if relevant

### Phase 1: Clean private release candidate

- create a release branch or separate public repo
- remove private infrastructure and personal docs
- add license map
- add release README
- add install/smoke runbook
- run secret scanning before publishing

### Phase 2: Private pilot release

- share source with a pilot under the intended license
- verify install path
- collect friction
- update docs
- do not optimize for community yet

### Phase 3: Public source release

- publish only after the pilot path works
- announce honestly as open-core / fair-source if using no-SaaS terms
- accept contributions only after CLA/DCO process exists

## Working Decision

Recommended decision for now:

> Publish a clean **source-visible single-tenant release package** under an
> open-core/fair-source model. Use `AGPL-3.0-or-later` for the generic agent
> runtime and `BUSL-1.1` or `FSL-1.1` for the full product shell. Permit
> self-hosted/internal use. Require a commercial license for competing managed
> hosting, resale, or embedding the full system as a paid service.

This matches the actual goal: keep building, allow trust and pilots, but avoid
giving away the exact hosted product as a free business for someone else.

## Sources To Review With Counsel

- Open Source Definition: https://opensource.org/osd
- GNU AGPLv3: https://www.gnu.org/licenses/agpl-3.0.html
- Business Source License 1.1: https://mariadb.com/bsl11/
- Functional Source License: https://fsl.software/
- MongoDB SSPL FAQ: https://www.mongodb.com/legal/licensing/server-side-public-license/faq
