#!/usr/bin/env bash
set -euo pipefail

# Each worker imports the orchestrator and agent stacks, so worker memory is
# substantial. Eight workers give good throughput locally and in CI without the
# import/RAM storm that `pytest -n auto` can cause on high-core-count machines.
python_bin="${SRW_PYTHON:-python}"
dist_mode="${SRW_PYTEST_DIST:-loadfile}"

if [[ -n "${SRW_PYTEST_WORKERS:-}" ]]; then
    workers="$SRW_PYTEST_WORKERS"
else
    detected_cpus="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 8)"
    if [[ "$detected_cpus" =~ ^[0-9]+$ ]] && (( detected_cpus < 8 )); then
        workers="$detected_cpus"
    else
        workers=8
    fi
fi

if ! "$python_bin" -c 'import xdist' >/dev/null 2>&1; then
    echo "pytest-xdist is not installed for $python_bin." >&2
    echo "Install the development dependencies first:" >&2
    echo "  $python_bin -m pip install -r requirements-dev.txt" >&2
    exit 2
fi

if (( $# == 0 )); then
    set -- tests/ -x -q --tb=short
fi

exec "$python_bin" -m pytest \
    -n "$workers" \
    --dist "$dist_mode" \
    "$@"
