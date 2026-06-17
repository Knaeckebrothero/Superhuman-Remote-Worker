"""Lifecycle / supersede scorer: ingest → assert the bi-temporal DB state.

Where ``contradiction.py`` scores the *observable* outcome (did the stale value
stop being read), this scores the *mechanism*: after an arm ingests
``fixtures/lifecycle_probe.json`` through the Phase-4 write path, it reads the
``memories`` table per question scope and checks which rows are valid
(``valid_to IS NULL``) and which were retired.

    # ingest the fixture with the verdict-ON arm (NOT --cleanup — rows must stay):
    python -m eval.memory.run \
        --dataset eval/memory/fixtures/lifecycle_probe.json \
        --arm eval/memory/data/contra_complete_verdict_k3d.yaml \
        --out eval/memory/runs/lifecycle_verdict
    # then score the bi-temporal state:
    python -m eval.memory.lifecycle eval/memory/runs/lifecycle_verdict

Scoring is exact (substring over ``content``/``summary``/``keywords``, no judge
LLM). Each ``lifecycle`` label lists distinctive values and the runner scopes
every question into its own project (``project_uuid(run_id, question_id)``), so
"the rows for this case" is an unambiguous query.

The four verdicts collapse into two checks:

- ``expect_retired`` (UPDATE / chain): the value must sit in a retired row and
  in NO valid row — otherwise ``missed_retire`` (supersede never fired).
- ``expect_valid`` / ``expect_unique`` (everything that must survive): the value
  must sit in a valid row. If it survived only in a retired row that is
  ``false_retired`` — the over-retiring bug the contradiction probe is blind to.
  ``expect_unique`` additionally fails ``duplicate`` if it spans >1 valid row
  (a NOOP that wrongly spawned a twin).

A value absent from every row is ``not_extracted`` — an extraction/recall gap,
not a lifecycle fault, so it is reported separately and never counted as a
lifecycle failure (only as "incomplete" coverage for that case).
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import infra

logger = logging.getLogger(__name__)

# Per-value outcomes.
OK = "ok"
FALSE_RETIRED = "false_retired"  # should-survive value found only in a retired row
MISSED_RETIRE = "missed_retire"  # should-retire value still served by a valid row
DUPLICATE = "duplicate"  # unique value spans more than one valid row
NOT_EXTRACTED = "not_extracted"  # value never captured at all (extraction gap)

#: Outcomes that are genuine lifecycle faults (vs. extraction coverage gaps).
FAULTS = (FALSE_RETIRED, MISSED_RETIRE, DUPLICATE)


def load_lifecycle_meta(fixture_path: str) -> Dict[str, dict]:
    """question_id → lifecycle block (category + expectation lists)."""
    raw = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    meta = {}
    for inst in raw:
        block = inst.get("lifecycle")
        if not block:
            continue
        block.setdefault("expect_valid", [])
        block.setdefault("expect_retired", [])
        block.setdefault("expect_unique", [])
        meta[str(inst["question_id"])] = block
    if not meta:
        raise ValueError(f"{fixture_path}: no instances carry a lifecycle block")
    return meta


def _hits(rows: List[dict], needle: str) -> tuple:
    """(valid rows, retired rows) whose text contains ``needle`` (ci)."""
    n = needle.lower()
    valid = [r for r in rows if n in r["text"] and not r["retired"]]
    retired = [r for r in rows if n in r["text"] and r["retired"]]
    return valid, retired


def _classify_survive(rows: List[dict], value: str) -> str:
    valid, retired = _hits(rows, value)
    if valid:
        return OK
    if retired:
        return FALSE_RETIRED
    return NOT_EXTRACTED


def _classify_retire(rows: List[dict], value: str) -> str:
    valid, retired = _hits(rows, value)
    if valid:
        return MISSED_RETIRE  # still served somewhere → supersede didn't take
    if retired:
        return OK
    return NOT_EXTRACTED


def _classify_unique(rows: List[dict], value: str) -> str:
    valid, retired = _hits(rows, value)
    if not valid:
        return FALSE_RETIRED if retired else NOT_EXTRACTED
    return OK if len(valid) == 1 else DUPLICATE


def classify_case(
    category: str,
    rows: List[dict],
    expect_valid: List[str],
    expect_retired: List[str],
    expect_unique: List[str],
) -> dict:
    """Pure per-case verdict from the case's rows. Unit-tested directly.

    ``rows`` is a list of ``{"text": <lowercased content+summary+keywords>,
    "retired": <bool>}``. Returns per-value outcomes plus a case ``status`` of
    ``pass`` (all expectations met), ``fail`` (a lifecycle fault), ``incomplete``
    (only extraction gaps, no fault) or ``no_rows`` (nothing ingested).
    """
    values = []
    for v in expect_valid:
        values.append(
            {"value": v, "role": "valid", "outcome": _classify_survive(rows, v)}
        )
    for v in expect_retired:
        values.append(
            {"value": v, "role": "retired", "outcome": _classify_retire(rows, v)}
        )
    for v in expect_unique:
        values.append(
            {"value": v, "role": "unique", "outcome": _classify_unique(rows, v)}
        )

    outcomes = [v["outcome"] for v in values]
    if not rows:
        status = "no_rows"
    elif any(o in FAULTS for o in outcomes):
        status = "fail"
    elif any(o == NOT_EXTRACTED for o in outcomes):
        status = "incomplete"
    else:
        status = "pass"

    return {
        "category": category,
        "status": status,
        "values": values,
        "rows_valid": sum(1 for r in rows if not r["retired"]),
        "rows_retired": sum(1 for r in rows if r["retired"]),
    }


def summarize(case_rows: List[dict]) -> dict:
    """Aggregate per-case verdicts into headline lifecycle metrics."""

    def _rate(num: int, denom: int) -> Optional[float]:
        return round(num / denom, 4) if denom else None

    # Headline: of every value that SHOULD survive and was actually extracted,
    # how many got wrongly retired (the over-retiring signal); and of every
    # value that SHOULD retire and was extracted, how many supersede missed.
    survive_assessable = false_retired = 0
    retire_assessable = missed = 0
    unique_assessable = dup = 0
    not_extracted = 0

    by_cat: Dict[str, Dict[str, int]] = {}
    for case in case_rows:
        cat = by_cat.setdefault(
            case["category"], {"pass": 0, "fail": 0, "incomplete": 0, "no_rows": 0}
        )
        cat[case["status"]] += 1
        for v in case["values"]:
            o = v["outcome"]
            if o == NOT_EXTRACTED:
                not_extracted += 1
                continue
            if v["role"] == "retired":
                retire_assessable += 1
                missed += o == MISSED_RETIRE
            elif v["role"] == "unique":
                unique_assessable += 1
                survive_assessable += 1
                dup += o == DUPLICATE
                false_retired += o == FALSE_RETIRED
            else:  # valid
                survive_assessable += 1
                false_retired += o == FALSE_RETIRED

    statuses = [c["status"] for c in case_rows]
    return {
        "cases": len(case_rows),
        "passed": sum(s == "pass" for s in statuses),
        "failed": sum(s == "fail" for s in statuses),
        "incomplete": sum(s == "incomplete" for s in statuses),
        "no_rows": sum(s == "no_rows" for s in statuses),
        # The two numbers that decide review_floor / verdict tuning:
        "false_retire_rate": _rate(false_retired, survive_assessable),
        "missed_retire_rate": _rate(missed, retire_assessable),
        "duplicate_rate": _rate(dup, unique_assessable),
        "false_retired_n": false_retired,
        "survive_assessable_n": survive_assessable,
        "missed_retire_n": missed,
        "retire_assessable_n": retire_assessable,
        "not_extracted_n": not_extracted,
        "by_category": by_cat,
    }


async def _fetch_case_rows(conn, project_id) -> List[dict]:
    """All memory rows in a project scope, as {text, retired} for matching."""
    records = await conn.fetch(
        "SELECT content, summary, keywords, valid_to FROM memories "
        "WHERE project_id = $1",
        project_id,
    )
    rows = []
    for r in records:
        parts = [r["content"] or "", r["summary"] or "", " ".join(r["keywords"] or [])]
        rows.append(
            {"text": " ".join(parts).lower(), "retired": r["valid_to"] is not None}
        )
    return rows


async def run_lifecycle(args: argparse.Namespace) -> dict:
    import asyncpg

    run_dir = Path(args.run_dir)
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    run_id = meta["run_id"]
    fixture = args.fixture or meta.get("dataset")
    if not fixture:
        raise SystemExit("no fixture: pass --fixture or run a dataset-bearing run")
    labels = load_lifecycle_meta(fixture)

    conn = await asyncpg.connect(args.dsn)
    try:
        case_rows = []
        for qid, block in labels.items():
            project = infra.project_uuid(run_id, qid)
            rows = await _fetch_case_rows(conn, project)
            verdict = classify_case(
                block["category"],
                rows,
                block["expect_valid"],
                block["expect_retired"],
                block["expect_unique"],
            )
            verdict["question_id"] = qid
            case_rows.append(verdict)
    finally:
        await conn.close()

    summary = summarize(case_rows)
    if summary["no_rows"] == summary["cases"]:
        logger.warning(
            "Every case scope is empty — was the ingest run made with "
            "--cleanup, or against a different --dsn / run_id?"
        )
    summary["cases_detail"] = case_rows
    (run_dir / "lifecycle_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    headline = {k: v for k, v in summary.items() if k != "cases_detail"}
    print(json.dumps(headline, indent=2))
    if args.verbose:
        for c in case_rows:
            if c["status"] in ("fail", "incomplete", "no_rows"):
                bad = [v for v in c["values"] if v["outcome"] != OK]
                print(f"  {c['status']:10s} {c['question_id']:22s} {bad}")
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.memory.lifecycle", description=__doc__
    )
    parser.add_argument("run_dir", help="run dir of a lifecycle-fixture ingest run")
    parser.add_argument(
        "--fixture",
        default=None,
        help="lifecycle fixture (default: the run's own dataset from run_meta.json)",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("EVAL_VECTOR_DSN", infra.DEFAULT_DSN),
        help="eval pgvector DSN (env EVAL_VECTOR_DSN)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="list each non-passing case's bad values"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run_lifecycle(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
