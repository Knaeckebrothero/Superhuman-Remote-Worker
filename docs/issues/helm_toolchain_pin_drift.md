---
tags:
  - issue
  - ci
  - helm
  - chart
  - tooling
---

# The chart toolchain pin (Helm v3.17.0) has drifted behind every Helm a developer is likely to have

**Filed:** 2026-07-28, found while debugging a CI-only test failure.
**Status:** OPEN. No production impact observed; this is drift risk plus a
recurring source of CI-only test failures.
**Severity:** **low-medium** — the chart is validated in CI against a version
nobody edits it on, and the gap widens on its own as distros ship Helm 4.
**Component:** 10 × `azure/setup-helm@v4` steps —
`.github/workflows/develop.yml:128,176,676,766,1509`;
`.github/workflows/main.yml:93,138,171,400,1165`.

## Summary

Every CI step pins **v3.17.0**. The chart is therefore linted, rendered,
kubeconform-checked, schema-gated, packaged and published by 3.17.0 only —
while the humans editing it run something newer, and operators install it with
whatever their distro ships. Nothing validates the chart against those.

## Evidence

On the current dev box, `which -a helm` resolves four paths that are really two
binaries (`/usr/*/sbin` are usrmerge symlinks):

| Path | Version | Source |
|---|---|---|
| `/usr/local/bin/helm` | **3.19.0** | hand-installed 2025-11-01, not rpm-owned — **wins on PATH** |
| `/usr/bin/helm` | **4.2.2** | Fedora package `helm-4.2.2-1.fc44` |

So the chart is authored on 3.19.0, validated in CI on 3.17.0, and the platform
package manager is already two minors *and a major* ahead of the pin.

The versions are not interchangeable for anything that reads Helm's output.
Helm 3.19 replaced the `values.schema.json` validator
(`xeipuuv/gojsonschema` → `santhosh-tekuri/jsonschema`), changing both message
wording and path notation:

```
3.17.0: - canvas.livePreview.viewer.database.provisionRole: ... does not match: false
        - canvas.livePreview.viewer.database.credentials: Must validate one and only one schema (oneOf)

3.19.0: - at '/canvas/livePreview/viewer/database/provisionRole': value must be false
        - at '/canvas/.../credentials': 'oneOf' failed, none matched
```

That difference alone produced a CI failure (`177df2c3`); see
`docs/issues/local_test_runs_cannot_reproduce_ci_environment.md`.

## Risk

Two directions, and the test-failure one is the less important:

1. **Untested rendering.** The chart's correctness is only ever demonstrated on
   3.17.0. Operators installing with Helm 4 exercise a code path CI has never
   run — schema validation, `lookup`, subchart resolution, and OCI
   packaging/push all changed across 3.18 → 4.x.
2. **Recurring CI-only test failures.** Any test matching Helm's stdout/stderr
   is version-coupled. The canvas contract tests are now dialect-tolerant, but
   that is 11 tests; the pattern will recur wherever a new test greps Helm
   output.

## What is already verified

`tests/test_canvas_slice3_infra.py` (11 tests) passes under **3.17.0, 3.19.0
and 4.2.2** as of `177df2c3` — the chart's schema gates reject the same
misconfigurations in all three; only the message text differs.

**Not verified under anything but 3.17.0:**

- the render matrix (`helm template` over each `helm/ci/*-values.yaml`
  scenario) and its kubeconform pass
- `helm lint`
- the invalid-values gate (`develop.yml:185` — `helm/ci/invalid-values.yaml`
  MUST fail to render; a validator change could alter *whether* it fails, not
  just how it reports)
- `helm dependency build` against the Collabora subchart
- `helm package` / `helm push` for the `srw-dev` chart publish

## Fix

Pick one and make it explicit; the current state is neither.

**Option A — bump the pin.** Move all 10 steps to 3.19.x or 4.x after running
the checks above against the candidate. 4.x is the more valuable target (it is
what users will have) and the more likely to surface real work. Bump all 10
together — a split pin means the render gate and the publish step disagree.

**Option B — keep 3.17.0 as the contract** and state it as the required
developer version, so local `helm template` matches the gate. Cheaper, but it
fights the distro: Fedora already ships 4.2.2, and nothing stops
`/usr/local/bin/helm` drifting again.

Either way, the pin deserves a comment naming *why* that version, next to at
least one of the 10 steps.

## Notes

Helm is a single static Go binary — switching versions is replacing one file,
with no service restart and no reboot. `rm /usr/local/bin/helm` on the dev box
promotes the packaged 4.2.2 immediately; a specific version is a tarball from
`get.helm.sh`. This makes Option A cheap to *trial* locally before committing
to it.

## Related

- `docs/issues/local_test_runs_cannot_reproduce_ci_environment.md` — the
  CI-only failure class this pin contributes to.
- `177df2c3` — made the canvas chart contract tests tolerate both dialects.
