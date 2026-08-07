#!/usr/bin/env bash
# Runner-side refusal of untrusted workloads.
#
# WHY THIS FILE EXISTS, AND WHY IT IS NOT IN .github/
# ---------------------------------------------------
# This repository is PUBLIC and its CI runs on self-hosted runners inside the
# homelab cluster. For a `pull_request` event GitHub executes the workflow file
# taken FROM THE PULL REQUEST's merge ref — the fork's copy. So the routing
# expression in .github/workflows/*.yml
#
#     runs-on: ${{ github.event_name == 'pull_request' && 'ubuntu-latest' || ... }}
#
# is something a stranger can simply delete in their own PR. It protects against
# OUR typo landing on develop/main. It does not protect against a fork at all.
#
# This script does. It is baked into the runner IMAGE and wired up through
# ACTIONS_RUNNER_HOOK_JOB_STARTED, so it is not part of the repository and a pull
# request cannot edit, skip, or delete it. The runner executes it before the
# job's first step — before actions/checkout — so nothing from the triggering ref
# has been fetched when it decides. A non-zero exit fails the job.
#
# If this script and the workflow routing ever disagree, this one wins and the
# job dies having done nothing.
#
# Checks 1-3 are each independently sufficient; check 4 only ever adds refusals.
# See docs/ci_self_hosted_runners.md and scripts/check_ci_runners.py (the
# merge-time half, which asserts this file still exists and still contains these
# checks).
#
# Deliberately NOT `set -e`: every check is explicit, and an unexpected non-zero
# from a probe must not be mistaken for a pass. `-u` catches an unset variable
# we meant to read; `pipefail` keeps a piped failure visible.
set -uo pipefail

readonly EXPECTED_REPO="Knaeckebrothero/Superhuman-Remote-Worker"

fail() {
  printf '::error title=Self-hosted runner refused this job::%s\n' "$1" >&2
  printf 'runner=%s repo=%s event=%s ref=%s head_ref=%s base_ref=%s workflow=%s actor=%s\n' \
    "${RUNNER_NAME:-?}" "${GITHUB_REPOSITORY:-?}" "${GITHUB_EVENT_NAME:-?}" \
    "${GITHUB_REF:-?}" "${GITHUB_HEAD_REF:-}" "${GITHUB_BASE_REF:-}" \
    "${GITHUB_WORKFLOW:-?}" "${GITHUB_ACTOR:-?}" >&2
  exit 1
}

# 1. This repository only. A runner registered at the wrong scope, or reachable
#    from another repo sharing a runner group, gets nothing.
[ "${GITHUB_REPOSITORY:-}" = "$EXPECTED_REPO" ] \
  || fail "unexpected repository '${GITHUB_REPOSITORY:-<unset>}'"

# 2. Event ALLOWLIST, not a denylist. An event type nobody has thought about yet
#    must be refused by default rather than inherit access — `merge_group`,
#    `workflow_run` and `pull_request_target` all have trust properties that
#    differ from `push`. Adding one is a deliberate edit to the runner image,
#    which is a separate review from a workflow change.
case "${GITHUB_EVENT_NAME:-}" in
  push | schedule | workflow_dispatch) ;;
  *) fail "event '${GITHUB_EVENT_NAME:-<unset>}' is not permitted on self-hosted runners" ;;
esac

# 3. Independent confirmation from the ref, so a mislabelled or spoofed
#    GITHUB_EVENT_NAME cannot get through check 2. Pull-request runs check out
#    refs/pull/N/merge and set HEAD_REF/BASE_REF regardless of what the event is
#    called.
case "${GITHUB_REF:-}" in
  refs/pull/*) fail "ref '${GITHUB_REF}' is a pull request ref" ;;
esac
[ -z "${GITHUB_HEAD_REF:-}" ] \
  || fail "GITHUB_HEAD_REF='${GITHUB_HEAD_REF}' — this is a pull request"
[ -z "${GITHUB_BASE_REF:-}" ] \
  || fail "GITHUB_BASE_REF='${GITHUB_BASE_REF}' — this is a pull request"

# 4. Payload cross-check, best effort. The event file is not guaranteed to be
#    readable this early in the job, so its absence is NOT itself a refusal —
#    checks 1-3 each stand alone. This only ever adds refusals, never removes.
if [ -r "${GITHUB_EVENT_PATH:-/nonexistent}" ] && command -v jq >/dev/null 2>&1; then
  head_repo=$(jq -r '.pull_request.head.repo.full_name // empty' \
    "$GITHUB_EVENT_PATH" 2>/dev/null || true)
  [ -z "$head_repo" ] \
    || fail "event payload carries a pull_request head repo '$head_repo'"
fi

printf 'job-started-guard: accepted (repo=%s event=%s ref=%s)\n' \
  "${GITHUB_REPOSITORY}" "${GITHUB_EVENT_NAME}" "${GITHUB_REF:-<unset>}"
