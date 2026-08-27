#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Self-healing Helm apply for the Tilt inner loop.
#
# Replaces the `helm_resource` extension's helm-apply-helper.py. Same contract
# (Tilt passes TILT_IMAGE_<i> plus the RELEASE_NAME/CHART/NAMESPACE env), same
# resulting `helm upgrade --install` argv — with one addition: a preflight that
# clears a stale `pending-*` release before invoking Helm.
#
# Why the preflight exists
# ------------------------
# Tilt kills the helm subprocess whenever it cancels an in-flight deploy — a
# superseding build, a Ctrl-C, or the k8s_upsert_timeout_secs deadline. Helm
# writes its release secret as `pending-upgrade` *before* applying and flips it
# to `deployed` only at the end, so a killed helm leaves that secret pending
# forever. Every later `helm upgrade` then refuses with:
#
#     Error: UPGRADE FAILED: another operation (install/upgrade/rollback) is in progress
#
# and the inner loop is wedged until someone clears it by hand. Restarting Tilt
# does not help — the lock lives in the cluster, not the process. Diagnosed
# 2026-07-29; see knowledge-base/knowledge/features/tilt_inner_loop_dev.md "Risks and known
# gotchas".
#
# The recovery is to drop the pending revision's secret. The previous revision
# stays `deployed` and becomes the head again, and because we always pass
# `--take-ownership`, the next upgrade re-adopts any object the killed run had
# already applied. Nothing is uninstalled and no data is touched — unlike
# `tilt trigger srw`, which runs the delete helper (`helm uninstall`) first.
#
# The preflight only fires when the pending revision has been untouched for
# SRW_HELM_STALE_AFTER seconds, so it cannot stomp a genuinely running helm.
# -----------------------------------------------------------------------------
set -euo pipefail

RELEASE="${RELEASE_NAME:?RELEASE_NAME not set (Tilt supplies this)}"
CHART="${CHART:?CHART not set (Tilt supplies this)}"
NS="${NAMESPACE:-}"
STALE_AFTER="${SRW_HELM_STALE_AFTER:-60}"
# helm/kubectl here use the ambient kubeconfig context, same as the extension's
# helper did. That is fine for `helm upgrade` (Tilt refuses non-local contexts
# unless allow_k8s_contexts is set), but the preflight *deletes* a Secret, so
# gate that one step on the context actually being the local dev cluster.
EXPECT_CONTEXT="${SRW_HELM_EXPECT_CONTEXT:-k3d-srw}"

ns_args=()
if [[ -n "$NS" ]]; then
    ns_args=(--namespace "$NS")
fi

# --- preflight: clear a stale pending-* revision -----------------------------
unstick_pending_release() {
    local status age pending_revs
    status="$(helm status "$RELEASE" "${ns_args[@]}" -o json 2>/dev/null |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["status"])' 2>/dev/null || true)"

    case "$status" in
    pending-install | pending-upgrade | pending-rollback) ;;
    *) return 0 ;;
    esac

    # How long has it sat there? A live helm run is not ours to interrupt.
    age="$(helm status "$RELEASE" "${ns_args[@]}" -o json 2>/dev/null | python3 -c '
import datetime, json, sys
ts = json.load(sys.stdin)["info"]["last_deployed"]
# Helm emits RFC3339 with nanosecond precision; trim to microseconds for fromisoformat.
import re
ts = re.sub(r"\.(\d{6})\d+", r".\1", ts)
delta = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(ts)
print(int(delta.total_seconds()))
' 2>/dev/null || echo 0)"

    if ((age < STALE_AFTER)); then
        echo "srw-preflight: release '$RELEASE' is $status but only ${age}s old — assuming a live helm run, not touching it." >&2
        return 0
    fi

    pending_revs="$(helm history "$RELEASE" "${ns_args[@]}" -o json 2>/dev/null | python3 -c '
import json, sys
hist = json.load(sys.stdin)
print(" ".join(str(h["revision"]) for h in hist if h["status"].startswith("pending")))
' 2>/dev/null || true)"

    if [[ -z "$pending_revs" ]]; then
        return 0
    fi

    local ctx
    ctx="$(kubectl config current-context 2>/dev/null || true)"
    if [[ "$ctx" != "$EXPECT_CONTEXT" ]]; then
        echo "srw-preflight: refusing to clear the lock — kube context is '$ctx', expected '$EXPECT_CONTEXT'." >&2
        echo "srw-preflight: set SRW_HELM_EXPECT_CONTEXT if that is wrong. Letting helm fail loudly instead." >&2
        return 0
    fi

    echo "srw-preflight: release '$RELEASE' stuck in $status for ${age}s (revisions: $pending_revs)." >&2
    echo "srw-preflight: dropping the pending revision secret(s); --take-ownership re-adopts any applied object." >&2

    for rev in $pending_revs; do
        kubectl delete secret "sh.helm.release.v1.${RELEASE}.v${rev}" \
            "${ns_args[@]}" --ignore-not-found >&2
    done

    echo "srw-preflight: cleared. Head is now $(helm list "${ns_args[@]}" --all -o json 2>/dev/null | python3 -c '
import json, sys
rels = json.load(sys.stdin)
print(next((f'"'"'rev {r["revision"]} ({r["status"]})'"'"' for r in rels), "none"))
' 2>/dev/null || echo "unknown")." >&2
}

unstick_pending_release

# --- build the image --set flags --------------------------------------------
# Tilt sets TILT_IMAGE_<i> to the fully-tagged ref it just built/pushed, in the
# order of the Tiltfile's image_deps. TILT_IMAGE_KEY_REPO_<i>/_TAG_<i> carry the
# chart keys those halves map to. Splitting on the LAST colon keeps a registry
# port in the repository half: srw-registry:5000/srw-agent:tilt-abc splits into
# `srw-registry:5000/srw-agent` + `tilt-abc`.
flags=("$@")

image_count="${TILT_IMAGE_COUNT:-0}"
for ((i = 0; i < image_count; i++)); do
    img_var="TILT_IMAGE_${i}"
    repo_key_var="TILT_IMAGE_KEY_REPO_${i}"
    tag_key_var="TILT_IMAGE_KEY_TAG_${i}"

    img="${!img_var:-}"
    repo_key="${!repo_key_var:-}"
    tag_key="${!tag_key_var:-}"

    if [[ -z "$img" || -z "$repo_key" || -z "$tag_key" ]]; then
        echo "srw-preflight: image slot $i is incompletely wired (img='$img' repo_key='$repo_key' tag_key='$tag_key')" >&2
        exit 1
    fi

    flags+=(--set "${repo_key}=${img%:*}" --set "${tag_key}=${img##*:}")
done

# --- apply -------------------------------------------------------------------
install_cmd=(helm upgrade --install "${flags[@]}" "${ns_args[@]}" "$RELEASE" "$CHART")
echo "Running cmd: ${install_cmd[*]}" >&2
"${install_cmd[@]}" >&2

# Hand Tilt the object set the release owns so it can track pod status. `-n`
# supplies the default namespace for manifest entries that omit one.
echo "Running cmd: helm get manifest $RELEASE | kubectl get -f - -oyaml" >&2
helm get manifest "$RELEASE" "${ns_args[@]}" | kubectl get "${ns_args[@]}" -oyaml -f -
