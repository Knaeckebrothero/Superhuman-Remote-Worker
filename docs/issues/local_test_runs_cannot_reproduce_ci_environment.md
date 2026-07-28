---
tags:
  - issue
  - ci
  - testing
  - tooling
  - developer-experience
---

# A green local test run does not predict CI — the test environment diverges on Helm version and injected env vars

**Filed:** 2026-07-28, from two consecutive CI failures on `develop`.
**Status:** OPEN. Both instances are fixed (`177df2c3`, `5eb436eb`); the gap
that produced them is not.
**Severity:** **medium** — each instance costs a full CI cycle, and `-x`
guarantees they arrive one per push. Neither was findable locally.
**Component:** `.github/workflows/develop.yml:774`,
`.github/workflows/main.yml:408`, `tests/conftest.py`.

## Summary

Two pushes in a row failed `test-python` on tests that pass on a developer
machine. Neither was a code regression. Both were **environment divergence**:
the test asserted on a value the environment supplies, and CI's environment
supplies a different one.

The suite is the authoritative gate, but the locally-run suite is not the same
suite. There is currently no way to run it the way CI does short of pushing.

## Evidence — the two instances

### 1. Helm error dialect (fixed in `177df2c3`)

`tests/test_canvas_slice3_infra.py` asserted on `values.schema.json` rejection
text. Helm 3.19 swapped its validator (`xeipuuv/gojsonschema` →
`santhosh-tekuri/jsonschema`), which reworded every message and changed paths
from dotted to JSON-pointer:

```
3.17.0 (CI):    - canvas.livePreview.viewer.database.credentials.create: ... does not match: false
3.19.0 (local): - at '/canvas/livePreview/viewer/database/credentials/create': value must be false
```

The assertion only accepted the 3.19 spelling, so it passed locally and failed
on the pinned CI toolchain. See
`docs/issues/helm_toolchain_pin_drift.md` for the pin itself.

### 2. Ambient `SRW_*` provenance vars (fixed in `5eb436eb`)

Both workflows declare build metadata in their **top-level `env:` block**, and
GitHub injects top-level env into *every step of every job* — including
`pytest tests/`:

| Var | develop.yml | main.yml |
|---|---|---|
| `SRW_SOURCE_URL` | ✓ (:41) | ✓ (:36) |
| `SRW_DOCUMENTATION_URL` | ✓ (:42) | ✓ (:37) |
| `SRW_RELEASE_VERSION` | ✓ (:43) | — |

Those are exactly the names `src/core/runtime_provenance.py:114-121` reads, so
`test_register_payload_separates_full_declared_provenance_from_short_sha`
monkeypatched two of the three, left `SRW_DOCUMENTATION_URL` ambient, and
asserted it was `None`. Unset locally → pass. Set in CI → fail.

## Why the existing mitigation was not enough

The known practice — *"CI's `-x` masks everything after the first failure, so
re-run the full suite locally without `-x`"* — was followed. A full local run
between the two failures reported **11384 passed** and still did not surface
instance 2, because the local environment does not have the vars CI injects.
Running the whole suite is necessary but not sufficient; the *environment*
has to match too.

Three axes differ, and all three are invisible from inside a local run:

| Axis | CI | Local box |
|---|---|---|
| Python | 3.12 (`actions/setup-python`) | `python3` is **3.14**; `.venv/bin/python` is **3.12.10** |
| Helm | pinned **v3.17.0** (10 steps) | **3.19.0** shadowing packaged **4.2.2** |
| Env | `SRW_SOURCE_URL`, `SRW_DOCUMENTATION_URL`, `SRW_RELEASE_VERSION` set | unset |

`-x` at `develop.yml:774` / `main.yml:408` is the amplifier: with ~11.4k tests
and a CI-only failure class, each push reveals exactly one instance.

## Fix

**1. Add a `test-like-ci` entry point** (`scripts/` or a make target) that pins
all three axes. This would have caught both instances in one local run:

```bash
export SRW_SOURCE_URL=https://github.com/knaeckebrothero/Superhuman-Remote-Worker
export SRW_DOCUMENTATION_URL=https://github.com/knaeckebrothero/Superhuman-Remote-Worker/tree/main/docs
export SRW_RELEASE_VERSION=0.0.0-dev.sha-local
PATH="<helm-3.17.0-dir>:$PATH" .venv/bin/python -m pytest tests/ -q   # no -x
```

It should also print the known local-only residue so a run can be read without
re-deriving it each time:

- `tests/test_database_phase1.py` — 2 failures, needs Postgres on `:5432`
  (and the gitignored repo-root `.env` sets `DATABASE_URL`)
- `ModuleNotFoundError` collection errors — `webdav3` (`tests/cloud_sync/`, 17
  errors), `imap_tools` (`tests/test_email_tools.py`). Both are in
  `requirements.txt`, so CI installs them.

Keeping the env values in the script rather than a doc matters: they must stay
byte-identical to the workflow `env:` blocks, and drift there is silent.

**2. Reconsider `-x` for `test-python`.** `--maxfail=5` keeps the
fail-fast economics on a genuinely broken push while surfacing a cluster of
environment failures in one cycle instead of five.

**3. (optional) Scope the build metadata to the jobs that need it.** Moving
`SRW_*` out of the top-level `env:` into the build jobs removes the leak at
source. Lower priority — `tests/conftest.py` now strips the whole surface, and
that also protects a developer who has the vars exported locally.

## Guard already in place

`5eb436eb` added an autouse fixture in `tests/conftest.py` that deletes the
declared-provenance surface (common `SRW_*`, every `SRW_<COMPONENT>_*` derived
from the `ProductComponent` enum, `SRW_DEPLOYMENT_PROVENANCE_JSON`,
`BUILD_SHA`) before each test. **New provenance tests should rely on that
fixture rather than re-adding ad-hoc `monkeypatch.setenv` calls** — the fixture
runs first, so setting a value in a test still works.

## Repro

Both instances reproduce locally by supplying the missing axis:

```bash
# instance 1 — needs helm 3.17.0 on PATH
PATH="<helm-3.17.0-dir>:$PATH" pytest tests/test_canvas_slice3_infra.py

# instance 2
SRW_DOCUMENTATION_URL=https://github.com/knaeckebrothero/Superhuman-Remote-Worker/tree/main/docs \
  pytest tests/test_orchestrator_client_register.py
```

Verified after both fixes: full suite with CI env exported and Helm 3.17.0 on
PATH → `2 failed, 11384 passed, 19 skipped, 17 errors` — the 2 + 17 being the
documented local-only residue above.

## Adjacent observation (not the same bug)

The assertion fixed in `177df2c3` had a **dead branch**. It read:

```python
assert ("production Canvas viewers require an operator-provisioned role" in stderr
        or "/canvas/livePreview/viewer/database/credentials/create" in stderr)
```

The first string appears nowhere in `helm/` — no schema `description`, no
template `fail`. Helm's validators emit neither custom messages nor
descriptions, so that branch could never match, and only the second did any
work. The assertion read stronger than it was for ~2 weeks. Worth a grep for
other `A or B` string assertions where `A` is unreachable.

## Related

- `docs/issues/helm_toolchain_pin_drift.md` — the pin behind instance 1.
- `src/core/runtime_provenance.py` — the reader of the leaked env surface.
