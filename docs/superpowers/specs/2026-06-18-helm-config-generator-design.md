# SRW Helm Config Generator — Design Spec

**Status:** Approved design (2026-06-18), pending implementation plan.
**Related:**
- `docs/website.md` — marketing-site≠webapp split, byte-budget ethos, the source-available licensing framing this generator inherits.
- `docs/issues/sales_page_landing_audit.md` — the live sales page and its currently-broken self-host CTA (a precondition this feature depends on; see §12).
- `docs/superpowers/plans/2026-06-15-phase1-chart-correctness-gates.md` — the CI render/`kubeconform`/schema gates this generator's drift-gate rides on.
- `helm/values.example.yaml` — the curated customer value surface the form mirrors.
- `helm/README.md` §Secret schema — the canonical secret modes; `secretKeyRef` keys across `helm/templates/` are the machine-readable key set the skeleton is checked against.
- `HomeLab/deployments_managed/srw-sales-page/` (current ConfigMap hosting) + `docker/Dockerfile.cockpit` / `build-cockpit` in `.github/workflows/develop.yml` — the CI image pattern §13 normalizes the sales page onto.

**Goal:** Replace "read a 765-line install guide and hand-edit a 217-line values
file" with a static, client-side **config generator**: the operator picks a
profile, fills a short form, and gets three ready-to-use artifacts — a
`values.yaml` overlay, a Secret skeleton with the *correct key set* for that
profile, and an `install.sh`. The generator's output is **provably correct
against the actual chart**, enforced by a CI drift-gate, so it cannot rot
silently as the chart evolves.

---

## 1. Why (the decision, recap)

Self-hosting SRW today means reconciling three documents by hand: the
`values.example.yaml` template (217 lines, referencing a 1345-line
`values.yaml`), the install guide (765 lines), and the Secret schema in the
chart README. The value surface has real branching — every backing service has a
"bundled vs bring-your-own" toggle, the session router has two mutually-exclusive
secret layouts, Neo4j has an edition/license gate, and several values must be
hand-generated (`openssl rand -hex 32`). The nastiest footgun, called out in the
install guide itself, is **Secret keys that the chart's pods reference
unconditionally** — omit one and the pod crash-loops with an opaque error.

A form that asks the right questions, hides irrelevant branches, generates the
random secrets, and emits the *complete* key set for the chosen profile removes
exactly this friction. It is the natural destination for the sales page's
"Self-host with Helm" CTA and absorbs the license attestation
(`license.acceptTerms`) as step one — the same pattern as Neo4j Enterprise.

**Strategic fit (deliberately small build):** this is a conversion tool for the
self-host → commercial-license funnel, not new product surface. Total footprint:
two new static files + a trivial Dockerfile, one CI build job plus the drift-gate
job, a one-line HomeLab manifest swap (ConfigMap → image), and a doc move. It
ships under the feature-freeze because it drives revenue and cannot become a
maintenance burden (see §9, the drift-gate).

## 2. Goals / Non-goals

**Goals**
- One-page, client-side generator producing `values.yaml` + Secret skeleton + `install.sh`.
- Output guaranteed to (a) render under `helm template`, (b) pass `kubeconform` and `values.schema.json`, and (c) contain every non-optional Secret key the rendered manifests reference.
- No backend, no analytics, no data leaves the browser.
- Lives alongside the sales page in a new `website/` directory, deployed by the existing `srw-sales-page` nginx/ConfigMap mechanism.

**Non-goals (YAGNI — see §11)**
- Automating cluster/DNS/TLS bootstrap (k3s, cert setup). Stays in the guide; the generator links to it.
- Exposing the full 1345-key chart surface. Curated subset only.
- Replacing the install guide. The generator *complements* it.
- A JS framework or client-side bundler. Vanilla source COPYed into an nginx image; the only "build" is the trivial Docker image (§13).

## 3. Placement & directory reorg

A **separate static page**, not a change to `index.html`. The sales page stays a
≤14kb, no-JS, render-without-JS artifact per `docs/website.md`; a config tool
legitimately needs JS and would blow that budget.

Introduce a `website/` directory to keep the marketing surface organized as one
deployable unit:

```
website/
  index.html      # MOVED from repo root (the existing sales page, unchanged content)
  configure.html  # NEW — the generator page (form + output UI)
  generator.js    # NEW — the "resolver": pure, DOM-free generation logic (ES module)
  og-image.png    # NEW — 1200×630 social card (resolves the open landing-audit item)
docker/
  Dockerfile.website  # NEW — nginx:alpine + COPY website/ + nginx config
```

- **Source of truth:** these files in-repo (mirrors how root `index.html` is the source today). They are baked into a container image at CI time (§13) — no more hand-inlining into a ConfigMap.
- **Served at:** `/` → `index.html`, `/configure` → the generator. Because we now own the nginx config in the image (§13), a clean `/configure` URL via `try_files` and sensible cache headers are trivial. `configure.html` loads `generator.js` via `<script type="module" src="generator.js">`.
- **Tech:** vanilla JS, no framework, no JS bundler — the only "build" is a Docker `COPY`. `generator.js` is an ES module so the *same* code is importable by the CI drift-gate (Node ESM) — one source of generation truth.
- **`<noscript>` fallback:** a short message linking to the manual install guide, since the tool requires JS by design.
- **Soft budget:** ~50kb gzipped for the generator (generous vs the sales page, still disciplined — zero dependencies).
- **`og:image`:** owning the image lets us serve a real `1200×630` PNG file (resolving the open landing-audit item — LinkedIn/Twitter reject data-URI cards). Drop `website/og-image.png` and reference it absolutely.

## 4. The pure generator module (`generator.js`)

The entire generation logic is one pure function plus helpers, no DOM access:

```
generate(profile, inputs) -> { valuesYaml: string, secretSkeleton: string, installScript: string }
```

`configure.html` wires the form to it; the CI drift-gate imports the *exact same
function*. There is no second copy of the logic to drift. YAML is assembled by
hand-rolled templating from a per-profile base overlay plus the user's deltas
(we control the exact shape; a YAML library is not worth the bytes). The emitted
`values.yaml` is a curated, human-readable overlay in the spirit of
`values.example.yaml` — it does **not** need to be byte-identical to any
`helm/ci/*.yaml`; correctness is proven by rendering (§9), not by structural
diffing.

## 5. Profiles + curated deltas

Three customer-facing profiles. Each corresponds to a chart shape the Phase-1 CI
matrix already validates, and each is independently exercised by the generator's
own drift-gate (§9):

| Profile | Chart shape | For |
|---|---|---|
| **Evaluation** | single node, `secrets.create` (chart-created secrets), minimal footprint | kick the tires on one box |
| **Production** | external managed services (`secrets.existingSecret`), bundled components off | real installs |
| **Production + same-cluster VMs** | Production plus `vmController.enabled` (KubeVirt) | agents as VMs in-cluster |

(The home/`test` ESO profile from the CI matrix stays internal — it is the
maintainer's homelab shape, not a customer config.)

Selecting a profile reveals a curated **~20-field delta** drawn from
`values.example.yaml`:

- `license.acceptTerms` (gating checkbox, step one)
- `global.domain`, optional hostname overrides
- `ingress.className` (nginx/traefik), `ingress.tls.issuerName`
- Secrets mode (per profile default; overridable)
- Per-service bundled-vs-external toggles + external URLs: Postgres, vector DB, MongoDB, Keycloak/OIDC, Gitea, cloud storage (Nextcloud/OpenCloud/WebDAV)
- `databases.neo4j` enabled + edition (+ license acceptance when enterprise)
- `s3.*` snapshot target (endpoint, bucket, region, retention)
- `agent.pool` sizing (min/max/reserved), `logLevel`, `logFormat`
- VMs profile only: `vmController.transport`, `namespace`, `vmStorageClass`, `vmDiskSize`, `vmSshPublicKey`

**Progressive disclosure:** a service's external-URL fields appear only when that
service is switched to "bring your own"; VM fields appear only on the VMs
profile; Neo4j license appears only when edition is enterprise.

## 6. Output — three artifacts

Rendered into a tabbed output panel, each with copy + download buttons:

1. **`values.yaml`** — the overlay for `helm install -f`.
2. **Secret skeleton** — in the profile's secrets mode (a chart-created
   `secrets.values` block for Evaluation; a `kubectl create secret` / manifest
   for the pre-existing-Secret modes). Contains the **complete** key set for the
   profile (§7) so no pod can crash-loop on a missing key.
3. **`install.sh`** — ties it together: create namespace, apply the Secret,
   `helm install … -f values.yaml`. Cluster/DNS/TLS bootstrap is **not** here —
   it links to the install guide for the parts that need a human at a terminal.

## 7. Secret handling

- **Random secrets** (`APP_ENCRYPTION_KEY`, the session-router JWT secret) are
  generated client-side via the Web Crypto API and filled in automatically — but
  **only in the operator-owned-Secret modes** (Production / existing-Secret). In
  the **Evaluation / chart-created mode** the chart itself auto-generates
  `APP_ENCRYPTION_KEY` when absent (per `templates/secret.yaml`), so the skeleton
  leaves it out rather than fighting the chart. The session-router JWT follows the
  router's secret layout: either embedded in `install.sh` as
  `--set-string sessionRouter.jwtSecret="$(openssl rand -hex 32)"` or generated
  client-side and written into the Secret, depending on the chosen layout.
- **User-supplied secrets** (DB passwords, LLM API keys, OIDC client secret) are
  emitted as `CHANGE_ME` placeholders by default.
- **Optional local-fill** — a clearly-labeled toggle ("100% in your browser,
  never sent anywhere — view source to confirm") lets a trusting operator type
  values in and get a ready-to-apply Secret. Default off.
- The emitted key **set** is always complete for the chosen profile and toggles,
  regardless of fill mode — that completeness is what §9 enforces.

## 8. Client-side validation

The Generate button stays disabled until the form is valid. Two layers:

- **Structural** — mirror `helm/values.schema.json`'s 12 type/enum rules with a
  tiny hand-rolled validator (the schema is small and deliberately partial). CI
  asserts the mirrored rules still match the canonical schema (§9).
- **Cross-field** — the semantics that live in Helm template `required`/`fail`
  guards, which the schema intentionally omits: license accepted; an external
  service implies its URL is required; Neo4j enterprise implies license
  acceptance; NATS transport mutual-exclusion for the VMs profile.

## 9. The drift-gate (keystone)

A new CI job, **`generator-test`**, makes the generator's output provably correct
against the chart and unable to drift. It runs a fixed set of input cases — for
each of the three profiles, the profile defaults **plus at least one variant that
flips a bundled→external toggle** (so the progressive-disclosure branches are
exercised, not just the happy path). For each `(profile, inputs)` case it:

1. Imports `generate()` from `website/generator.js` (Node ESM) and produces `valuesYaml` + `secretSkeleton`.
2. Writes `valuesYaml` to a temp file and runs `helm template srw helm/ -f tmp.yaml` → pipes to `kubeconform` (same flags/version as the Phase-1 `chart-test` job). Asserts it **renders and validates**. Because Helm validates merged values against `values.schema.json` during `template`, this also exercises the structural schema.
3. Extracts every `secretKeyRef` from the rendered manifests, **excluding refs marked `optional: true`**, and asserts each referenced key is present in `secretSkeleton`. This is the crash-loop footgun killer, and it reads the *real* rendered templates, so it tracks the chart automatically.
4. Asserts the in-page structural validator rules equal `helm/values.schema.json` (so §8 layer 1 can't fall out of sync).

**Gating policy** (mirrors Phase-1's "render/validate are hard on both
branches"):
- `develop.yml`: `generator-test` runs when `website/` **or** `helm/` changed (uses the existing `changes` job's booleans; a `website` filter is added). Hard gate.
- `main.yml`: always runs, hard gate.
- It does **not** gate the chart/image publish jobs — the generator is not part of the chart artifact; it is its own correctness surface.

Requires Node + Helm 3.17 + `kubeconform` v0.6.7 in the job (Helm/kubeconform
install steps are copied from the Phase-1 plan).

## 10. UX / layout

```
┌─ SRW config generator ────────────────────────────┐
│ ☐ I hold a valid SRW commercial license  [required]│
│ Profile:  (•) Evaluation  ( ) Production  ( ) +VMs │
│ ─────────────────────────────────────────────────  │
│ Base domain      [ srw.example.com            ]    │
│ Ingress class    [ traefik ▾ ]   TLS issuer […]    │
│ Postgres         (•) bundled ( ) external →[url]   │
│ … curated deltas, progressive disclosure …         │
│ ☐ Fill secret values in my browser (advanced)      │
│              [ Generate ▸ ]                         │
├─ Output (tabs) ───────────────────────────────────┤
│ [values.yaml] [srw-secrets] [install.sh]           │
│   <monospace, syntax-tinted>          [Copy][⬇]    │
└────────────────────────────────────────────────────┘
```

Visual language reuses the sales page's CSS variables (palette, type scale,
corner radius) so the two pages feel like one site. No web fonts, no raster
images.

## 11. Out of scope (YAGNI)

No backend; no analytics/telemetry; no cluster/DNS/TLS automation; no full
1345-key surface; no install-guide replacement; no JS framework or client-side
bundler; no deploy automation (manual redeploy, §13); no multi-language. The
generator does one thing: turn a short form into a correct, copy-pasteable
install bundle.

## 12. Dependencies & preconditions (revenue reality)

The generator sits at the end of the self-host → license funnel. Per
`sales_page_landing_audit.md`, that funnel is **currently broken**: the
"Self-host with Helm" CTA and repo links 404, and the chart's public
pull-ability for customers is not confirmed. The generator only converts if it
emits a `helm install` against a chart customers can actually pull, reached from
a working CTA. **Therefore this should ship with (or just after) wiring up the
self-host CTA and confirming a pullable chart** — otherwise it is a polished dead
end. Implementation plan must list these as blocking prerequisites, not
afterthoughts.

## 13. Packaging & deployment (image-based)

Today the sales page is the lone SRW surface hand-inlined into a ConfigMap; every
other component is a CI-built **public** GHCR image. This normalizes it. Decided
deploy model (deliberately low-automation — the site changes a few times a year):
**CI builds an image and publishes `:latest`; deploy is a manual redeploy.**

- **Image** — `docker/Dockerfile.website`: `FROM nginx:alpine`, `COPY website/`
  into the html root, and a small server block (clean `/configure` via
  `try_files`, static cache headers, `listen 80`). It is `Dockerfile.cockpit`
  minus the Node build stage — there is nothing to compile.

- **CI build job (`build-website`)** in `develop.yml`, modeled on `build-cockpit`:
  - Change-gated on a new `changes.outputs.website` filter (diff over `website/`
    and `docker/Dockerfile.website`).
  - **Depends on the `generator-test` drift-gate (§9)** — a broken generator is
    never published.
  - On PR: `push: false` (builds the image to validate the Dockerfile only). On
    push to `develop`: `push: true`, tagging
    `ghcr.io/knaeckebrothero/superhuman-remote-worker-website:latest` (a
    `sha-XXXX` tag is also pushed for traceability; HomeLab references `:latest`).
  - **One-time:** flip the new GHCR package to **public** (first push defaults to
    private) so the cluster pulls with no secret — same as the other SRW images.
  - No `deploy-experimental` change, no tag auto-bump — CI's job ends at "image
    published."

- **HomeLab manifest swap** (`HomeLab/deployments_managed/srw-sales-page/10-deployment.yaml`):
  - **Delete** the `srw-sales-page-content` ConfigMap and the single-file
    `subPath` mount.
  - Point the Deployment at
    `image: ghcr.io/knaeckebrothero/superhuman-remote-worker-website:latest` with
    `imagePullPolicy: Always`. **Keep** the Namespace, Service (port 80), and
    Ingress (`superhuman-remote-worker.com`, Traefik, cert-manager DNS-01)
    unchanged. Stays its own Fleet bundle → independent failure domain preserved.

- **Rollout (manual, by design):** after CI publishes a new `:latest`, redeploy
  on demand with `kubectl rollout restart deploy/srw-sales-page -n srw-sales-page`
  (`Always` pull refetches the image). That is the entire deploy step — no
  ConfigMap re-indenting, no cross-repo sync, no `kubectl` for routine copy work
  beyond this one command when you choose to publish.

- **`index.html` move + doc references:** `git mv index.html website/index.html`;
  add the new files. Update `docs/website.md` (the `gzip -c index.html` path) and
  `docs/issues/sales_page_landing_audit.md` (its "repo root" + "inline into
  ConfigMap" notes — the ConfigMap flow is retired). Update the
  `project_sales_page_deploy` memory to describe the image build + manual-restart
  flow.

## 14. Testing strategy

- **`generator-test` CI job** (§9) — the authoritative correctness gate across all three profiles.
- **Local dev:** a `node` script invoking `generate()` per profile and piping through the §9 `ktest`-style helper, runnable before pushing (same tooling as the Phase-1 plan's prerequisites).
- **Manual smoke:** open `website/configure.html` in a browser, generate each profile, confirm copy/download and the local-fill toggle; verify `<noscript>` shows the guide link.
- **Image build:** `build-website` builds `docker/Dockerfile.website` on every PR (`push: false`), so a broken Dockerfile/image fails before merge; develop pushes publish `:latest`. A local `docker build -f docker/Dockerfile.website .` + `docker run` smoke-serves all routes (`/`, `/configure`, `/og-image.png`).
- **Byte budget:** `gzip -c website/configure.html | wc -c` (+ `generator.js`) checked against the ~50kb soft budget.

## 15. Open risks

- **Curated-subset coverage:** the ~20 fields cover the common cases; exotic
  installs still need the full `values.yaml`. Acceptable — the output is a
  starting overlay, and `install.sh` links to the full reference. The page should
  say so plainly.
- **Profile↔chart-shape correspondence** is conceptual, not file-pinned; §9
  proves each profile renders, which is the property that actually matters.
- **Clean `/configure` URL** needs custom nginx config; deferred as polish (ship
  `/configure.html`).
