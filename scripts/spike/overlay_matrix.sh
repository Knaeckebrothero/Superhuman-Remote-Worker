#!/usr/bin/env bash
# scripts/spike/overlay_matrix.sh
#
# FUSE-on-FUSE feasibility matrix for protected cloud mode (Phase 0 spike,
# docs/design/cloud_access_unification.md §6.1). Run INSIDE a workspace pod
# that already has a LIVE rclone cloud mount at $LOWER (the "raw" cloud view).
#
# Stacks fuse-overlayfs (writable scratch upper) over the rclone WebDAV lower
# and measures the protected-mode stack the design proposes:
#
#     merged = fuse-overlayfs(lowerdir=<rclone mount, RO>, upperdir=<local scratch>)
#
# ALL destructive ops go through the MERGED view only; the raw rclone mount is
# never mutated directly. Mount the rclone lower --read-only for a faithful
# protected-mode lower (this harness does not mount rclone itself; it consumes
# $LOWER as given). For a clean COLD readdir/copy-up number, restart the rclone
# mount (fresh vfs dir-cache) before each run.
#
# Output is NUMBERS + observed behaviour, not pass/fail. A failed overlay mount
# is a valid (no-go) finding.
#
# Steps are ordered so each measurement is uncontaminated by the next:
#   (c) readdir (cold, before any mutation) -> (b) copy-up (big file intact)
#   -> (a) whiteout storm (protects the big file) -> whiteout-form inspection
#   -> (d) external-edit hook -> (e) build workload -> (f) enumerate_diff.
#
# Usage:  overlay_matrix.sh <rclone-mount-point> [seeded-subdir]
#   $1 LOWER  : active rclone mount point, e.g. /cloud/home  (required)
#   $2 SUBDIR : subdir under LOWER holding the seeded test tree (default: .)
#
# Env:
#   SRC_ROOT     : path importable as src.services.cloud_overlay.whiteout
#                  (default /app; set to a copy dir if src is not on the image).
#   BIG_GLOB     : find -name pattern for the ~100MB copy-up file (default big.bin)
#   EXT_EDIT_CMD : optional command mutating $EXT_REL under the lower via the
#                  backend directly (WebDAV), for external-edit test (d). It runs
#                  with env EXT_REL set to the tree-relative path of the probe.
set -uo pipefail

LOWER="${1:?rclone mount point, e.g. /cloud/home}"
SUBDIR="${2:-.}"
SRC_ROOT="${SRC_ROOT:-/app}"
BIG_GLOB="${BIG_GLOB:-big.bin}"
TEST="$LOWER/$SUBDIR"
BASE="${HOME}/.overlay"
UP="$BASE/upper"; WORK="$BASE/work"; MERGED="$BASE/merged"
MTEST="$MERGED/$SUBDIR"

hr() { printf '\n========== %s ==========\n' "$1"; }
ok() { printf '  -> %s\n' "$1"; }

hr "environment"
echo "whoami=$(id -un) uid=$(id -u)"
echo "rclone: $(rclone version 2>/dev/null | head -1)"
fuse-overlayfs --version 2>&1 | grep -i 'fuse-overlayfs\|FUSE library' || true
echo "LOWER=$LOWER  TEST=$TEST"
echo "-- lower is a mountpoint? --"; mountpoint "$LOWER" 2>&1 || echo "  (not a mountpoint)"
echo "-- lower mount line --"; mount | grep -F "$LOWER" || true
echo "-- lower top entries --"; ls "$LOWER" 2>&1 | head

rm -rf "$BASE"; mkdir -p "$UP" "$WORK" "$MERGED"

hr "mount fuse-overlayfs over the rclone lower"
set -x
fuse-overlayfs -o "lowerdir=$LOWER,upperdir=$UP,workdir=$WORK" "$MERGED"
rc=$?
set +x
if [ $rc -ne 0 ] || ! mountpoint -q "$MERGED"; then
  echo "!! fuse-overlayfs mount FAILED (rc=$rc) — this is the no-go signal"
  exit 1
fi
ok "fuse-overlayfs mounted at $MERGED"
trap 'fusermount3 -u "$MERGED" 2>/dev/null || fusermount -u "$MERGED" 2>/dev/null || true' EXIT
echo "-- overlay mount line --"; mount | grep -F "$MERGED" || true

# ---------------------------------------------------------------------------
hr "(c) readdir latency: raw rclone lower vs merged overlay (COLD then WARM)"
echo "NOTE: for a true cold number the rclone vfs dir-cache must be fresh"
echo "      (restart the rclone mount before this run)."
echo "-- COLD ls -R on RAW lower (cold cloud PROPFIND) --"
time ls -R "$TEST" >/dev/null 2>&1
echo "-- COLD ls -R on MERGED (overlay over the same, now-warming lower) --"
time ls -R "$MTEST" >/dev/null 2>&1
echo "-- WARM ls -R on RAW lower --"
time ls -R "$TEST" >/dev/null 2>&1
echo "-- WARM ls -R on MERGED --"
time ls -R "$MTEST" >/dev/null 2>&1

# ---------------------------------------------------------------------------
hr "(b) copy-up timing on the ~100 MB binary (in-place edit through MERGED)"
BIG="$(find "$MTEST" -maxdepth 2 -type f -name "$BIG_GLOB" 2>/dev/null | head -1)"
echo "big file: ${BIG:-<none>}"
if [ -n "${BIG:-}" ]; then
  ls -la "$BIG"
  bytes="$(stat -c %s "$BIG" 2>/dev/null)"
  echo "-- 1-byte in-place edit forces full copy-up of $bytes bytes from lower --"
  time sh -c "printf 'X' | dd of=\"$BIG\" bs=1 count=1 conv=notrunc status=none"
  echo "-- upper now holds the copied-up file: --"
  find "$UP" -type f -size +50M -exec ls -la {} \; 2>/dev/null | head
else
  echo "!! no ~100MB file matched '$BIG_GLOB' under $MTEST — copy-up not measured"
fi

# ---------------------------------------------------------------------------
hr "(a) rm -rf whiteout storm (through MERGED only; protects the big file)"
echo "-- (a1) rm -rf a whole nested subtree --"
VICTIM_DIR="$(find "$MTEST" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | grep -v wide | head -1)"
echo "victim dir: ${VICTIM_DIR:-<none>}"
if [ -n "${VICTIM_DIR:-}" ]; then
  n_before="$(find "$VICTIM_DIR" -type f 2>/dev/null | wc -l)"
  echo "files under victim before: $n_before"
  time rm -rf "$VICTIM_DIR"
  ok "removed $VICTIM_DIR via merged view"
fi
echo "-- (a2) delete a handful of INDIVIDUAL files from the wide dir (per-file whiteouts) --"
mapfile -t FILES < <(find "$MTEST/wide" -maxdepth 1 -type f 2>/dev/null | head -6)
for f in "${FILES[@]}"; do rm -f "$f"; done
echo "deleted ${#FILES[@]} individual files from wide/"
echo "-- (a3) inspect upperdir markers: char-dev whiteout vs .wh.* file? --"
echo "find $UP -type c (char-device whiteouts):"
find "$UP" -type c -exec ls -la {} \; 2>/dev/null | head
echo "find $UP -name '.wh.*' (.wh. file whiteouts):"
find "$UP" -name '.wh.*' 2>/dev/null | head -20
echo "find $UP -name '.wh..wh..opq' (opaque-dir sentinels):"
find "$UP" -name '.wh..wh..opq' 2>/dev/null | head
echo "-- opaque xattrs on upper dirs (python os.listxattr; getfattr not in image) --"
python3 - "$UP" <<'PY'
import os, sys
up = sys.argv[1]; found = False
for root, dirs, files in os.walk(up):
    try: attrs = os.listxattr(root)
    except OSError: attrs = []
    for a in attrs:
        if "opaque" in a:
            try: v = os.getxattr(root, a)
            except OSError: v = b"?"
            print(f"  {root}  {a}={v!r}"); found = True
if not found: print("  (no opaque xattrs on any upper dir)")
PY

# ---------------------------------------------------------------------------
hr "(d) external WebDAV edit while overlay is mounted -> merged fresh/stale?"
PROBE="$(find "$MTEST/wide" -maxdepth 1 -type f -name '*.txt' 2>/dev/null | sort | tail -1)"
if [ -n "${PROBE:-}" ]; then
  EXT_REL="${PROBE#"$MERGED"/}"; EXT_REL="${EXT_REL#"$SUBDIR"/}"
  echo "probe file (merged): $PROBE"
  echo "  tree-rel path: $EXT_REL"
  echo "  merged content BEFORE external edit: $(cat "$PROBE" 2>/dev/null | head -c 80)"
  if [ -n "${EXT_EDIT_CMD:-}" ]; then
    echo "  running EXT_EDIT_CMD to mutate '$EXT_REL' via backend directly..."
    EXT_REL="$EXT_REL" bash -c "$EXT_EDIT_CMD" || echo "  (ext edit cmd failed)"
    sleep 2
    echo "  raw    content AFTER external edit: $(cat "$TEST/$EXT_REL" 2>/dev/null | head -c 80)"
    echo "  merged content AFTER external edit: $(cat "$PROBE" 2>/dev/null | head -c 80)"
    echo "  (merged==raw => fresh; merged==old => stale, needs refresh op)"
  else
    echo "  EXT_EDIT_CMD not set — driver performs the WebDAV edit + re-read out of band."
  fi
fi

# ---------------------------------------------------------------------------
hr "(e) build-like write workload: merged overlay vs plain local disk"
WL_LOCAL="${HOME}/.overlay_wl_local"
WL_MERGED="$MTEST/.overlay_wl_build"
run_build_workload() {
  local dst="$1"; mkdir -p "$dst"
  for d in $(seq 1 50); do
    mkdir -p "$dst/pkg$d"
    for f in $(seq 1 30); do
      printf 'package pkg%s;\n// file %s\n%s\n' "$d" "$f" \
        "$(head -c 256 /dev/zero | tr '\0' 'x')" > "$dst/pkg$d/src$f.c"
    done
  done
  head -c 5242880 /dev/zero > "$dst/blob.bin"
}
rm -rf "$WL_LOCAL" "$WL_MERGED"
echo "-- workload on PLAIN LOCAL DISK ($WL_LOCAL) --"
time run_build_workload "$WL_LOCAL"
echo "-- workload in MERGED overlay (lands in upper, scratch-local) --"
time run_build_workload "$WL_MERGED"
echo "-- merged workload files that landed in upper: --"
find "$UP" -path '*overlay_wl_build*' -type f 2>/dev/null | wc -l
rm -rf "$WL_LOCAL"

# ---------------------------------------------------------------------------
hr "(f) enumerate_diff over the upperdir (Task 2 enumerator fidelity)"
cd /
python3 - "$UP" "$SRC_ROOT" <<'PY'
import sys
up, src_root = sys.argv[1], sys.argv[2]
sys.path.insert(0, src_root)
try:
    from src.services.cloud_overlay.whiteout import enumerate_diff
except Exception as e:
    print(f"could not import enumerate_diff from {src_root}: {e}"); sys.exit(0)
entries = enumerate_diff(up)
deleted = [e for e in entries if e.status == "deleted"]
present = [e for e in entries if e.status == "present"]
print(f"enumerate_diff: {len(entries)} entries  ({len(deleted)} deleted, {len(present)} present)")
print("-- ALL deleted entries (whiteouts + opaque dirs) --")
for e in deleted: print(f"  DEL  {e.path}")
print(f"-- present sample (first 8 of {len(present)}) --")
for e in present[:8]: print(f"  PUT  {e.path}")
PY

hr "done"
echo "upperdir object count: $(find "$UP" | wc -l)"
