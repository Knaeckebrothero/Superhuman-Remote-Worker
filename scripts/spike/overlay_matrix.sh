#!/usr/bin/env bash
# scripts/spike/overlay_matrix.sh
#
# FUSE-on-FUSE feasibility matrix for protected cloud mode (Phase 0 spike,
# knowledge-base/knowledge/design/cloud_access_unification.md §6.1). Run INSIDE a workspace pod
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
# $LOWER as given).
#
# Cell map to design §6.1 (the design's own lettering):
#   §6.1(a) rm -rf whiteout storm + enumeration fidelity   -> STEP A (storm),
#           "incl. opaque-dir renames, binaries"           -> STEP A-BIN, STEP A-REN
#   §6.1(b) copy-up timing on a ~100 MB file               -> STEP B
#   §6.1(c) external WebDAV edit mid-session               -> STEP D
#   §6.1(d) readdir/merge latency                          -> STEP C
#   §6.1(e) build-like write workload                      -> STEP E
#   (enumeration fidelity of §6.1(a))                      -> STEP F
#
# Physics learned in the 2026-07-09 spike runs (why the knobs below exist):
#   * The whiteout FORM depends on the upperdir's backing filesystem, NOT on
#     CAP_MKNOD: on kernels >=5.8 an unprivileged uid can mknod char(0,0)
#     whiteout nodes, so an emptyDir/PVC-backed upper yields char devices,
#     while an upper on the container's own overlayfs rootfs gets EPERM and
#     falls back to `.wh.` files. Put $BASE where production puts it
#     (/home/agent-host, the emptyDir).
#   * Whiteout-ing a lower file transfers the WHOLE file through rclone
#     (--vfs-cache-mode full opens it); deletion cost scales with bytes.
#   * A dir-listing warm (`ls -R`) does NOT warm deletion; only content reads do.
#
# Output is NUMBERS + observed behaviour, not pass/fail. A failed overlay mount
# is a valid (no-go) finding.
#
# Usage:  overlay_matrix.sh <rclone-mount-point> [seeded-subdir]
#   $1 LOWER  : active rclone mount point, e.g. /cloud/home  (required)
#   $2 SUBDIR : subdir under LOWER holding the seeded test tree (default: .)
#
# Env:
#   SRC_ROOT    : path importable as src.services.cloud_overlay.whiteout
#                 (default /app; set to a copy dir if src is not on the image).
#   BIG_GLOB    : find -name pattern for the ~100MB file (default big.bin)
#   REMOUNT_CMD : optional command that tears down and re-creates the rclone
#                 mount at $LOWER with a FRESH vfs cache. Invoked between the
#                 cold-raw and cold-merged readdir passes (STEP C) and before
#                 the cold binary delete (STEP A-BIN) so neither pass warms the
#                 other. Without it those cells are only cold-ish and say so.
#   RC_ADDR/RC_USER/RC_PASS : rclone remote-control endpoint of the $LOWER
#                 mount; when set, STEP A-BIN records core/stats bytes
#                 before/after the delete (measures backend bytes moved).
#   EXT_EDIT_CMD: optional command mutating $EXT_REL under the lower via the
#                 backend directly (WebDAV) for STEP D. Runs with env EXT_REL
#                 set to the tree-relative probe path.
#                 NOTE (provenance): in the recorded 2026-07-09 spike runs this
#                 cell was driven MANUALLY out-of-band — an `rclone rcat` to
#                 the Nextcloud WebDAV endpoint from a second rclone config
#                 acting as the external editor — not via EXT_EDIT_CMD.
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

rc_stats() {  # print "bytes transfers" from rclone rc core/stats, or "-"
  if [ -n "${RC_ADDR:-}" ]; then
    rclone rc --rc-addr "$RC_ADDR" --rc-user "${RC_USER:-}" --rc-pass "${RC_PASS:-}" \
      core/stats 2>/dev/null |
      python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("bytes"), d.get("transfers"))' \
      || echo "-"
  else
    echo "- (RC_ADDR unset)"
  fi
}

mount_overlay() {  # fresh upper each call; rerun guard against leftover mounts
  fusermount3 -u "$MERGED" 2>/dev/null || fusermount -u "$MERGED" 2>/dev/null || true
  rm -rf "$BASE"; mkdir -p "$UP" "$WORK" "$MERGED"
  fuse-overlayfs -o "lowerdir=$LOWER,upperdir=$UP,workdir=$WORK" "$MERGED"
}

show_markers() {
  echo "char-device whiteouts in upper:"
  find "$UP" -type c -exec ls -la {} \; 2>/dev/null | head -20
  echo ".wh.* file whiteouts in upper:"
  find "$UP" -name '.wh.*' 2>/dev/null | head -20
  echo "opaque xattrs on upper dirs (python os.listxattr; getfattr not in image):"
  python3 - "$UP" <<'PY'
import os, sys
up = sys.argv[1]; found = False
for root, dirs, files in os.walk(up):
    try: attrs = os.listxattr(root)
    except OSError: attrs = []
    for a in attrs:
        if "opaque" in a or "overlay" in a:
            try: v = os.getxattr(root, a)
            except OSError: v = b"?"
            print(f"  {root}  {a}={v!r}"); found = True
if not found: print("  (none)")
PY
}

hr "environment"
echo "whoami=$(id -un) uid=$(id -u)"
grep -E "^CapEff" /proc/self/status
echo "upper backing fs: $(stat -f -c %T "$(dirname "$BASE")" 2>/dev/null)  (char(0,0) whiteouts need non-overlayfs backing)"
echo "rclone: $(rclone version 2>/dev/null | head -1)"
fuse-overlayfs --version 2>&1 | grep -i 'fuse-overlayfs\|FUSE library' || true
echo "LOWER=$LOWER  TEST=$TEST"
echo "-- lower is a mountpoint? --"; mountpoint "$LOWER" 2>&1 || echo "  (not a mountpoint)"
echo "-- lower mount line --"; mount | grep -F "$LOWER" || true
echo "-- lower top entries --"; ls "$LOWER" 2>&1 | head

# ===========================================================================
# STEP C — design §6.1(d): readdir/merge latency, cold isolated from warm.
# Cold RAW runs BEFORE any overlay exists; REMOUNT_CMD (fresh vfs cache)
# separates it from the cold MERGED pass so neither warms the other.
# ===========================================================================
hr "STEP C [design 6.1(d)]: readdir latency — cold raw vs cold merged"
echo "-- COLD ls -R on RAW lower (no overlay mounted yet) --"
time ls -R "$TEST" >/dev/null 2>&1
if [ -n "${REMOUNT_CMD:-}" ]; then
  echo "-- remounting lower with fresh cache (REMOUNT_CMD) --"
  bash -c "$REMOUNT_CMD" || echo "  (remount cmd failed; merged pass will be WARM)"
else
  echo "-- REMOUNT_CMD unset: merged pass below is over a WARMED lower --"
fi

hr "mount fuse-overlayfs over the rclone lower"
set -x
mount_overlay
rc=$?
set +x
if [ $rc -ne 0 ] || ! mountpoint -q "$MERGED"; then
  echo "!! fuse-overlayfs mount FAILED (rc=$rc) — this is the no-go signal"
  exit 1
fi
ok "fuse-overlayfs mounted at $MERGED"
trap 'fusermount3 -u "$MERGED" 2>/dev/null || fusermount -u "$MERGED" 2>/dev/null || true' EXIT
echo "-- overlay mount line --"; mount | grep -F "$MERGED" || true

echo "-- COLD ls -R on MERGED (fresh overlay; lower cold iff REMOUNT_CMD ran) --"
time ls -R "$MTEST" >/dev/null 2>&1
echo "-- WARM ls -R on RAW lower --"
time ls -R "$TEST" >/dev/null 2>&1
echo "-- WARM ls -R on MERGED --"
time ls -R "$MTEST" >/dev/null 2>&1

# ===========================================================================
# STEP A-BIN — design §6.1(a) "binaries": whiteout a LARGE lower file with a
# cold VFS cache. Open question it answers: fixed round-trip, or scales with
# file size? (Spike answer: the FULL file transfers through rclone.)
# Runs in its OWN overlay instance so STEP B still sees the file in the lower.
# ===========================================================================
hr "STEP A-BIN [design 6.1(a) binaries]: cold delete of the ~100MB file"
if [ -n "${REMOUNT_CMD:-}" ]; then
  fusermount3 -u "$MERGED" 2>/dev/null || true
  echo "-- remounting lower with fresh cache so the delete is COLD --"
  bash -c "$REMOUNT_CMD" || echo "  (remount cmd failed; delete below is WARM)"
  mount_overlay
else
  echo "-- REMOUNT_CMD unset: file cache state is whatever prior steps left --"
fi
BIGM="$(find "$MTEST" -maxdepth 2 -type f -name "$BIG_GLOB" 2>/dev/null | head -1)"
echo "big file (merged): ${BIGM:-<none>}"
if [ -n "${BIGM:-}" ]; then
  echo "core/stats before: $(rc_stats)"
  time rm "$BIGM"
  echo "core/stats after:  $(rc_stats)   (delta = bytes the DELETE moved)"
  echo "upper size after delete (0 expected — whiteout only, no copy-up):"
  du -sb "$UP"
  show_markers
else
  echo "!! no ~100MB file matched '$BIG_GLOB' under $MTEST — cold binary delete NOT measured"
fi

# ===========================================================================
# STEP B — design §6.1(b): copy-up timing. Fresh overlay so the big file is
# visible again (the A-BIN whiteout lived in the previous upper).
# ===========================================================================
hr "STEP B [design 6.1(b)]: copy-up timing on the ~100 MB binary"
fusermount3 -u "$MERGED" 2>/dev/null || true
mount_overlay
BIGM="$(find "$MTEST" -maxdepth 2 -type f -name "$BIG_GLOB" 2>/dev/null | head -1)"
echo "big file: ${BIGM:-<none>}"
if [ -n "${BIGM:-}" ]; then
  ls -la "$BIGM"
  bytes="$(stat -c %s "$BIGM" 2>/dev/null)"
  echo "-- 1-byte in-place edit forces full copy-up of $bytes bytes from lower --"
  time sh -c "printf 'X' | dd of=\"$BIGM\" bs=1 count=1 conv=notrunc status=none"
  echo "-- upper now holds the copied-up file: --"
  find "$UP" -type f -size +50M -exec ls -la {} \; 2>/dev/null | head
else
  echo "!! no ~100MB file matched '$BIG_GLOB' under $MTEST — copy-up NOT measured"
fi

# ===========================================================================
# STEP A-REN — design §6.1(a) "opaque-dir renames": REAL directory rename of a
# lower dir with files (§9 item 8: renames apply as DELETE+PUT).
# ===========================================================================
hr "STEP A-REN [design 6.1(a) renames]: mv a lower dir with files"
REN_SRC="$(find "$MTEST" -mindepth 2 -maxdepth 2 -type d 2>/dev/null | head -1)"
echo "rename source: ${REN_SRC:-<none>}"
if [ -n "${REN_SRC:-}" ]; then
  echo "files inside: $(find "$REN_SRC" -type f | wc -l)"
  time mv "$REN_SRC" "${REN_SRC}-renamed"
  echo "-- upper after rename (expect: whiteout on old name; opaque marker on new dir; children copied up) --"
  find "$UP" -mindepth 1 | sort | head -25
  show_markers
fi

# ===========================================================================
# STEP A — design §6.1(a): rm -rf whiteout storm + individual-file whiteouts.
# NOTE: cost is ~1 full-content fetch per cold lower file (see A-BIN), so this
# is slow on a cold cache by design — that IS the measurement.
# ===========================================================================
hr "STEP A [design 6.1(a)]: rm -rf whiteout storm (through MERGED only)"
echo "-- (A1) rm -rf a whole nested subtree --"
VICTIM_DIR="$(find "$MTEST" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | grep -v wide | head -1)"
echo "victim dir: ${VICTIM_DIR:-<none>}"
if [ -n "${VICTIM_DIR:-}" ]; then
  n_before="$(find "$VICTIM_DIR" -type f 2>/dev/null | wc -l)"
  echo "files under victim before: $n_before"
  time rm -rf "$VICTIM_DIR"
  ok "removed $VICTIM_DIR via merged view"
fi
echo "-- (A2) delete individual files from the wide dir (per-file whiteouts) --"
mapfile -t FILES < <(find "$MTEST/wide" -maxdepth 1 -type f 2>/dev/null | head -6)
for f in "${FILES[@]}"; do rm -f "$f"; done
echo "deleted ${#FILES[@]} individual files from wide/"
echo "-- (A3) marker inspection --"
show_markers

# ===========================================================================
# STEP D — design §6.1(c): external WebDAV edit while the overlay is mounted.
# Spike answer: files already read through the mount stay STALE (rclone VFS
# file cache; survives poll-interval and rc vfs/refresh); un-read files are
# fresh. The refresh op must invalidate the FILE cache, not just metadata.
# ===========================================================================
hr "STEP D [design 6.1(c)]: external WebDAV edit -> merged fresh/stale?"
PROBE="$(find "$MTEST/wide" -maxdepth 1 -type f -name '*.txt' 2>/dev/null | sort | tail -1)"
if [ -n "${PROBE:-}" ]; then
  EXT_REL="${PROBE#"$MERGED"/}"; EXT_REL="${EXT_REL#"$SUBDIR"/}"
  echo "probe (merged): $PROBE  tree-rel: $EXT_REL"
  echo "  merged BEFORE: $(head -c 80 "$PROBE" 2>/dev/null)"
  if [ -n "${EXT_EDIT_CMD:-}" ]; then
    EXT_REL="$EXT_REL" bash -c "$EXT_EDIT_CMD" || echo "  (ext edit cmd failed)"
    sleep 2
    echo "  raw    AFTER: $(head -c 80 "$TEST/$EXT_REL" 2>/dev/null)"
    echo "  merged AFTER: $(head -c 80 "$PROBE" 2>/dev/null)"
    echo "  (merged==raw => fresh; merged==old => stale, needs refresh op)"
  else
    echo "  EXT_EDIT_CMD unset — drive the WebDAV edit + re-read out of band."
  fi
fi

# ===========================================================================
# STEP E — design §6.1(e): build-like write workload, merged vs local disk.
# ===========================================================================
hr "STEP E [design 6.1(e)]: build-like write workload"
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

# ===========================================================================
# STEP F — enumeration fidelity for §6.1(a): enumerate_diff over the upper.
# COUNT ARITHMETIC: this upper accumulates steps B..E of THIS run only:
#   deleted = 1 (A1 whole-dir whiteout) + 6 (A2 files) + rename markers
#             (old-name whiteout + opaque new dir [+ any phantom `.wh..opq`
#             char node fuse-overlayfs adds inside opaque dirs])
#   present = copied-up big file + rename children + workload files.
# Run a step against a FRESH upper when a headline count must close exactly.
# ===========================================================================
hr "STEP F [fidelity of 6.1(a)]: enumerate_diff over the upperdir"
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
