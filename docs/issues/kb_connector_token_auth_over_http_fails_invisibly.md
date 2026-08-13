# External KB connector: token auth over http fails, and the failure is invisible

**Status:** **Open, observed live on dev 2026-08-13.** Worked around by pointing the connector at
the TLS ingress instead of the in-cluster service; needs `kbGitAllowedHosts` to carry the public
hostname. No code fix.
**Severity:** **Medium** — the rejection itself is correct and deliberate. The problem is that it
is thrown where nothing records it, so the connector shows a **stale, misleading** error forever
while silently retrying and failing every tick.

## What happens

`RemoteKnowledgeGitSource.__init__` validates the auth configuration up front
(`orchestrator/services/kb_git_source.py:1028` → `validate_git_auth_configuration`, `:720`):

```python
raise ValueError("Token/password authentication requires an HTTPS URL")
```

That is a good rule — a token must not be sent in cleartext. But it fires during **construction**,
inside `kb_source_from_datasource` (`kb_datasources.py:67`), which `reindex_kb_datasource` calls
while building its arguments (`:106`). The exception therefore escapes **before** any code path
that writes `kb_index_watermark`.

Consequences:

1. **`kb_index_watermark` is never updated.** Its `status`, `last_error` and `last_attempt_at`
   keep whatever they held before — potentially from a completely different, long-resolved cause.
2. **The cockpit shows that stale error.** In our case the connector row displayed
   `Git HEAD read failed: fatal: could not read Username …` for over an hour. That error was real
   but from a *different* failure 46 seconds after creation; the actual recurring failure was the
   HTTPS rule, and nothing surfaced it.
3. **The only truthful signal is the sweep's own log line**, once per tick:
   `kb_sweep: reindex failed for datasource <id>` (`kb_reindex.py:1060`) with the traceback in
   the `exc` field.

So a connector can be retried every 15 minutes indefinitely, fail every time, and present a
completely unrelated error to the operator.

## A second, smaller trap

`create_datasource` schedules a reindex immediately. A connector created without credentials —
for example because the operator intends to attach them in a second step — fires a reindex within
**0.2 s**, resolves auth to `public`, and records a spurious `could not read Username` failure.
That stale failure is then what the operator sees, even after credentials are attached, because
of the bug above.

## Why this bit us specifically

The in-cluster Gitea service (`srw-gitea`) exposes **only port 3000, over http**. There is no
internal TLS endpoint. So an external KB connector pointed at a **private** in-cluster repo is
impossible: token auth requires HTTPS, and HTTPS only exists on the public ingress
(`git.srw.works`). The connector must therefore hairpin out through the ingress and back, or the
service must gain a TLS port.

Verified the ingress path works, from the orchestrator pod:

```
GET https://git.srw.works/api/v1/repos/srw/better-resavio-history   -> 200
git ls-remote https://git.srw.works/srw/better-resavio-history.git  -> 5044b83c… refs/heads/main
```

(askpass-supplied token, `GIT_TERMINAL_PROMPT=0`.)

## Workaround applied

- Connector `connection_url` changed from `http://srw-gitea:3000/srw/better-resavio-history.git`
  to `https://git.srw.works/srw/better-resavio-history.git`.
- `orchestrator.kbGitAllowedHosts` must list `git.srw.works` (default port 443, so a bare
  hostname entry matches). Set to `"git.srw.works,srw-gitea:3000"` — the in-cluster entry is kept
  because it remains valid for a *public*, credential-less repo.

## Suggested fix

1. **Record construction failures on the watermark.** Wrap the `kb_source_from_datasource` call
   in `reindex_kb_datasource` so a `ValueError` (or any construction error) lands in
   `kb_index_watermark.status='failed'` + `last_error`, exactly as a HEAD-read failure does.
   Without this, any validation error is invisible to every operator surface.
2. **Validate at write time, not only at sweep time.** `POST/PUT /api/datasources` should run
   `validate_git_auth_configuration` against the effective (url, credentials) pair and reject with
   a 400 that names the problem. An operator should not be able to save a connector that can
   never work.
3. **Do not fire a reindex on a credential-less create**, or record its failure as "not yet
   configured" rather than a hard error, so a two-step create→attach flow does not leave a
   misleading corpse.
4. **Consider a TLS port on the in-cluster Gitea service** so private internal KB repos do not
   have to hairpin through the public ingress.

## Related

- [`kb_sweep_indexes_archived_projects_and_starves_connectors`](kb_sweep_indexes_archived_projects_and_starves_connectors.md)
  — found at the same time; it delayed this connector's *first* attempt by 27 minutes but was not
  the blocker.
- `docs/superpowers/specs/2026-08-13-better-resavio-restart-design.md` §3a — the allowlist and the
  external-KB topology this was found while building.
