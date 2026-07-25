# App Guide M1 Verification

**Date:** 2026-07-25
**Status:** Final verification in progress; M1 is not closed yet.

This record separates implementation evidence from the release evidence that
still has to run against the exact final source and guide digest. All live
questions, projects, and sessions used for this work are synthetic. No
datasource credentials, private session content, provider keys, or local
deployment secrets are included here or in the retained evaluation artifacts.

## Final candidate identity

| Item | Identity |
|---|---|
| Source revision | `d999b673f821404c49851a1e516f5683ee42206f` |
| Agent image tag | `srw-registry:5000/srw-agent:m1f-d999b673` |
| Local/imported image ID | `sha256:aa43bbdb138fc5cdc5cc1dec2563f14d365afc6ee04d2e6cb10ff21f186d4eb5` |
| Managed App Guide digest | `20974f2e991e45b407bd18d73719f2579b518af595d095ac2a94dcaadd86072f` |
| Held-out corpus digest | `e4bf7b51aed8c148bfc006ddb8ae2fbda4a1ec3a2c29fa2e10ac01389cc2ebc8` |
| Held-out corpus shape | 30 cases: 17 product positives and 13 near-miss negatives |

The image was built from a detached clean worktree at the full source revision,
then inspected in a container. Its `BUILD_SHA` and embedded guide digest matched
the table above. It was also imported into the local k3d node's containerd
store. The first Helm attempt from that clean worktree stopped before changing
the release because the ignored `collabora-online` chart dependency was not
present in the detached worktree. The dependency must be supplied and the
deployment identity rechecked before the final run.

## Deployment checkpoint before final hardening

The first live acceptance pass used:

| Item | Identity |
|---|---|
| Source/guide checkpoint | `bbed7e3e` plus the then-current managed bundle |
| Agent image tag | `srw-registry:5000/srw-agent:m1f-bbed7e3e` |
| Image ID | `sha256:95afc13593f349e5d4a9220cac30b06296823b4aec752e6f207f2c1186724a2a` |
| Managed App Guide digest | `7d16da6338f4e6bc1a50b3c1ab20e6da3a2d0405f6815998ecc506d82d744dc0` |
| Helm release | `srw`, namespace `srw`, revision 9 |
| Acceptance flags | DB experts off, DB skills off, break-glass off |

The host kubeconfig endpoint was unavailable from this environment, so the
checks used the responding local k3d API at `https://172.18.0.2:6443`. The
registry path was also unavailable from the node, so the exact image was
imported into k3s containerd rather than rebuilt or silently substituted. The
existing `srw-gitea-data` PVC remained bound at 5 GiB.

## Fresh and resumed session matrix

The complete six-cell behavioral matrix passed on the checkpoint bundle. Each
row returned the same managed digest shown above and called the real
`read_product_guide` tool.

| Session path | Workspace | Observed focused trajectory | Result |
|---|---|---|---|
| Fresh | None | `index -> overview` | Pass |
| Fresh | Virtual | `index` and explicit enterprise-identity guide gap | Pass |
| Fresh | Container/Sandbox | `index -> project-loops` | Pass |
| Resumed pre-upgrade | None | `index -> project-loops` after persisted compaction | Pass |
| Resumed pre-upgrade | Virtual | `experts` | Pass |
| Resumed pre-upgrade | Container/Sandbox | `permissions-and-availability` | Pass |

The resumed sandbox contained a deliberately stale mutable workspace
`app-guide` with marker `STALE_WORKSPACE_GUIDE_M1` and digest
`2300590...`. The response used the managed `7d16da...` bundle and did not
repeat the false workspace claim.

The None-workspace resume used a real persisted compaction: 18 messages became
12, a summary was stored with manual trigger metadata, and the operation took
about 380.7 seconds. A subsequent product question retrieved the guide again
from the current managed catalog.

This matrix proves the delivery paths, but it does not by itself close M1
because the routing/grounding hardening changed the bundle afterward. The
final `d999b673` image must still demonstrate the current digest in live
fresh/resumed paths.

## Break-glass contract

The local release was upgraded once with
`APP_GUIDE_BREAK_GLASS_DISABLED=true` and then restored to false.

While disabled:

- `/health` stayed HTTP 200 and reported `status: degraded`;
- the bounded App Guide reason was `operator_break_glass`;
- `/ready` stayed HTTP 200 with chat readiness true; and
- `/status` exposed 48 tools and omitted `read_product_guide`.

After restoring the value and rebinding the session:

- `/health` returned to healthy/ready;
- `/ready` remained true;
- `/status` exposed 49 tools including `read_product_guide`; and
- a product question retrieved the then-current managed digest.

No mutable same-name guide was restored during either transition.

## Live-model evaluation

The standalone runner uses the production-fenced menu and reader, one fresh
context per case, and an in-memory credential handoff from the already
authorized synthetic session. Credentials and base URLs are not written to
artifacts.

The first complete current-arm run against the checkpoint bundle was a useful
failure, not a release pass:

| Metric | Result |
|---|---:|
| Cases | 30 |
| Passed | 18 |
| Trajectory pass rate | 0.9000 |
| Grounding pass rate | 0.6333 |
| Critical forbidden claims | 0 |
| Provider errors | 0 |
| Release gate | Fail |

That run exposed ambiguous near-miss routing, weak prerequisite wording, and
unsupported workflow composition. Those observations drove the hardening in
`d999b673`. A candidate-snapshot run then reached 29/30 with no critical
forbidden claims or provider errors; it is diagnostic evidence only because it
was not the deployed `current` arm and preceded the final honest-gap assertion
adjustment.

The release artifact still required is one complete deployed current-arm run
against guide digest `20974f2e...`. It must contain all 30 cases, no provider
errors, no critical forbidden claims, and a passing release gate.

Retained synthetic artifacts are under the ignored
`eval/app_guide/runs/` directory. They intentionally exclude raw guide
payloads, provider credentials, base URLs, and private sessions.

## Automated verification checkpoint

The following focused command passed after the final routing changes:

```bash
.venv/bin/pytest \
  tests/test_app_guide_content.py \
    -k 'not canvas_and_direct_browser_tool_inventories_have_guide_coverage' \
  tests/test_product_help_tool.py \
  tests/test_app_guide_eval_harness.py -q
```

Result: **42 passed, 1 deselected**. Ruff check, Ruff format check, and
`git diff --check` also passed for the touched files.

The excluded content test currently detects unrelated concurrent work:
`CanvasRenderer.office` has not yet been classified in the Canvas guide
coverage contract. That failure is not hidden or counted as green App Guide
evidence, and this work does not modify the separate Office implementation.

Before M1 closes, run the complete focused Python union, both Helm lint
profiles, final `git diff --check`, the deployed current-arm model evaluation,
and the final-digest fresh/resumed confirmation. Record the results here and
only then change the feature status and remaining Phase 1 checkboxes.
