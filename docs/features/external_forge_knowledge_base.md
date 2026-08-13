---
tags:
  - feature
  - knowledge-management
  - git-integration
  - projects
  - datasources
status: implemented
created: 2026-08-13
related:
  - "[[knowledge_base_repo_separation]]"
  - "[[project_knowledge_base]]"
  - "[[repo_datasource]]"
  - "[[agent_open_source_split]]"
  - "[[public_datasources]]"
---

# External-forge knowledge bases (writable KB on GitHub)

> Let a project's **writable** knowledge vault live in an external forge repository —
> specifically a private GitHub repo — instead of the cluster's internal Gitea. Reading from
> external repos already works; only the write path is Gitea-bound.

**Status:** Implemented 2026-08-13. Decisions taken 2026-08-13 (§2).
Original `file:line` references were verified at `89dbadb0`.

## 1. Why

The design vault must leave the public code repo before the OSS release
([[agent_open_source_split]]), and the internal Gitea is the wrong destination for it:

- **Gitea on the dev instance is for development and testing.** It is an implementation
  detail of a deployment, not a system of record for the project's accumulated design work.
- **Everything belongs in one GitHub org** — the public code repo plus private vault repos
  beside it.
- **Access from outside the cluster**: browse, diff and edit the vault from GitHub, Obsidian,
  or another machine, without the cluster being in the path.
- **Local workflow is unchanged**: vault repos are cloned into the working tree as plain
  nested repos (the `HomeLab/` pattern — *not* submodules), so `rg docs/...` keeps working
  while agents read and write the same vault through the KB tools.

## 2. Decisions

| # | Decision |
|---|---|
| D1 | **Opt-in per project.** Gitea remains the default; a project may instead point its `knowledge`-role repo at an external forge. |
| D2 | **Live vault** = the project's writable native KB, backed by an external GitHub repo (this feature). |
| D3 | **History vault** = an ordinary external `kb` connector — **already supported, no new code**. |
| D4 | **Repos are created by hand** in the org and attached. SRW does not create them: auto-creation needs an org-admin credential, a large blast radius for little gain. |
| D5 | **Token lives on the project's native `kb` datasource row**, where credentials are already encrypted. |
| D6 | **Only the KB moves.** Jobs repo, cloud baseline and loop retros keep using Gitea; their `change_files` call sites are untouched. |

## 3. What already works, and what does not

**Reading external repos is solved.** `RemoteKnowledgeGitSource`
(`orchestrator/services/kb_git_source.py`) clones over a `git` subprocess and is
forge-agnostic. A private GitHub repo attached as a read-only `kb` connector works **today**,
credentials included. That is the whole of D3.

**Writing is Gitea-bound — but only by wiring, not by design.**
`materialize_knowledge_note` (`orchestrator/services/kb_materialize.py:153`) is explicitly
duck-typed; its own docstring says the parameter is *"`GiteaClient` (or any object with
`list_tree` / `change_files`)"*. So the write path needs a **new client**, not a refactor.

**There is one resolution seam.** `resolve_kb_repo` (`orchestrator/services/kb_reindex.py:923`)
returns a bare `(repo_name, branch)` — Gitea-shaped, dropping URL, forge and credentials. Its
docstring is emphatic that it is *"deliberately the only place that rule is written down:
every consumer — the sweep, the backlog mirror, the write path — resolves through here,
because a second copy is how the reader and the writer come to target different repos without
anything failing loudly."* Widening that one function is the main wiring change.

**A forge adapter already exists.** `src/services/forge.py` handles URL parsing
(`parse_owner_repo`), API-base resolution (`resolve_api_base`) and per-forge auth for
`github` / `gitea` / `gitlab` — but implements only `open_pull_request`. Its docstring notes
that *"Gitea deliberately mirrors GitHub's REST API, so those two differ only in API base and
auth scheme"*, which holds for everything this feature needs **except** multi-file commits.

## 4. Design

### 4.1 The client

Four calls, each with a direct GitHub equivalent of what Gitea already serves:

| need | Gitea today | GitHub |
|---|---|---|
| `list_tree` | `/repos/{o}/{r}/git/trees/{sha}?recursive=true` | `GET /repos/{o}/{r}/git/trees/{ref}?recursive=1` |
| `change_files` | ChangeFiles API (multi-file, one commit) | `PUT /repos/{o}/{r}/contents/{path}` (single file; blob sha required to update) |
| archive prefetch | `/repos/{o}/{r}/archive/{sha}.tar.gz` | `GET /repos/{o}/{r}/tarball/{ref}` |
| branch head | `/repos/{o}/{r}/branches/{branch}` | `GET /repos/{o}/{r}/branches/{branch}` |

**Multi-file atomicity is not needed.** `materialize_knowledge_note` commits *one note per
call*, so GitHub's single-file contents API suffices. Achieving Gitea's multi-file semantics
on GitHub would require the Git Data API (blobs → tree → commit → ref); explicitly out of
scope, and the KB write path must not start batching without revisiting this.

Build it against `forge.py`'s existing primitives rather than a second HTTP layer.

### 4.2 The seam

`resolve_kb_repo` returns a descriptor instead of `(repo_name, branch)`:

```
KbRepoRef(forge, repo_url, owner, repo, branch, credential_ref)
```

Consumers (`kb_materialize`, `kb_reindex`, the backlog mirror) select a client by `forge`.
Gitea stays the default and its behaviour is unchanged when `forge == "gitea"`.

### 4.3 Credentials (D5)

`project_repositories.credentials` is stored **plaintext** (`postgres.py:18114`,
`json.dumps(credentials)`) and the table is in `ALLOWED_TABLES` — admin-gated, but still the
wrong place for a GitHub PAT. `datasources` credentials are encrypted at rest
(`_encrypt_credentials_dict`, `postgres.py:12467`, and on every update path).

So the token goes on the project's **native `kb` datasource row** — the management surface
`create_project` already creates, marked `native_project_id`, with `connection_url`
deliberately `NULL`.

This does **not** reintroduce the §10 divergence that
[[knowledge_base_repo_separation]] warns about: that warning is about two copies of *where the
vault lives*. Location stays solely in `project_repositories`; the datasource row carries only
the credential.

Scope the PAT as narrowly as the forge allows — a fine-grained token limited to the vault
repos, contents read/write only.

### 4.4 Attachment

`create_project` gains an optional external-KB argument. When supplied it **skips Gitea repo
creation entirely** and records the external repo as the `knowledge`-role row — rather than
creating a Gitea repo and replacing it, which would leave an orphan. Existing projects get a
separate attach path.

Implemented request shape: `external_kb: {repo_url, branch, token, forge?}`. `forge` is inferred
for `github.com` and is required as `github` for GitHub Enterprise. Existing repo-less projects
attach through `POST /api/projects/{project_id}/knowledge/repository`. The attach route refuses
to replace an existing `knowledge`-role repository: automatic content/history migration remains
out of scope under §8.2.

## 5. Implementation order

1. `KbRepoRef` + widen `resolve_kb_repo`; all consumers keep working against Gitea. Inert.
2. GitHub client (`list_tree`, `change_files`, `tarball`, `branch_head`) on `forge.py`
   primitives, with tests against a mock transport — the pattern `test_forge_adapter.py`
   already uses.
3. Client selection at the `kb_materialize` and `kb_reindex` call sites, keyed on forge.
4. Credential read/write on the native `kb` datasource row.
5. `create_project` external-KB option + an attach path for existing projects.
6. Live gate (§7).

Steps 1–2 land without behaviour change; step 3 is the first live seam.

## 6. Risks

| risk | note |
|---|---|
| **GitHub rate limit** (5,000 req/hr authenticated) | A reindex is ~3 calls (branch, tree, tarball); a write is 1–2. Fine normally; a loop agent writing continuously plus frequent sweeps is the case to watch. Gitea is effectively unlimited today, so this ceiling is new. |
| **Latency** | Every note write becomes an external round-trip instead of in-cluster. |
| **No multi-file atomicity** | Acceptable while writes are one note per call — see §4.1. |
| **Token blast radius** | A contents-write token on private org repos. Fine-grained and vault-scoped only. |
| **Availability** | GitHub outage stops KB writes; Gitea outage does not. Writes should fail non-fatally, as the materialize path already does (`status: failed`, log-and-continue). |
| **Rebase/force-push on the vault** | A human force-pushing the vault repo invalidates the reindex watermark. Same exposure as Gitea today, but more likely with humans editing directly. |

## 7. Acceptance criteria

1. A project can be created with an external GitHub KB repo and no Gitea knowledge repo exists.
2. `kb_write` from an agent commits a note to the GitHub repo, at `knowledge/<slug>.md`.
3. A reindex of that project indexes the notes under `kb_id = project_id`, with `path`,
   `search_doc` and link edges populated (the §12 fixes must hold on this path too).
4. A history vault attached as an ordinary external `kb` connector is searchable and read-only.
5. Agent `kb_search` finds notes from both vaults; the native one stays writable.
6. Existing Gitea-backed projects are unaffected — verified by reindexing one.
7. The PAT is encrypted at rest and never appears in `project_repositories`, a workspace, or
   an API response.

## 8. Open questions

1. **Where does the *history* vault's read credential live?** As an ordinary `kb` connector it
   already has encrypted credentials — so probably a second PAT, or the same one. Decide
   whether one token covers both repos.
2. **Migration for existing Gitea-backed projects** — out of scope for v1, but the SRW project
   is being created fresh so this does not block it.
3. **Multi-tenancy.** Per-project external repos mean a PAT per tenant. v1 is deliberately
   opt-in and single-org; going wider needs a credential story per tenant.
