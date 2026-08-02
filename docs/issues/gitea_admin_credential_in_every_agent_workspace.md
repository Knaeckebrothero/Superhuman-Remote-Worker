# The shared Gitea admin credential is in every agent workspace's `.git/config`

Status: **OPEN, unbuilt.** Filed 2026-08-01. Found while verifying
`docs/features/knowledge_base_repo_separation.md` on k3d.

All `file:line` verified at `74353152` + the uncommitted KB-separation tree.

## The finding

Every agent workspace is a checkout of its project's Gitea jobs repo, cloned from a URL that
embeds the **shared Gitea admin account**. Git stores the clone URL verbatim in
`.git/config` as `remote.origin.url`, and nothing scrubs it. So any command the model chooses
to run — `cat .git/config`, `git remote -v` — yields a working admin credential for the whole
Gitea instance.

The credential is not scoped to the project, or to the repo, or to the job. It is the
instance admin.

Evidence, all from the current tree:

- `GiteaClient.create_repo` (`orchestrator/services/gitea.py:311`) returns, per its own
  docstring at `:318`, *"Authenticated clone URL (`http://user:pass@host/user/repo.git`)"*.
- That value is stored as `project_repositories.repo_url`. Confirmed live: 29 rows on the dev
  cluster match `//[^@/]*:[^@/]*@`, and on k3d both the jobs and the new knowledge repo do.
- `WorkspaceManager` clones with it (`src/core/workspace.py:619`) and the code knows:
  `:629` reads *"(repo name only in the message — repo_url carries credentials.)"*
- `GitManager.clone` documents `url: Remote URL (may contain credentials)`
  (`src/managers/git_manager.py:754`) and masks it **only for logging** via
  `_mask_url_static` (`:933`). The unmasked URL is what `git clone` receives.
- No `remote set-url`, scrub, or sanitisation exists anywhere on that path.

## Why this is not already covered

`docs/features/scoped_git_push.md` (Proposed, 2026-06-15) asks precisely the right question
— *"Can I restrict the SSH key to a single repo?"* — but scopes itself to **external**
forges and explicitly excludes this case: *"the Gitea-backed versioning repo is unaffected"*
(:154–155) and *"internal workspace repo keeps the current default"* (:301–302).

So the external-push story has a design and the internal one does not.

## Blast radius

The workspace is not a trusted environment. It runs shell commands authored by a
non-frontier LLM, and this project has already observed agents doing surprising things with
their filesystem. With the instance admin credential in hand, a workspace can read, rewrite
or delete **every project's** jobs repo, knowledge vault, and job records — including the
change records that `workspace_and_change_records.md` makes the audit substrate. An agent
can rewrite the record of what it did.

Nothing suggests this has happened. The point is that nothing prevents it.

Related but distinct, and much less severe: `redact_datasource`
(`orchestrator/security/access.py:698`) only pops `credentials` and never sanitises
`connection_url`, whereas `redact_repository` (`:766`) strips embedded userinfo and
externalizes the host — its docstring calls the raw value "a credential leak to every project
member". Latent today (zero datasources on k3d or dev carry credentials in that field), but
it is why the KB-separation work deliberately stores a null `connection_url` on the
auto-created connector.

## Options

**A. Per-repo SSH deploy key.** Mint a keypair when the repo is created, register the public
half as a Gitea deploy key, store the private half encrypted, inject it at dispatch, and put
a clean `git@host:org/repo.git` in `repo_url`.
*For:* least privilege per repo; the secret becomes a key file in `~/.ssh` rather than a URL
in the repo config; write access is a per-key toggle; it is the pattern the codebase already
uses for repository datasources — `src/core/datasource_setup.py:913` branches on
`auth_method == "ssh"`, writes `~/.ssh/repo_<name>`, chmods it, and appends an `IdentityFile`
stanza at `:926`. The injection half is therefore already built and tested.
*Against:* Gitea SSH is **disabled** today — `GITEA__server__DISABLE_SSH: "true"`
(`helm/templates/services/gitea.yaml:200`) and the service exposes only `:3000`. Needs the
daemon enabled, a service port, and deploy-key methods on `GiteaClient` (none exist).

**B. Per-project scoped access token over the existing HTTP port.**
*For:* no infrastructure change; Gitea supports scoped tokens; much smaller diff.
*Against:* if the token is embedded in the remote URL it is still in `.git/config` — the
exposure shape is unchanged, only the privilege shrinks. Keeping it out of the repo config
means a credential helper or `askpass`, which is most of the work of A without the ergonomics.

**C. Scrub `.git/config` after clone and supply credentials per-invocation.**
*For:* smallest change; removes the at-rest copy.
*Against:* every push path must then inject the credential, and any missed path fails at the
worst moment. Treats the symptom.

**Recommendation: A**, with B as the interim if enabling Gitea SSH is unattractive. The
decisive argument for A is that the risky half — materialising key material into a workspace
over SSH — is already implemented and in production use for datasources; what is missing is
the Gitea-side plumbing, which is ordinary API work.

Whichever is chosen, it also dissolves an open question in
`knowledge_base_repo_separation.md` §8a: with a credential-free `repo_url` there is nothing
to redact, so the KB connector can carry a real URL and the cockpit gets something clickable.

## Acceptance criteria

1. A freshly provisioned project's workspace contains no credential in `.git/config` —
   `git remote -v` inside a live workspace shows a credential-free URL.
2. A workspace can still clone, commit, and push its own jobs repo, and the knowledge
   materialisation path is unaffected.
3. The credential a workspace holds cannot read or write another project's repo. Prove it
   with a negative test, not by inspection.
4. `project_repositories.repo_url` holds no embedded userinfo for new projects; existing rows
   are migrated or documented as legacy.
5. `redact_repository` remains correct for legacy rows (it must keep stripping, since old
   rows still carry credentials).
6. No regression in `_clone_auxiliary_repos`, the per-subdirectory `GitManager` used for
   repository datasources, or `close_backlog_ticket`'s Gitea writes.

## Notes for whoever picks this up

- 29 dev rows and every k3d row carry the credential today, so the migration question is
  real: rotating the admin password invalidates every stored `repo_url` at once.
- `redact_repository` already produces the safe, externalized form; reuse it rather than
  writing a second sanitiser.
- The tempting quick fix — masking the URL in logs — is already done
  (`_mask_url_static`) and is not the issue. The issue is the value on disk in the workspace.
