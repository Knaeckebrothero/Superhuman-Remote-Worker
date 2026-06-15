# Phase 1 — Chart Correctness Gates in CI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add static chart-correctness gates (render matrix + kubeconform schema validation + a `values.schema.json` input gate) to the CI pipeline so any chart regression is caught on every `helm/`-touching change, and a broken chart can never be published.

**Architecture:** Two new GitHub Actions jobs (`chart-test`, `chart-schema-negative`) added to both `develop.yml` and `main.yml`. `chart-test` is a scenario matrix that renders the chart under each meaningful values permutation and pipes the output through `kubeconform`. A new permissive `helm/values.schema.json` gives `helm` install-time type/enum validation; `chart-schema-negative` proves that schema actually rejects bad input. The existing `helm lint` steps move out of the `lint` job into `chart-test`. The dev/release chart-publish jobs gain a dependency on `chart-test` so a red gate blocks publication.

**Tech Stack:** Helm 3.17, [kubeconform](https://github.com/yannh/kubeconform) v0.6.7, the [datreeio/CRDs-catalog](https://github.com/datreeio/CRDs-catalog) for CRD schemas, JSON Schema draft-07, GitHub Actions.

---

## Background the executor needs

This chart is the single supported install path for an OSS release. Today CI only runs `helm lint` against two values files (`helm/ci/test-values.yaml`, `helm/ci/customer-external-values.yaml`). `helm lint` catches template *syntax* errors but does **not** prove the rendered manifests are valid Kubernetes, and there is no install-time validation of operator-supplied values. Phase 1 closes the static-analysis gap (Phase 2, a real install test on a throwaway cluster, is a separate plan).

**The two-tier pipeline (do not break this contract):**
- `develop.yml` — the soft/fast branch. Most checks are `continue-on-error: true` (advisory). A `changes` job emits per-component booleans; jobs gate on them so only impacted work runs. The dev chart (`srw-dev`) is published by the `deploy-experimental` job.
- `main.yml` — the full-power release branch. Every check is a hard gate. There is **no** `changes` job; everything runs unconditionally. The release chart is published by `release-chart`.

**Policy decision already made for these gates** (by the maintainer):
- `helm lint` stays **soft** on develop, **hard** on main — same as its historical behavior.
- Render + kubeconform + the schema negative-test are **hard on BOTH branches**. Rationale: a chart that won't render or won't validate is a deploy-breaking error, not a style nit — and develop is the branch that *publishes and deploys* the `srw-dev` chart. So unlike `ruff`, these get teeth even on develop.

**Critical CI mechanics discovered during design (get these exactly right):**
1. Both publish jobs use `if: always() && !cancelled() && …`. Under `always()`, a **failed** `needs` job does **not** auto-block the dependent — only the explicit `needs.<job>.result` clauses in the `if:` do. Therefore wiring the gate requires **adding a clause to the `if:`**, not just appending to the `needs:` list.
2. On develop, `chart-test` is gated on `needs.changes.outputs.chart == 'true'`, so on a non-chart push it is **skipped** (`result == 'skipped'`). The gate clause must allow skip. `!contains(needs.chart-test.result, 'failure')` is true for `success` and `skipped`, false only for `failure` — exactly what we want. On main, `chart-test` always runs, so the stricter `needs.chart-test.result == 'success'` is used.
3. GitHub Actions only interpolates `${{ … }}`. The kubeconform catalog URL uses Go-template `{{ .Group }}` syntax — this passes through a `run:` block untouched. Do **not** escape it.

**The `values.schema.json` is intentionally permissive.** Helm validates the *fully-merged* values (defaults + overrides) against it on every `lint`/`template`/`install`/`upgrade`. Two consequences:
- The schema must be satisfiable by the **default** `helm/values.yaml`. Every type/enum below was checked against the real defaults.
- `additionalProperties` is left at its default (`true`). Do **not** set it to `false`. The existing `test-values.yaml` sets keys that aren't in the chart's documented surface (e.g. `headscale.enabled`, which the chart ignores — the real key is `headscale.url`). A strict schema would reject those valid-today overlays. Cross-field semantics (internal/external `externalUrl` requirements, license acceptance, nats mutual-exclusion, neo4j enterprise license) are **already** enforced by template `required`/`fail` guards and must stay there — the schema only does structural type/enum checks, which run *earlier* (before rendering).

---

## Scenario coverage matrix

`chart-test` renders these four files. Two exist; two are created in this plan. Each exercises an output shape the others don't.

| Scenario file | Secrets mode | Databases | Notable rendered output | Status |
|---|---|---|---|---|
| `test-values.yaml` | ESO (`externalSecrets.enabled`) | internal | `ExternalSecret` CRDs, `Certificate`, Traefik `Middleware`, full home stack | exists |
| `customer-external-values.yaml` | pre-existing Secret (`secrets.existingSecret`) | external (postgres/vector/mongo external, neo4j off) | external IdP/git/cloud wiring, nginx ingress | exists |
| `eval-values.yaml` | chart-created (`secrets.create`) | internal, minimal | chart-managed `Secret`, minimal single-node/mini-PC footprint | **create (Task 2)** |
| `vm-values.yaml` | chart-created | internal | `vmController` Deployment + ClusterRole (kubevirt verbs) + namespace | **create (Task 3)** |

Plus `invalid-values.yaml` (Task 1) — **not** in the render matrix; it is the negative test asserted to fail.

---

## File structure

**Create:**
- `helm/values.schema.json` — permissive draft-07 type/enum schema (Task 1)
- `helm/ci/invalid-values.yaml` — deliberately schema-invalid values, asserted to fail (Task 1)
- `helm/ci/eval-values.yaml` — chart-created-secrets / minimal-footprint scenario (Task 2)
- `helm/ci/vm-values.yaml` — `vmController.enabled=true` scenario (Task 3)

**Modify:**
- `.github/workflows/develop.yml` — remove helm-lint steps from `lint`; add `chart-test` + `chart-schema-negative` jobs (chart-gated, render hard / lint soft); add `chart-test` to `deploy-experimental` gate (Task 4)
- `.github/workflows/main.yml` — remove helm-lint steps from `lint`; add `chart-test` + `chart-schema-negative` jobs (always-run, all hard); add `chart-test` to `release-chart` gate (Task 5)

---

## Prerequisites (one-time local setup)

The chart-rendering tasks (1–3) are fully testable locally and must be verified locally before the CI tasks. Install the same tools CI uses.

- [ ] **Confirm Helm is present (need 3.x):**

Run: `helm version --short`
Expected: `v3.x.y` (any 3.x). If absent: `sudo dnf install -y helm` or download v3.17.0 from the Helm releases page.

- [ ] **Install kubeconform v0.6.7 locally:**

```bash
curl -fsSL \
  https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz \
  | sudo tar -xz -C /usr/local/bin kubeconform
kubeconform -v
```
Expected: prints `v0.6.7`.

- [ ] **Define the render+validate command you'll reuse** (paste this helper into your shell so the tasks below are one-liners):

```bash
ktest() {  # ktest <scenario>   e.g. ktest eval
  helm template srw helm/ -f "helm/ci/$1-values.yaml" \
    | kubeconform \
        -kubernetes-version 1.28.0 \
        -ignore-missing-schemas \
        -schema-location default \
        -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
        -summary
}
```

---

## Task 1: `values.schema.json` + negative test

Add install-time structural validation and prove it bites.

**Files:**
- Create: `helm/ci/invalid-values.yaml`
- Create: `helm/values.schema.json`

- [ ] **Step 1: Write the negative-test values file (the "failing test")**

Create `helm/ci/invalid-values.yaml`:

```yaml
# NEGATIVE TEST — this file is INTENTIONALLY invalid and MUST be rejected by
# helm/values.schema.json. CI (the chart-schema-negative job) asserts that
# `helm template -f invalid-values.yaml` exits non-zero. If this file ever
# renders successfully, the schema has stopped doing its job.
#
# Do NOT "fix" this file to make it render. Its only purpose is to fail.
license:
  acceptTerms: "yes"   # WRONG TYPE: must be a boolean (true/false), not a string
```

- [ ] **Step 2: Run it to confirm it currently (wrongly) renders — no schema yet**

Run: `helm template srw helm/ -f helm/ci/invalid-values.yaml >/dev/null && echo "RENDERED (no schema present yet)"`
Expected: prints `RENDERED (no schema present yet)`. This is the bug Task 1 fixes — bad input is accepted because there is no schema.

- [ ] **Step 3: Write the schema (minimal implementation)**

Create `helm/values.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "Superhuman Remote Worker Helm values",
  "description": "Phase 1 structural validation: types and enums for high-signal fields. Intentionally permissive (additionalProperties allowed) so forward-compatible keys and existing values overlays are not rejected. Cross-field semantics (internal/external requirements, license acceptance, nats mutual-exclusion, neo4j enterprise license) stay in template `required`/`fail` guards.",
  "type": "object",
  "required": ["license", "global"],
  "properties": {
    "license": {
      "type": "object",
      "properties": {
        "acceptTerms": { "type": "boolean" }
      }
    },
    "global": {
      "type": "object",
      "properties": {
        "domain": { "type": "string" }
      }
    },
    "externalSecrets": {
      "type": "object",
      "properties": {
        "enabled": { "type": "boolean" }
      }
    },
    "secrets": {
      "type": "object",
      "properties": {
        "create": { "type": "boolean" },
        "existingSecret": { "type": "string" }
      }
    },
    "mcp": {
      "type": "object",
      "properties": {
        "enabled": { "type": "boolean" }
      }
    },
    "opencloud": {
      "type": "object",
      "properties": {
        "enabled": { "type": "boolean" }
      }
    },
    "nextcloud": {
      "type": "object",
      "properties": {
        "enabled": { "type": "boolean" }
      }
    },
    "ingress": {
      "type": "object",
      "properties": {
        "enabled": { "type": "boolean" },
        "tls": {
          "type": "object",
          "properties": {
            "enabled": { "type": "boolean" }
          }
        }
      }
    },
    "nats": {
      "type": "object",
      "properties": {
        "internal": { "type": "boolean" }
      }
    },
    "databases": {
      "type": "object",
      "properties": {
        "neo4j": {
          "type": "object",
          "properties": {
            "edition": { "type": "string", "enum": ["community", "enterprise"] }
          }
        }
      }
    },
    "vmController": {
      "type": "object",
      "properties": {
        "enabled": { "type": "boolean" },
        "transport": { "type": "string", "enum": ["http", "nats", "both"] }
      }
    }
  }
}
```

- [ ] **Step 4: Confirm the schema now REJECTS the invalid file (negative test passes)**

Run: `helm template srw helm/ -f helm/ci/invalid-values.yaml`
Expected: FAILS, non-zero exit, message like:
`values don't meet the specifications of the schema(s) in the following chart: superhuman-remote-worker … - license.acceptTerms: Invalid type. Expected: boolean, given: string`

- [ ] **Step 5: Confirm the schema does NOT over-reject valid inputs (defaults + all real scenarios)**

Run:
```bash
helm template srw helm/ >/dev/null && echo "defaults OK"
for s in test customer-external; do
  helm template srw helm/ -f "helm/ci/$s-values.yaml" >/dev/null && echo "$s OK"
done
helm lint helm/ -f helm/ci/test-values.yaml
```
Expected: `defaults OK`, `test OK`, `customer-external OK`, and `helm lint` reports `1 chart(s) linted, 0 chart(s) failed`. If any valid input is rejected, a declared type/enum disagrees with the real default — fix the schema (loosen that property), do not change the values file.

- [ ] **Step 6: Commit**

```bash
git add helm/values.schema.json helm/ci/invalid-values.yaml
git commit -m "feat(helm): add values.schema.json input validation + negative test"
```

---

## Task 2: `eval-values.yaml` — chart-created-secrets scenario

Covers secrets mode 2 (`secrets.create`) and the minimal single-node/mini-PC footprint, neither of which `test`/`customer-external` exercise.

**Files:**
- Create: `helm/ci/eval-values.yaml`

- [ ] **Step 1: Write the scenario values file**

Create `helm/ci/eval-values.yaml`:

```yaml
# Eval / single-node profile — chart-created secrets (secrets mode 2).
# Exercises the `secrets.create=true` path that test-values (ESO) and
# customer-external (existing Secret) do not cover, with the minimal footprint
# a mini-PC k3s install would use. The chart auto-generates APP_ENCRYPTION_KEY
# when it is absent from secrets.values (see templates/secret.yaml), so no
# secret keys are required just to render. NOT a production config — chart-
# created secrets are for eval only.
license:
  acceptTerms: true

global:
  domain: "eval.example.com"

# Mode 2: chart creates the runtime Secret from these values (dev/eval only).
# Empty is fine for a render test; a real eval install adds DB/provider creds.
secrets:
  create: true
  values: {}

# Keep the footprint small: optional heavy components off.
databases:
  neo4j:
    enabled: false

opencloud:
  enabled: false
nextcloud:
  enabled: false

mcp:
  enabled: true

vmController:
  enabled: false

# No home-cluster admin UIs / mesh on a minimal box.
pgadmin:
  enabled: false
mongoExpress:
  enabled: false
dozzle:
  enabled: false
codexProxy:
  enabled: false

ingress:
  enabled: true
  className: traefik
  tls:
    enabled: false
```

- [ ] **Step 2: Confirm a chart-managed Secret renders with an auto-generated key**

Run: `helm template srw helm/ -f helm/ci/eval-values.yaml | grep -A6 'kind: Secret'`
Expected: a `kind: Secret` block containing `APP_ENCRYPTION_KEY:` with a non-empty value (helm generated it via `randAlphaNum`).

- [ ] **Step 3: Render + kubeconform the scenario**

Run: `ktest eval` (the helper from Prerequisites)
Expected: kubeconform summary with `Valid: <N>`, `Invalid: 0`, `Errors: 0`. (`Skipped` may be non-zero — those are CRDs not in the catalog; acceptable.) If a resource is *Invalid*, fix the values file until it renders valid K8s; do not edit templates in this plan.

- [ ] **Step 4: Lint the scenario**

Run: `helm lint helm/ -f helm/ci/eval-values.yaml`
Expected: `1 chart(s) linted, 0 chart(s) failed`.

- [ ] **Step 5: Commit**

```bash
git add helm/ci/eval-values.yaml
git commit -m "test(helm): add chart-created-secrets eval scenario for render matrix"
```

---

## Task 3: `vm-values.yaml` — vmController scenario

Covers `vmController.enabled=true`, which renders a Deployment, a dedicated namespace, and a ClusterRole/ClusterRoleBinding with kubevirt verbs — output no other scenario produces. The `vm-controller/` templates have no `required`/`fail` guards, so this renders on defaults; a dummy SSH key is supplied for realism.

**Files:**
- Create: `helm/ci/vm-values.yaml`

- [ ] **Step 1: Write the scenario values file**

Create `helm/ci/vm-values.yaml`:

```yaml
# Same-cluster KubeVirt scenario — exercises the vmController templates
# (Deployment, namespace, RBAC with kubevirt.io verbs, service) plus the
# transport enum in values.schema.json. Uses chart-created secrets so the file
# is self-contained. The chart does NOT install KubeVirt/CDI — irrelevant for a
# static render test.
license:
  acceptTerms: true

global:
  domain: "vm.example.com"

secrets:
  create: true
  values: {}

vmController:
  enabled: true
  transport: http
  namespace: agent-vms
  # Dummy key — only needs to be a non-empty string to render the mount.
  vmSshPublicKey: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEYFORCIRENDERONLY00000000000000000 agent@srw"

ingress:
  enabled: true
  className: traefik
  tls:
    enabled: false
```

- [ ] **Step 2: Confirm the vmController resources render**

Run: `helm template srw helm/ -f helm/ci/vm-values.yaml | grep -E '^kind: |name: .*vm-controller' | grep -iE 'vm-controller|ClusterRole|Deployment' | head`
Expected: shows a vm-controller `Deployment` and a `ClusterRole`/`ClusterRoleBinding`. (If naming differs, just confirm vm-controller resources appear at all.)

- [ ] **Step 3: Render + kubeconform the scenario**

Run: `ktest vm`
Expected: kubeconform summary `Invalid: 0`, `Errors: 0`.

- [ ] **Step 4: Lint the scenario**

Run: `helm lint helm/ -f helm/ci/vm-values.yaml`
Expected: `1 chart(s) linted, 0 chart(s) failed`.

- [ ] **Step 5: Commit**

```bash
git add helm/ci/vm-values.yaml
git commit -m "test(helm): add vmController scenario for render matrix"
```

---

## Task 4: Wire gates into `develop.yml` (soft lint, hard render, chart-gated, blocks dev publish)

**Files:**
- Modify: `.github/workflows/develop.yml`

- [ ] **Step 1: Remove the helm-lint steps from the `lint` job**

In `.github/workflows/develop.yml`, delete this block from the `lint` job (currently around lines 96–108) — these checks move into `chart-test`:

```yaml
      - name: Install Helm
        uses: azure/setup-helm@v4
        with:
          version: v3.17.0

      - name: Helm lint (home cluster scenario)
        run: helm lint helm/ -f helm/ci/test-values.yaml
        continue-on-error: true

      - name: Helm lint (customer external-services scenario)
        run: helm lint helm/ -f helm/ci/customer-external-values.yaml
        continue-on-error: true
```

The `lint` job now ends after the "Verify Playwright pin consistency" step.

- [ ] **Step 2: Add the `chart-test` and `chart-schema-negative` jobs**

Insert these two jobs immediately after the `changes` job (after its last line, before the `# Python tests` separator around line 541). Both gate on `needs.changes.outputs.chart`:

```yaml
  # ---------------------------------------------------------------------------
  # Chart correctness gates (Phase 1) — render matrix + schema validation.
  # Runs only when helm/ changed. Lint is advisory (soft, matches the lint
  # job's history); render + kubeconform are HARD — a chart that won't render
  # or validate is deploy-breaking, and this branch publishes the srw-dev chart.
  # ---------------------------------------------------------------------------
  chart-test:
    needs: changes
    if: needs.changes.outputs.chart == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
    strategy:
      fail-fast: false
      matrix:
        scenario:
          - test
          - customer-external
          - eval
          - vm
    steps:
      - uses: actions/checkout@v4

      - name: Install Helm
        uses: azure/setup-helm@v4
        with:
          version: v3.17.0

      - name: Install kubeconform
        run: |
          curl -fsSL \
            https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz \
            | sudo tar -xz -C /usr/local/bin kubeconform
          kubeconform -v

      - name: Helm lint (${{ matrix.scenario }})
        run: helm lint helm/ -f helm/ci/${{ matrix.scenario }}-values.yaml
        continue-on-error: true

      - name: Render + kubeconform (${{ matrix.scenario }})
        run: |
          helm template srw helm/ -f helm/ci/${{ matrix.scenario }}-values.yaml \
            | kubeconform \
                -kubernetes-version 1.28.0 \
                -ignore-missing-schemas \
                -schema-location default \
                -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
                -summary

  # ---------------------------------------------------------------------------
  # Schema negative test — proves values.schema.json actually rejects bad input.
  # A deliberately-invalid values file MUST make `helm template` fail. Hard gate
  # (the whole point is that it fails); runs only on chart changes.
  # ---------------------------------------------------------------------------
  chart-schema-negative:
    needs: changes
    if: needs.changes.outputs.chart == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4

      - name: Install Helm
        uses: azure/setup-helm@v4
        with:
          version: v3.17.0

      - name: Assert invalid values are rejected by the schema
        run: |
          if helm template srw helm/ -f helm/ci/invalid-values.yaml >/dev/null 2>&1; then
            echo "::error::helm/ci/invalid-values.yaml rendered successfully — values.schema.json is not rejecting it. The schema has stopped validating."
            exit 1
          fi
          echo "OK: invalid-values.yaml was correctly rejected by values.schema.json"
```

- [ ] **Step 3: Gate the dev chart publish on `chart-test`**

In the `deploy-experimental` job (around line 1149), add `chart-test` to `needs:` and add the result clause to the `if:`. The `needs:` list gains one line:

```yaml
      - build-agent-vm-base
      - chart-test
```

And the `if:` gains a final clause (note `!contains(... 'failure')` deliberately allows the *skipped* case on non-chart pushes):

```yaml
    if: >-
      always() && !cancelled() &&
      github.event_name == 'push' &&
      needs.dependency-audit.result == 'success' &&
      !contains(needs.test-python.result, 'failure') &&
      !contains(needs.test-cockpit.result, 'failure') &&
      !contains(needs.chart-test.result, 'failure')
```

- [ ] **Step 4: Validate the workflow YAML parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/develop.yml')); print('develop.yml OK')"`
Expected: `develop.yml OK`. If `actionlint` is available, also run `actionlint .github/workflows/develop.yml` (expected: no output).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/develop.yml
git commit -m "ci(develop): chart render matrix + kubeconform + schema gates, block dev publish on failure"
```

---

## Task 5: Wire gates into `main.yml` (all hard, always-run, blocks release publish)

Mirror of Task 4 with two differences: no `changes`-gating (everything runs on main), and **no** `continue-on-error` anywhere (lint is hard on the release branch).

**Files:**
- Modify: `.github/workflows/main.yml`

- [ ] **Step 1: Remove the helm-lint steps from the `lint` job**

In `.github/workflows/main.yml`, delete this block from the `lint` job (currently around lines 66–73):

```yaml
      - name: Install Helm
        uses: azure/setup-helm@v4
        with:
          version: v3.17.0
      - name: Helm lint (home cluster scenario)
        run: helm lint helm/ -f helm/ci/test-values.yaml
      - name: Helm lint (customer external-services scenario)
        run: helm lint helm/ -f helm/ci/customer-external-values.yaml
```

- [ ] **Step 2: Add the `chart-test` and `chart-schema-negative` jobs**

Insert these two jobs after the `lint` job (before the `codeql` job, around line 75). No `needs`/`if` gate; lint is hard (no `continue-on-error`):

```yaml
  # ---------------------------------------------------------------------------
  # Chart correctness gates (Phase 1) — render matrix + schema validation.
  # Full-power branch: always runs, every step is a hard gate.
  # ---------------------------------------------------------------------------
  chart-test:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
    strategy:
      fail-fast: false
      matrix:
        scenario:
          - test
          - customer-external
          - eval
          - vm
    steps:
      - uses: actions/checkout@v4

      - name: Install Helm
        uses: azure/setup-helm@v4
        with:
          version: v3.17.0

      - name: Install kubeconform
        run: |
          curl -fsSL \
            https://github.com/yannh/kubeconform/releases/download/v0.6.7/kubeconform-linux-amd64.tar.gz \
            | sudo tar -xz -C /usr/local/bin kubeconform
          kubeconform -v

      - name: Helm lint (${{ matrix.scenario }})
        run: helm lint helm/ -f helm/ci/${{ matrix.scenario }}-values.yaml

      - name: Render + kubeconform (${{ matrix.scenario }})
        run: |
          helm template srw helm/ -f helm/ci/${{ matrix.scenario }}-values.yaml \
            | kubeconform \
                -kubernetes-version 1.28.0 \
                -ignore-missing-schemas \
                -schema-location default \
                -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
                -summary

  # ---------------------------------------------------------------------------
  # Schema negative test — proves values.schema.json rejects bad input.
  # ---------------------------------------------------------------------------
  chart-schema-negative:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4

      - name: Install Helm
        uses: azure/setup-helm@v4
        with:
          version: v3.17.0

      - name: Assert invalid values are rejected by the schema
        run: |
          if helm template srw helm/ -f helm/ci/invalid-values.yaml >/dev/null 2>&1; then
            echo "::error::helm/ci/invalid-values.yaml rendered successfully — values.schema.json is not rejecting it. The schema has stopped validating."
            exit 1
          fi
          echo "OK: invalid-values.yaml was correctly rejected by values.schema.json"
```

- [ ] **Step 3: Gate the release chart publish on `chart-test`**

In the `release-chart` job (around line 921), add `chart-test` to the inline `needs:` array and add the clause to the `if:`. On main `chart-test` always runs, so use the strict `== 'success'`:

```yaml
    needs: [resolve-version, build-agent, build-orchestrator, build-cockpit, build-mcp, build-workspace, build-vm-controller, build-agent-vm-base, chart-test]
    if: >-
      always() && !cancelled() &&
      github.event_name == 'push' &&
      needs.resolve-version.result == 'success' &&
      needs.build-agent.result == 'success' &&
      needs.build-orchestrator.result == 'success' &&
      needs.build-cockpit.result == 'success' &&
      needs.build-mcp.result == 'success' &&
      needs.build-agent-vm-base.result == 'success' &&
      !contains(needs.build-workspace.result, 'failure') &&
      needs.chart-test.result == 'success'
```

- [ ] **Step 4: Validate the workflow YAML parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/main.yml')); print('main.yml OK')"`
Expected: `main.yml OK`. If `actionlint` is available, also run `actionlint .github/workflows/main.yml`.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/main.yml
git commit -m "ci(main): chart render matrix + kubeconform + schema gates, block release publish on failure"
```

---

## Operator follow-ups (not code — note for the maintainer)

- **Branch protection:** the YAML makes a chart failure show **red** and (via the publish-gate clauses) blocks the dev/release chart from publishing. To also block **PR merge** into develop, add `chart-test` (and optionally `chart-schema-negative`) to the repo's required status checks in branch protection — that is a GitHub setting, not something this plan can change.
- **First real exercise:** the GitHub-side matrix/gating can only be fully verified by a PR that touches `helm/`. The first such PR is the live smoke test of Tasks 4–5.

---

## Self-Review

**Spec coverage** (against `docs/deployment_readiness.md` Phase 1 — render matrix, kubeconform, values.schema.json, wired into the existing lint flow gated on `helm/` changes):
- Render matrix across meaningful permutations → Tasks 2–3 add the two missing scenarios; Tasks 4–5 run all four in a matrix. ✔
- kubeconform schema validation of rendered output → Tasks 4–5 render-pipe to kubeconform with a pinned K8s version + CRD catalog. ✔
- `values.schema.json` for install-time validation → Task 1, with a negative test proving it bites. ✔
- Wired into the existing pipeline, gated on `helm/` changes, honoring develop-soft/main-hard → Tasks 4 (chart-gated, lint soft) and 5 (always-run, all hard), with the maintainer's deviation (render/kubeconform hard on develop too) applied. ✔
- Publish protection (a stated risk of adding a schema that could break publish) → publish jobs gated on `chart-test`. ✔

**Placeholder scan:** every code/step block contains literal content (full schema JSON, full values files, full job YAML, exact commands + expected output). No TBD/TODO. The one intentional empty is `secrets.values: {}`, which is a valid, deliberate value (the chart auto-generates the only mandatory key) — not a placeholder.

**Type/name consistency:** scenario filenames (`test`, `customer-external`, `eval`, `vm`) match the matrix entries in both workflows and the `helm/ci/<scenario>-values.yaml` convention. `chart-test` is the job name referenced consistently in both `needs:` additions and `if:` clauses. The schema enums (`community|enterprise`, `http|nats|both`) match the values files (`vmController.transport: http`, `databases.neo4j.edition` unset→default community).

**Known acceptable looseness:** `-ignore-missing-schemas` means a CRD absent from the datreeio catalog is *skipped*, not failed (kubeconform `Skipped` count may be >0). Tightening to strict validation (drop `-ignore-missing-schemas`, add `-strict`, vendor CRD schemas for the 4 CRD kinds the chart emits: `ExternalSecret`, `Middleware`, `Certificate`) is a deliberate Phase-1 stretch, not a gap.
