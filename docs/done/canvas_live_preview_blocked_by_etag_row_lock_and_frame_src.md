# The Canvas live preview never reached the app — three unrelated defects stacked in the exact order a browser meets them

**Status:** **SHIPPED + PUSHED 2026-08-05 · on develop in `51db3570`, `27235e71`, `ecfcf9da` · deployed to dev and user-confirmed working 2026-08-06 (`sha-feba62c`).** Each fix was written test-first against a reproduction of the live failure, not against a guess.

**Filed under `done/` because the WORK shipped.** Two things are deliberately not fixed here and are called out in §Not covered.

**Found:** 2026-08-05, user-reported in three rounds against session `4ad107ad` on dev, each round uncovering the next layer: a 412 request storm ("Everything is blinking all the time and I'm getting errors every second", 595 console errors), then `{"detail":{"code":"canvas_gateway_error"}}` rendered inside the frame, then a fully drawn app shell with a blank content area.

**Severity:** **High.** The first defect made the Canvas effectively read-only through the CDN *and* pinned a browser tab at roughly four failed request cycles per second. Behind it, the isolated-origin live preview had never worked at all under the production-shaped database role — the feature was dark and the 412 storm was hiding it.

**Component:** `orchestrator/services/canvas.py` + `orchestrator/routers/canvases.py` (state preconditions) · `cockpit/src/app/views/canvas/canvas-viewer.controller.ts` (retry) · `orchestrator/services/canvas_viewer_sessions.py` (gateway-only reads) · `orchestrator/services/canvas_proxy_policy.py` (injected response CSP)

**Related:** `docs/features/dynamic_canvas.md` — the state-ETag contract, the gateway database-isolation checkpoint, and the proxy CSP section all carry the resulting rules · `docs/tests/dynamic_canvas_slice3c_verification.md` (§Correction) · `docs/operations/dynamic_canvas_gateway_database.md`

## Why one report became three

Each defect sat behind the previous one, so fixing each revealed the next:

1. the browser could never keep an attachment (**412**), so it never navigated the isolated frame;
2. once it did, the gateway's first database statement was denied (**500**), so no app document was served;
3. once one was, the app's shell rendered and its nested content frame was refused (**CSP**).

None of them could have been found by reading the code the layer above complained about. Each needed the live cluster.

## Defect 1 — a compressing CDN weakens the ETag, so every write refused it

`POST …/canvases/main/view-attachments` returned `412` forever. The Canvas state ETag is strong (`"canvas:<revision>:<sha256>"`), and Cloudflare compresses the JSON and rewrites it to the weak form `W/"canvas:…"`. Verified live rather than inferred: the identical request returns `etag: "canvas:1:00a1091b…"` from traefik/uvicorn and `etag: W/"canvas:1:00a1091b…"` with `content-encoding: zstd` through `server: cloudflare`.

The conditional `GET` stripped the `W/` marker (`_if_none_match_matches`), so reads revalidated to `304` normally. Every mutation compared bytes, so the only tag a browser could ever echo failed **every** write: view-attachments, office-session, `DELETE /main` (`412`), refresh and reset-origin (`400`). The content `PUT` was immune only because it rides the custom `X-Canvas-Content-ETag` header, which the CDN does not touch.

**The storm.** The Cockpit's 412 handler reconciled Canvas state; the reconciled state was a new object; the pane effect re-ran and re-created the attachment with the same never-matching tag. A permanent failure became an unbounded loop.

**Fix (`51db3570`).** `strong_state_precondition()` in `services/canvas.py` drops a leading `W/` at all seven precondition sites, leaving full-digest comparison intact and still refusing `*` — weakening marks the transfer encoding, not the state. Separately, `CanvasViewerController` records the desired state a create failed for and does not re-attempt until the state actually changes or the user retries, so no permanent failure can storm again.

## Defect 2 — the gateway may not lock the rows it reads

Every bootstrap into the isolated origin returned `500 canvas_gateway_error`. The gateway pod's own traceback named it exactly:

```
canvas_gateway.py _bootstrap → begin_bootstrap → _canvas_record
asyncpg.exceptions.InsufficientPrivilegeError: permission denied for table canvases
```

The public gateway authenticates as `srw_canvas_gateway`, which by design holds column-level `SELECT` and nothing else on `users`, `threads`, `srw_sessions`, `canvases`. **PostgreSQL requires `UPDATE` on at least one column for any row-locking clause**, so the `FOR SHARE` reads in `begin_bootstrap` and `exchange_bootstrap` were never a stricter check — they were a guaranteed denial. Reproduced as that role against the dev database: the plain select returns a row, the identical select `FOR SHARE` returns `permission denied`; same for `threads` and `srw_sessions`, while the viewer-owned tables lock fine because their grants carry `UPDATE` columns.

**Fix (`27235e71`).** Those two gateway-only methods read the four authoritative tables unlocked, exactly as `authenticate` already did, and keep locking the bootstrap, attachment, and origin-session rows they mutate. No freshness is lost: `authenticate` re-derives parent-session liveness, approval, thread ownership, and canvas identity — lock-free — on every proxied exchange and every revalidation tick, and revokes the session on mismatch. Granting the gateway `UPDATE` on `canvases` was the other option; it would fail the startup attestation's no-extra-privileges check and hand an internet-facing process a lever to block Canvas saves.

**Why no test caught it.** Every canvas test ran as the migration owner, which can lock anything, and the startup attestation *passes* — the role does hold every column privilege its contract names. The contract was column-complete and statement-incomplete.

## Defect 3 — the app may not frame its own pages

The shared app renders each mockup into a nested `<iframe>` (`frame.src = 'pages/…html'`). The gateway strips the app's CSP and injects its own, which carried `frame-src 'none'`, so the shell, nav, and chrome drew and the content frame was refused. The iframe still fires its `load` event, so the app's own "loading" overlay cleared and nothing on the page explained the emptiness.

Reproduced in Chromium against the exact policy the code emits:

```
Framing 'http://…/pages/inner.html' violates the following Content Security
Policy directive: "frame-src 'none'". The request has been blocked.
```

Relaxing `frame-src` alone is **not** enough, and only the browser said so: once the app frames itself, the app origin sits in the nested document's ancestor chain, and `frame-ancestors` named only the Cockpit origins, so the child is refused right after the parent's directive admits the load.

**Fix (`ecfcf9da`).** `frame-src 'self' blob:` and `frame-ancestors 'self' <cockpit origins>`, together. A nested same-origin document reaches nothing new — same gateway, same session, same injected policy, same sandbox flags, which nested contexts inherit. Anti-framing is unchanged because browsers check *every* ancestor.

## Verification

- **Live evidence first, in all three cases**: edge-vs-origin header comparison for the ETag; the gateway pod's traceback plus `SET ROLE srw_canvas_gateway` reproduction on the dev database for the lock; a Chromium repro serving the exact emitted policy for the CSP.
- **Tests written first and watched fail on the real symptom.** The DB one fails with the production error (`permission denied for table canvases`) and each of the three lock sites was re-locked individually to prove the test catches each.
- `tests/test_canvas_viewer_postgres_integration.py` now drives the gateway-only methods — bootstrap start, exchange, session reuse, authentication, origin revocation — through a pool authenticated **as** the restricted role. That is the only harness in the repo that can see a privilege defect; it runs in the `db-migrations` CI job, which triggers on `orchestrator/services/canvas*.py`.
- Suites at the time of each fix: 459→460 canvas pytest green, 1667 cockpit vitest green (211 in the canvas folder), ruff and tsc clean.
- **Chromium, under the shipped policy:** the shell's own subpage renders; a cross-origin canary frame is blocked with zero requests reaching the canary server; a hostile origin renders no app document, directly or chained through an app-origin frame.
- **User-confirmed on dev 2026-08-06** with the mock app rendering its mockups.

## Not covered here

- **A non-ASCII `If-Match` still raises `TypeError` in `secrets.compare_digest` → 500 instead of 412.** Pre-existing, authenticated-only, unrelated to the loop; deliberately left out rather than widening those commits.
- **Prod (`srw-prod-private`) was not checked.** If it sits behind the same Cloudflare edge it has defect 1, and it is pinned to an older version, so it would need a cut. Defects 2 and 3 apply wherever the viewer is enabled.
- **The Playwright conformance suite was not run locally** for defect 3 — it builds the production Cockpit bundle. Its fixture policy and assertion were updated to match the shipped contract; CI is the gate. A same-origin nested-frame probe in that harness would encode this regression at the browser level and does not exist yet.
