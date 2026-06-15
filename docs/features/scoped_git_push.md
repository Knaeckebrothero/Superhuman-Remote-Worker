---
tags:
  - git-integration
  - credential-management
  - tool-development
  - cloud-infrastructure
  - security
---

# Scoped Git Push (Attributed Agent Pushes to External Repos)

> **Status (2026-06-15):** Proposed (design). Not yet implemented. Builds on the
> shipped [[repo_datasource]] clone flow and [[credential_file_datasources]]
> materialization + encryption-at-rest. **Completes the deferred write-tool half
> of [[repo_datasource]] §2 / Phase 1** (`repo_commit`, `repo_push`, `repo_pull`),
> and adds a credential indirection + configurable commit identity on top.

## Motivation

A user wants an agent to push to **one specific repository of their GitHub
account** with two properties:

1. **Scoped credential** — the credential the agent holds must not be able to
   touch the rest of the account. ("Can I restrict the SSH key to a single
   repo?")
2. **Distinct identity** — commits and pushes should be attributed to the agent
   (e.g. "Claude"), not to the human account owner, even though the underlying
   authorization derives from that owner. ("Agents should push as Claude, not
   as me.")

### What exists today, and why it's not enough

The plumbing is ~80% present but crude:

- **Clone with a token already works** — `clone_repository_datasources()`
  (`src/core/datasource_setup.py:775-780`) embeds an `oauth2:<token>@host`
  credential **directly in the remote URL**. Consequences: the token lands at
  rest in the cloned repo's `.git/config`, it cannot be rotated, and it leaks
  into any process that reads the remote URL.
- **No push/commit tools** — the agent-facing `repo_commit` / `repo_push` /
  `repo_pull` tools were specced but **deferred** ([[repo_datasource]] §2,
  Phase 1). A crude `run_command("git -C repos/<name> push")` already succeeds
  once a remote has credentials, but there is no clean, identity-aware,
  read-only-respecting surface.
- **Commit identity is hardcoded** — `Agent <agent@workspace.local>` is set in
  **four** places in `src/managers/git_manager.py` (`init_repository()`
  148-149; `clone()` backend path 794-795; `clone()` local path 824-825;
  `from_worktree()` 874-875). There is no per-job / per-repo override.

### Why a fine-grained PAT, not an SSH deploy key

A GitHub **deploy key** (an SSH key added to one repo) would satisfy the
"scope to one repo" ask directly, and the [[credential_file_datasources]]
`ssh_key` path already delivers SSH keys to the workspace. We deliberately do
**not** build on that here.

The chosen end-state identity is a **GitHub App bot** (`<app>[bot]`), decided
during design ("Bot via GitHub App, defer build"). A GitHub App authenticates
git over **HTTPS using a short-lived installation token** presented as
`username=x-access-token`, `password=<token>`. A **fine-grained personal access
token** uses the *identical* HTTPS scheme and is *also* repo- and
permission-scoped. So the PAT:

- provides the same one-repo restriction the deploy key would (its repo scope),
- and exercises the exact transport the App will use — so the App later drops in
  as "a different token source" behind an unchanged agent-side seam.

The SSH/deploy-key path remains supported as-is for users who prefer it; it is
simply not what this feature extends.

## Relationship to existing design

| Concern | Source of truth | This doc |
|---|---|---|
| Cloning an external repo into `repos/<name>/` on the workspace backend | [[repo_datasource]] (shipped: `clone_repository_datasources`) | Reused unchanged for the clone; auth step is hardened |
| Encrypted credential storage + file materialization + cleanup manifest | [[credential_file_datasources]] (shipped) | Token file materialized + cleaned up via the same pattern |
| `GitManager` push/commit/remote primitives | `src/managers/git_manager.py` (shipped) | Identity made configurable; primitives reused by new tools |
| Agent-facing write tools (`repo_*`) | [[repo_datasource]] §2 (deferred) | **Implemented here** |
| Bot identity via App installation tokens | future (deferred) | Designed-for via the credential seam; not built |
| Trust posture (agent-readable secret) | [[credential_file_datasources]] §Trust model | Inherited (Posture 1); see below |

## Design

Five components. (1) is the forward-compatibility core; the rest are
straightforward extensions of shipped code.

### 1. Credential seam — token indirection (the forward-compat core)

**Replace token-in-URL with a git credential helper backed by a refreshable,
on-disk credential file.** The token never enters the remote URL or
`.git/config`.

Concretely, in the `token` branch of `clone_repository_datasources()` (today
`datasource_setup.py:775-780`), instead of rewriting the URL:

1. Materialize a `0600` git-credentials file on the workspace backend (via
   `backend.write_home_file`, mirroring the existing SSH-key block at
   `datasource_setup.py:721-758`), e.g. at
   `~/.config/srw/git-credentials/<ds-slug>`, containing one line:

   ```
   https://x-access-token:<token>@github.com/<owner>/<repo>.git
   ```

   Track it in the cleanup manifest (same machinery as
   `process_credential_files` / `cleanup_credential_files`) so it is removed at
   job teardown.
2. Clone with the **clean** HTTPS URL (no embedded secret) plus the helper
   wired in for that invocation, then persist the helper in the cloned repo so
   later pushes reuse it:

   ```bash
   git -c credential.helper="store --file=<path>" \
       -c credential.useHttpPath=true \
       clone https://github.com/<owner>/<repo>.git repos/<name>
   git -C repos/<name> config credential.helper "store --file=<path>"
   git -C repos/<name> config credential.useHttpPath true
   ```

   `credential.useHttpPath=true` makes git match the stored credential on the
   full path, so two repository datasources pointing at different GitHub repos
   (each with its own scoped PAT) don't collide on `github.com`.

**Why a config-based helper rather than `GIT_ASKPASS`:** `GitManager._run_git`
executes git through `backend.shell_run` and does **not** inject per-command
environment variables. A `credential.helper` lives in git config and is
therefore picked up by every subsequent git invocation on that repo with no
env threading — so `GitManager.push()` works unmodified. `GIT_ASKPASS` would
require plumbing env through the backend shell layer.

**At-rest footprint:** the only place the token persists on the workspace is the
`0600` store file (encrypted at rest in Postgres; ephemeral on the workspace —
see Security posture). It is *not* in `.git/config`'s remote URL, and `git
remote -v` shows the clean URL.

### 2. Configurable commit identity

Add an optional `commit_identity` to the repository datasource:

```json
{ "commit_identity": { "name": "Claude", "email": "claude@<configured-domain>" } }
```

Thread it into the cloned repo's git config in place of the hardcoded values.
Refactor the four `git_manager.py` sites to a single helper:

```python
def _configure_identity(self, name: str | None = None, email: str | None = None):
    self._run_git(["config", "user.name",  name  or DEFAULT_AGENT_NAME])
    self._run_git(["config", "user.email", email or DEFAULT_AGENT_EMAIL])
```

`DEFAULT_AGENT_NAME` / `DEFAULT_AGENT_EMAIL` preserve today's
`Agent <agent@workspace.local>` for the **internal workspace** repo (the
Gitea-backed versioning repo is unaffected). For an **external repository
datasource**, `clone_repository_datasources()` passes the datasource's
`commit_identity` (falling back to a system-wide default, e.g.
`Claude <claude@...>`, set in config/helm).

Notes:
- Identity is cosmetic-but-real: GitHub renders the configured author on each
  commit. With a noreply-style or bot email it shows as that identity, fully
  decoupled from the owner. Later, the App supplies its `<app>[bot]` identity
  through this same field.
- Optional, deferred: append a `Co-authored-by:` trailer naming the human owner
  for provenance. Out of scope for v1.

### 3. Write tools (finish the deferred half)

New dedicated tools, operating on the existing
`workspace_manager.source_repos[<name>]` `GitManager` instances (registered at
`datasource_setup.py:793`). The `repo` argument is the **clone-directory name**
surfaced in `datasources.md` (the `source_repos` key from
`resolve_repo_clone_names`), which may differ from the datasource label when two
datasources collide on the same upstream name:

| Tool | Behaviour |
|---|---|
| `repo_commit(repo, message)` | Stage + commit in `repos/<repo>/`, using the datasource's configured identity. |
| `repo_push(repo, branch=None)` | Push current (or named) branch via `GitManager.push()`; credential helper supplies auth. |
| `repo_pull(repo, branch=None)` | Fast-forward pull via `GitManager.pull()`. |

Design decisions:
- **Dedicated tools, not `run_command`** — per [[repo_datasource]] §2's
  recommendation. The tool enforces identity, respects read-only, and stays
  auditable. (`run_command("git push")` still works once the helper is set; the
  tool is the supported surface.)
- **Gating by `read_only` / `sync_mode`** — a `readonly` datasource exposes
  `repo_pull` only; `repo_commit` / `repo_push` refuse with a clear message.
  Vocabulary already defined in [[repo_datasource]] §5.
- **Branch handling** — the agent pushes the repo's current branch; it may
  create/switch branches with the existing `GitManager.checkout_branch`
  (exposed if not already). Default branch comes from the datasource
  `default_branch` (already honored at clone, `datasource_setup.py:791-792`).
- **Read tools** — the existing read-only git tools gain an optional `repo`
  parameter ([[repo_datasource]] §2 Option A) so `git_log(repo="...")` etc.
  inspect the datasource repo. (Small, can land with this or follow on.)

### 4. Storage model

No schema migration required — reuse the encrypted `datasources.credentials`
JSONB ([[credential_file_datasources]] encryption-at-rest):

```jsonc
{
  "auth_method": "token",            // existing; "token" | "ssh"
  "token": "github_pat_...",          // existing fine-grained PAT
  "commit_identity": {                // NEW (optional)
    "name": "Claude",
    "email": "claude@..."
  }
}
```

`sync_mode` / `default_branch` / clone `path` live where [[repo_datasource]] §6
puts them (config JSONB or credentials, per that doc's resolution). This feature
adds only `commit_identity`.

### 5. UI, validation, and scope guidance (cockpit + orchestrator)

- The repository-datasource form surfaces the **token** auth method with inline
  guidance: *"Create a GitHub fine-grained PAT limited to this one repository,
  with `Contents: write` (and `Pull requests: write` if you want PRs). Paste it
  here."* Link to GitHub's fine-grained PAT settings.
- Add optional **Commit identity** fields (name + email) with a sensible
  placeholder default.
- An **explainer** of what GitHub will show: commits authored by the configured
  identity; the push authorized by the scoped PAT (not your interactive account
  session). Set expectations honestly per the Security posture.
- Orchestrator validation: extend the `repository` validator alongside the
  existing credential-file validators (`orchestrator/security/` + the
  `create_datasource` / `update_datasource` paths in `orchestrator/main.py`) to
  accept/normalize `commit_identity` (name non-empty; email matches a basic
  address regex).

## Security posture

This is **Posture 1 — agent-readable**, identical to the existing `ssh_key`
datasource ([[credential_file_datasources]] §Trust model). The seam improves
at-rest hygiene (token out of `.git/config`, rotatable) but does **not** hide
the token from the agent: a determined agent can `cat` the `0600` store file.
The credential is *issued to the agent for use*, not hidden from it.

Mitigations and user responsibilities:
- **Scope the PAT** to exactly one repo with least privilege (`Contents: write`).
  Blast radius of leakage is then that one repo.
- **Ephemerality** — the workspace is `emptyDir` ([[workspace_storage_state_topology]]);
  the store file is gone at teardown. The encrypted copy in Postgres is the only
  durable one.
- **Snapshot / log hygiene** — a workspace snapshot would capture the store
  file. Treat snapshots accordingly (same caveat as SSH keys today). URL masking
  in `GitManager` (`_mask_url_static`) already prevents the token appearing in
  git logs once it's out of the URL.

Stronger postures (custom helper that fetches a token on demand from the
orchestrator so the agent never holds a long-lived secret; ssh-agent-style
forwarding) are the same Posture 2/3 future work listed in
[[credential_file_datasources]] §Future work — and the credential seam is the
natural place they'd attach.

## Forward-compatibility: the GitHub App seam

The whole point of choosing HTTPS-token over SSH. When the App is built later:

1. The orchestrator registers a GitHub App and the user installs it on selected
   repos (this replaces "paste a PAT").
2. At job dispatch (and on a refresh timer, since installation tokens expire
   ~hourly), the orchestrator **mints an installation token and writes it into
   the same `0600` store file** the agent already reads — or swaps the helper
   for a custom one that calls back to the orchestrator on demand.
3. **The agent side is unchanged.** Same `credential.helper`, same
   `x-access-token` username, same `commit_identity` mechanism (now carrying the
   `<app>[bot]` identity). `repo_push` / `repo_commit` don't change.

This is what makes the MVP provably not a dead end: only the token *source*
changes from "user-pasted PAT (static)" to "orchestrator-minted installation
token (refreshed)". Aligns with the Vault/secret-source direction in
[[deployment]].

## Out of scope (YAGNI)

- **Building the GitHub App** — designed-for, not built.
- **Auto-sync** (`sync_mode: auto`, commit/push on phase transitions) — MVP is
  agent-driven `manual`. The `auto` design already exists in [[repo_datasource]]
  §5 if wanted later.
- **Multi-repo-per-job** — the `uq_datasource_type_job` constraint
  ([[repo_datasource]] §4) still limits to one repository datasource per job.
  (`useHttpPath` is specified anyway so the design is robust if that lifts.)
- **PR creation** — a fine-grained PAT with `Pull requests: write` + `gh`/API
  makes this a small follow-on; not in v1.
- **SSH deploy-key extension** — the SSH path stays as-is; not extended here.
- **Hiding the token from the agent** (Posture 2/3) — future work.

## Implementation phases

### Phase 1 — Credential seam + identity (backend)
- Rewrite the `token` branch of `clone_repository_datasources()` to materialize
  a `0600` git-credentials store file + clone with the clean URL + persist the
  helper; register the file in the cleanup manifest.
- Refactor `git_manager.py`'s four identity sites to `_configure_identity()`;
  add `DEFAULT_AGENT_NAME/EMAIL`; pass `commit_identity` from the datasource for
  external repos (internal workspace repo keeps the current default).
- Validator: accept/normalize `commit_identity` on `repository` datasources.
- Tests: store-file materialization + cleanup; clean remote URL (no token in
  `.git/config`); identity applied to datasource repo, default preserved for the
  workspace repo.

### Phase 2 — Write tools (agent)
- `repo_commit` / `repo_push` / `repo_pull` over `source_repos`, gated by
  `read_only` / `sync_mode`; identity enforced on commit.
- Optional: `repo` parameter on existing read-only git tools.
- `datasources.md` index line per repo notes push availability + identity.
- Tests: push round-trip against a throwaway remote; read-only refusal;
  identity on resulting commits.

### Phase 3 — Cockpit UI + docs
- Token auth method with fine-grained-PAT scope guidance; commit-identity
  fields; the "what GitHub shows" explainer; i18n parity (en + de-DE).
- Update [[repo_datasource]] to mark the write tools shipped and point here.

## Open questions

- **Default identity** — system-wide default name/email (config/helm value) vs.
  required per-datasource? Lean: a config default (e.g. `Claude`) so the common
  case needs zero extra input.
- **Token verification at save time** — should "Test connection" do an
  authenticated `GET /repos/{owner}/{repo}` to confirm the PAT's scope before
  the agent ever runs? Cheap and high-value; candidate for Phase 3.
- **Push target branch policy** — push straight to `default_branch`, or steer
  agents toward a working branch by convention (cf. [[repo_resolution]])? For an
  external user repo, pushing to a feature branch is the safer default.
- **classic vs fine-grained PAT** — fine-grained is the documented
  recommendation; classic PATs work with the identical helper if a user supplies
  one. No code difference.

## Related

- [[repo_datasource]] — clone flow this builds on; its deferred write tools land here.
- [[credential_file_datasources]] — materialization, cleanup manifest, encryption-at-rest, Posture 1.
- [[coding_agent]] — primary consumer (clone → change → push/PR).
- [[deliverables]] — internal Gitea push for review; distinct from external-forge push.
- [[repo_resolution]] — per-job repo/branch conventions.
- [[auth_bff_and_api_tokens]] — token-shape precedent (`ak_*` PATs for the orchestrator API).
- [[workspace_storage_state_topology]] — why the on-disk token is ephemeral.
- [[deployment]] — Vault/secret-source direction the App token-minting aligns with.
- [[hardened_container]] — filesystem constraints, writable mounts, egress.
