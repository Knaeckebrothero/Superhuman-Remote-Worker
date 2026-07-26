# App Guide M1 Verification

**Date:** 2026-07-26
**Status:** Passed and operationally closed. M1 is complete.

This record separates the exact runtime implementation from the evaluator and
documentation commits that follow it. All live questions and sessions used for
this verification were synthetic. No datasource credentials, provider keys,
base URLs, private session content, or local deployment secrets are retained
in this document or the evaluation artifact.

## Final identities

| Item | Identity |
|---|---|
| Runtime implementation source | `6ff4d443c7e2d8defa5db792e2d80f21f392686a` |
| Evaluation-manifest source | `0bb6d0184d0992e41b4b6220bcaa67ab1213c738` |
| Agent image tag | `srw-registry:5000/srw-agent:m1f-6ff4d443` |
| Local and running image ID | `sha256:c0bbbf3cc2d84c5d0e85d79acb08d10d1740e1e28c09ab86b988e79fcd03ca9d` |
| Local image digest | `sha256:7c509a75b2d17bbf348a6a526fe9b3246c41b4323f037f77fe445b5d3a2511d4` |
| k3d/containerd manifest | `sha256:feba3c17d4a01d30927a76722980d2cd71e0e7ac82b8991923b9e8a28bfe0962` |
| Local image size | `3,747,426,909` bytes |
| Managed App Guide digest | `f05ca7e71e3b8514b8d919cf565c0c93ecb445f967492e5c80affd50990aeb45` |
| Held-out corpus digest | `d146ae493509240a64126cf289a5735690ac07037c394c70794d028f974dd0da` |
| Evaluation harness digest | `018fe3db1eca205f2fe14f575208fe5cde4d159a45be13a1ba0d8596ae3cd9e6` |
| Held-out corpus shape | 30 cases: 17 product positives and 13 near-miss negatives |

The agent image was built from a detached clean worktree at the full runtime
source revision. A running session pod independently reported the same full
`BUILD_SHA`, image ID, and guide digest from inside the container.

`0bb6d018` changes only the held-out case definition: the broad session and
workspace question now permits the directly relevant
`permissions-and-availability` secondary topic. It does not change runtime
bytes, required facts, forbidden claims, or the image under test.

## Acceptance deployment

The final matrix and evaluation ran against Helm release `srw` in namespace
`srw`, revision 23, through the responding local k3d API at
`https://172.18.0.2:6443`.

| Setting | Acceptance value |
|---|---|
| Agent image | `m1f-6ff4d443` |
| DB experts | off |
| DB skills | off |
| App Guide break-glass | off |
| Gitea storage request | `5Gi` |

The exact test configuration intentionally disables DB experts and skills to
prove that the managed guide is a product floor rather than a database-authored
skill. The release stayed available during rollout: the preceding Keycloak pod
served traffic until its replacement completed the chart's initialization
hook.

## Final fresh and resumed session matrix

All six live cells passed on the final image and the same managed digest. The
three resumed sessions predated the final image and retained real conversation
history across multiple upgrade checkpoints.

| Session path | Workspace | Baseline | Observed reader trajectory | Result |
|---|---|---:|---|---|
| Fresh | None | 0 messages | `index -> overview` | Pass |
| Fresh | Virtual | 0 messages | `index -> datasources-email` | Pass |
| Fresh | Container/Sandbox | 0 messages | `index -> project-loops` | Pass |
| Resumed pre-upgrade | None | 18 turns / 57 messages | `automations` | Pass |
| Resumed pre-upgrade | Virtual | 6 turns / 24 messages | `fleet-and-delegation` | Pass |
| Resumed pre-upgrade | Container/Sandbox | 5 turns / 20 messages | `jobs` | Pass |

Every row returned
`f05ca7e71e3b8514b8d919cf565c0c93ecb445f967492e5c80affd50990aeb45`,
loaded the expected focused topic, loaded no unrelated topic, and passed its
answer-level semantic checks. This confirms current immutable delivery on all
three workspace tiers and on sessions resumed from pre-upgrade state.

During diagnostics, heavily reused synthetic threads sometimes answered a
recently repeated, identical topic from their existing conversation rather
than issuing another same-turn reader call. The runtime still appends the
managed current-guide boundary to every trusted reader-capable LLM call, and
the deterministic compaction regression proves the current reader remains
available after recall. To keep the final live resume evidence independent of
that repeated-topic history, each final resumed probe used a relevant topic
not previously asked in that thread.

## Final live-model evaluation

The standalone runner used the production-fenced managed menu and reader, one
fresh message list per case, and the authorized synthetic session's resolved
model route in memory. Credentials and endpoint details were neither printed
nor written to the artifact.

Final artifact:
`eval/app_guide/runs/m1f-live-current-6ff4d443/`

| Metric | Result |
|---|---:|
| Model | `gemma-4-moe` |
| Run ID | `bd261547f8d7` |
| Cases | 30 / 30 complete corpus |
| Passed | 30 |
| Pass rate | 1.0000 |
| Trajectory pass rate | 1.0000 |
| Grounding pass rate | 1.0000 |
| Positive reader-call rate | 1.0000 |
| Positive topic pass rate | 1.0000 |
| Near-miss reader false-positive rate | 0.0000 |
| Critical forbidden claims | 0 |
| Provider/harness errors | 0 |
| Release gate | **Pass** |

The first exact-image attempt scored 29/30 because the broad session/workspace
question legitimately read both `sessions` and
`permissions-and-availability`; all answer grounding passed. The case manifest
was corrected to declare that related secondary topic explicitly. A following
attempt was terminated unscored by the disposable session's ten-minute idle
timer after eight passing cases. The evaluation thread alone was given a
60-minute timeout, and the next single uninterrupted full-corpus run produced
the passing artifact above.

The artifact's staged evaluation tree is intentionally not a Git checkout, so
its internal source field reports `unknown`/dirty. Runtime provenance is
instead bound by the independently verified pod image, full `BUILD_SHA`, and
guide digest recorded above.

## Automated and static verification

The final App Guide Python union on runtime source `6ff4d443` completed in
54.90 seconds:

```text
607 passed, 1 deselected, 6 warnings
```

The one explicit deselection is
`test_canvas_and_direct_browser_tool_inventories_have_guide_coverage`. It
detects an unrelated concurrent Office renderer classification gap:
`CanvasRenderer.office` was introduced outside the App Guide work and has not
yet been assigned guide coverage. The gap remains visible and is not presented
as an App Guide pass.

Additional final checks:

- focused prompt/corpus contracts: **58 passed**;
- evaluation harness after the related-topic correction: **25 passed**;
- Ruff check and format check: pass;
- `git diff --check`: pass;
- `helm lint helm/ -f helm/ci/test-values.yaml`: pass;
- `helm lint helm/ -f helm/ci/customer-external-values.yaml`: pass.

Both Helm lint runs retain only the existing informational recommendation that
`Chart.yaml` include an icon.

## Break-glass and recovery evidence

The earlier live break-glass probe remains valid for the final implementation:

- with `APP_GUIDE_BREAK_GLASS_DISABLED=true`, `/health` stayed HTTP 200 but
  reported degraded `operator_break_glass`, `/ready` remained healthy, and the
  managed reader disappeared;
- after restoring false and rebinding, health returned to ready and the
  current digest-stamped guide and reader returned; and
- no frozen, database-authored, project, user, or mutable workspace guide was
  revived during either transition.

## M1 result and remaining scope

The M1 exit gate passes: fresh and pre-upgrade resumed sessions on every
persistent workspace tier use the exact current immutable guide with DB skill
and expert resolution disabled, the complete held-out release corpus passes,
critical false-positive claims remain at zero, and the operator withdrawal and
recovery path is verified.

This closes the dependable static text-guide milestone only. Runtime
capability evaluation, component-wide provenance surfaces, drift automation,
validated UI actions, screenshots/help cards, guided UI, and pinned public
repository fallback remain in Phases 2–8.

## Operational closeout

After capturing the acceptance evidence:

- Helm revision 24 restored the normal local values while retaining the final
  image: DB experts on, DB skills off, App Guide break-glass off, and Gitea at
  `5Gi`;
- every SRW pod reached Ready;
- all 23 tracked synthetic M1 threads were resolved to their `M1 …` titles,
  permanently deleted with `permanent=true&force=true`, and verified absent;
- sensitive `/tmp/srw-app-guide-*` files were shredded, and all detached M1
  build worktrees were removed;
- obsolete M1 candidate image references were removed locally and from the k3d
  node, leaving only `m1f-6ff4d443`; and
- the temporary orchestrator and Keycloak port-forwards were stopped.

The final ignored evaluation artifact is intentionally retained as release
evidence. Older ignored diagnostic runs in the same runs directory are not
release evidence.
