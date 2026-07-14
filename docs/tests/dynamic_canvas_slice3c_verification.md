# Dynamic Canvas Slice 3C — gateway database isolation verification

**Status:** Implemented, default-off, and repository-verified on 2026-07-14.
This checkpoint closes the gateway credential/configuration launch blocker. It
does not enable the viewer or claim production-browser/edge acceptance.

**Feature:** `docs/features/dynamic_canvas.md`

## What this checkpoint proves

- the isolated-origin gateway cannot inherit `DATABASE_URL`, `POSTGRES_*`, or
  the application database owner when its dedicated configuration is absent;
- enabled startup accepts only a small `CANVAS_VIEWER_POSTGRES_*` pool,
  requires the authenticated session role to remain the active role, and
  verifies its effective—not merely direct—privileges before schema checks or
  listener startup;
- the role has exactly the columns required by gateway bootstrap, exchange,
  session authentication, workspace-target revalidation, and revocation;
- extra token/session columns, extra writes on allowlisted tables, all access
  to unrelated public relations or sequences, role membership, elevated
  attributes, a switched current role, and database/public-schema creation
  make startup fail closed;
- the gateway pod receives a dedicated allowlisted ConfigMap, two keys from a
  separate database credential Secret, and the workspace SSH key—never the
  shared application ConfigMap or application DB credential; and
- chart-owned development Postgres can reconcile the fixed restricted role
  without placing its administrator password in gateway argv, logs, or pod
  environment. Production and external Postgres require an operator-managed
  role and dedicated credentials supplied through either an existing Secret or
  the feature-gated Vault/ESO path.

## Delivered surfaces

| Surface | Location |
|---|---|
| Strict database factory and effective-privilege attestation | `orchestrator/services/canvas_viewer_database.py` |
| Lazy startup and minimal thread workspace-binding query | `orchestrator/canvas_gateway.py` |
| Explicit origin-session reads | `orchestrator/services/canvas_viewer_sessions.py` |
| Reusable role/grant reconciler | `helm/files/canvas-viewer-role.sql` |
| Secret-safe production operator workflow | `scripts/provision-canvas-gateway-database.sh`, `docs/operations/dynamic_canvas_gateway_database.md` |
| Dedicated ConfigMap, credential Secret, bounded role Job and NetworkPolicy | `helm/templates/canvas-gateway/` |
| Profiled Compose role reconciler and dedicated gateway variables | `docker-compose.yaml`, `docker-compose.local.yaml` |
| Production/development value contract | `helm/values.yaml`, `helm/values.schema.json` |
| Fast and real-PostgreSQL regressions | `tests/test_canvas_gateway_database.py`, `tests/test_canvas_gateway_role_sql.py`, `tests/test_canvas_slice3_infra.py`, `tests/test_canvas_viewer_postgres_integration.py` |

## Credential and grant contract

Production viewers must select exactly one operator-managed credential source:
`canvas.livePreview.viewer.database.credentials.existingSecret` names a Secret
separate from the main SRW application Secret, while `credentials.vaultPath`
asks the chart to map only `username` and `password` into a dedicated Secret
through ESO. That ExternalSecret shares the viewer lifecycle and is absent while
the viewer is disabled. Chart-created credentials and automatic role
provisioning are rejected under the production profile.
The operator workflow now has an explicit read-only preflight and opt-in
database-only or database-plus-Secret apply path. It requires explicit
database and Kubernetes targets, reads the restricted password from a private
file, passes Secret content through private files and a pipe, and prints no
credential values. Helm still receives no administrator credential.

Development with chart-owned Postgres may instead set
`credentials.create=true` and `provisionRole=true`. The generated password is
purpose-specific. Within the provisioning SQL, it appears only in
password-aware `CREATE ROLE`/`ALTER ROLE` statements, never a wrapper query. A
regular revision-scoped Job—not a
post-install hook—waits for migration `0062`, then applies the packaged psql
contract. This avoids a
`helm --wait` deadlock with the gateway Deployment. The Job has a deadline,
bounded retries, no service-account token, no ingress, and egress only to DNS
and bundled Postgres.

The resulting role receives:

- database `CONNECT` and `USAGE` on `public`;
- column-scoped reads on `users`, `threads`, `srw_sessions`, `canvases`, and the
  three Canvas viewer tables;
- column-scoped insert only on `canvas_origin_sessions`; and
- column-scoped updates only for bootstrap challenge/consumption, attachment
  linkage/last-seen, and origin-session renewal/revocation.

It receives no application-schema `CREATE`, DELETE, TRUNCATE, TRIGGER,
sequence, unrelated-relation, credential/token-column, or authoritative-state
mutation privilege. The reconciler refuses elevated/member/owner roles and an
installation where PUBLIC still grants `CREATE` on schema `public` or on the
application database. It now removes stale direct database grants before
restoring only `CONNECT`; PostgreSQL's default PUBLIC `TEMPORARY` capability
remains outside the application-schema contract.

## Verification

- **Focused repository suite:** 299 Canvas, gateway, proxy, SSH, tool,
  infrastructure, integration-wrapper, and anti-framing tests passed; two
  opt-in database tests were skipped in that ordinary run.
- **Fresh PostgreSQL 15:** all application migrations replayed through `0062`,
  then both real-PostgreSQL tests passed. The new test applied the packaged
  role script, ran the production startup attestation as that login, rejected
  a broad authenticated session switched into the narrow role, allowed a
  required workspace-metadata read, and received `InsufficientPrivilege` for
  a BFF access-token read, user API-key read, Canvas mutation, viewer-session
  delete, sequence use, and public table creation.
- **Role script:** a separate disposable PostgreSQL 15 smoke test applied the
  reconciler twice, confirmed required column access remained true while
  unrelated reads, extra thread columns, DELETE, and schema CREATE remained
  false, and verified under `log_statement=all` that its probe credential did
  not appear in PostgreSQL logs.
- **Deployment:** default and enabled Helm renders, both required Helm lint
  overlays, dedicated-secret production validation, generated-role development
  validation, and both Compose documents passed structural checks. The default
  render still contains no gateway resources.
- **Static:** focused Ruff lint/format, JSON/YAML parsing, and `git diff --check`
  passed.

The production operator workflow and direct-Secret helper are repository
implemented. Both its database-only and database-plus-Secret paths were
exercised against the migrated local k3d PostgreSQL instance using a disposable
role and dedicated two-key Secret; verification passed and both objects were
removed afterward. It has not been run against an external production database
or cluster. Current local k3d remains deliberately dark and contains neither
the restricted role nor its Secret; its development-only chart-managed path is
covered separately by Helm rendering tests.

## Remaining launch boundary

The viewer remains dark. User-facing Slice 3 still requires a raw-path-
preserving wildcard edge, propagated private PSL boundary, edge rate limits,
black-box trusted-parent/PWA validation, and the Chromium/Firefox/WebKit plus
Safari/iOS authentication/security matrix. The local production-bundle
Playwright harness, explicit unsupported/storage UX, authenticated wrapper
pop-out, and conditional trusted reset-origin action have since landed. They do
not replace a hosted Python-gateway/TLS/CHIPS/PSL run or device/PWA acceptance;
multi-port, SSE, WebSocket/HMR, and Shared Browser remain later slices.
