# CI deploy race silently reverts image tags on dev

**Status:** **OPEN**, found 2026-08-13 while deploying the KB reindex fixes. Diagnosis complete,
fix proposed below (§Fix), not implemented — a broken `deploy-experimental` breaks every dev
deploy, so this wants a deliberate change rather than a drive-by.
**Severity:** Medium-high — not data loss, but it silently reverts a deploy you just verified,
which corrupts the *verification process* rather than the data. Dev only.

## Symptom

A deploy lands, is verified working, and is then rolled back to older code by a concurrently
running CI job — with no error anywhere. Observed:

```
commit 2517b73c  "deploy: update image tags to sha-2b03875"
  deployment/values-experimental.yaml:
    - tag: sha-0ef826c     ← the just-deployed fix
    + tag: sha-9dd5175     ← an older commit's image
```

`b00ada17` had deployed `sha-0ef826c` (KB reindex fixes). Both fixes were verified live on dev.
`2517b73c` then reverted the orchestrator to `sha-9dd5175`, which predates them.

Nothing failed. Both CI runs report **success**.

## Mechanism

Each component's tag is its **per-component identity** — `needs.changes.outputs.<component>-sha`,
the full sha of the newest commit that touched that component's build inputs
(`.github/workflows/develop.yml:448`). The `changes` job computes it with `fetch-depth: 0`, so
it sees full history **as of its own run's checkout**.

That is the bug: the identity is correct *for that run*, and **stale for the branch** if a newer
commit has since landed. Two runs overlap, and the one that finishes last writes its view of
history over the other's:

| | run for `2b038753` | run for `0ef826ca` (newer) |
|---|---|---|
| newest orchestrator commit *it can see* | `9dd51755` | `0ef826ca` |
| writes | `tag: sha-9dd5175` | `tag: sha-0ef826c` |
| finished | **13m08s — last** | 8m31s — first |

Last writer wins, and last-to-finish is not last-in-history. `concurrency` does not save us:
the group is `${{ github.workflow }}-${{ github.ref }}` with **`cancel-in-progress: false`**, so
overlapping runs all complete.

The deploy job has no staleness guard — it writes unconditionally
(`if: needs.build-<component>.result == 'success'`) and cannot check ancestry even if it wanted
to, because its checkout is **shallow**:

```yaml
deploy-experimental:
  steps:
    - uses: actions/checkout@v4
      with:
        ref: develop          # no fetch-depth: 0 — depth 1
```

## Second defect: tags are mutable, so they do not identify content

The same run also rebuilds and **re-pushes an existing tag with different content**. A
`workflow_dispatch` run at head `a6b20e38` pushed
`…orchestrator:sha-9dd5175` — the same tag string as an older image, now carrying head's code.

Consequences:

- `sha-9dd5175` meant different things at different times. After the dispatch it *did* contain
  the KB fixes; before it did not.
- **"What is deployed?" is unanswerable from the tag alone.** During this incident the only
  reliable way to tell was to exercise the behaviour — running a reindex and checking whether
  `search_doc` and `knowledge_links` were populated.
- Rollback by tag is not trustworthy: re-pulling a tag may not reproduce the prior image.

## Third defect (cosmetic but confusing): one commit, two disagreeing files

`2517b73c` is internally inconsistent — its message and `deployment/fleet.yaml` say
`sha-2b03875`, while `deployment/values-experimental.yaml` says `sha-9dd5175`. The message
reports the run sha; the values file reports per-component identities. Both are "correct" by
their own rule, which makes the commit unreadable at a glance.

## Why it matters more than "dev self-heals"

It does self-heal — the next successful deploy of that component fixes it. The real damage is to
trust in verification: **an agent or engineer can verify a fix on dev, report it truthfully, and
be wrong minutes later.** That happened here. It was caught only by re-checking the running image
for an unrelated reason.

Any workflow of the form *deploy → verify → report* is unsound while this exists.

## Fix

Guard each tag write so a component's tag never moves **backwards**. Two changes to
`deploy-experimental` in `.github/workflows/develop.yml`:

1. Give the deploy job full history so it can reason about ancestry:

```yaml
- uses: actions/checkout@v4
  with:
    ref: develop
    fetch-depth: 0
```

2. Gate each component's update step on ancestry. Compare against
`provenance.components.<component>.sourceRevision`, which already stores the **full** sha —
better than parsing the 7-char tag, which is ambiguous and awkward to resolve:

```yaml
- name: Update orchestrator tag
  if: needs.build-orchestrator.result == 'success'
  env:
    COMPONENT_SHA: ${{ needs.changes.outputs.orchestrator-sha }}
  run: |
    : "${COMPONENT_SHA:=$GITHUB_SHA}"
    CURRENT=$(yq -r '.provenance.components.orchestrator.sourceRevision // ""' \
                 deployment/values-experimental.yaml)
    # Skip when what is already deployed is NOT an ancestor of what we would write:
    # that means a newer run already landed and this run's view of history is stale.
    if [ -n "$CURRENT" ] && ! git merge-base --is-ancestor "$CURRENT" "$COMPONENT_SHA"; then
      echo "::notice::skipping stale orchestrator tag: $CURRENT is not an ancestor of $COMPONENT_SHA"
      exit 0
    fi
    export COMPONENT_TAG="sha-${COMPONENT_SHA::7}"
    ...unchanged...
```

Note each component step writes **two** files — `deployment/values-experimental.yaml` and
`deployment-vms/srw-vm-controller/fleet.yaml` — so the guard belongs at the top of the step,
covering both, not around an individual `yq` call.

Idempotent, order-independent, and it needs no coordination between runs. Equal shas are
ancestors of themselves, so a re-run is a no-op rather than an error.

**Alternatives considered and rejected**

- *`cancel-in-progress: true`* — does not help once the older run has already finished, and
  cancels legitimate in-flight builds.
- *Compute tags from the deploy-time checkout instead of the run's* — would reference images
  that may not be built yet.
- *Only deploy when `github.sha` is the branch tip* — a docs-only tip would then block deploying
  a code commit underneath it.

**Also worth fixing, separately:** make the deploy commit message report the per-component
identities it actually wrote, so the message and the file agree.

## Verification when the fix lands

The failure needs two overlapping runs to reproduce, so verify deliberately:

1. Push a commit touching orchestrator paths; immediately push a second unrelated commit so the
   runs overlap.
2. Confirm both runs succeed and the final `values-experimental.yaml` orchestrator tag is the
   **newer** commit's identity.
3. Confirm the older run logged the `skipping stale orchestrator tag` notice rather than writing.

## Related

- `[removed]` §12.4 — the verification
  this incident invalidated and forced to be redone.
