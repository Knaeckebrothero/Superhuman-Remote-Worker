"""Contradiction-survival probe: stored fact → superseding update → query.

    # 1. run any arm over the committed probe fixture:
    python -m eval.memory.run \
        --dataset eval/memory/fixtures/contradiction_probe.json \
        --arm eval/memory/arms/persistent_current.yaml \
        --out eval/memory/runs/contra_seam
    # 2. score it:
    python -m eval.memory.contradiction eval/memory/runs/contra_seam [--reader]

The fixture is LongMemEval-schema (so the normal runner ingests it
unchanged) with one extra key per instance the runner ignores:

    "probe": {"current_value": ..., "stale_value": ...,
              "update_session_id": ..., "original_session_id": ...}

Because the old/new values are KNOWN and chosen to be unambiguous
substrings, scoring is exact — no judge LLM involved. Two layers, which
decompose *where* a stale answer comes from (the Phase-4 acceptance
metric of docs/features/agent_memory_overhaul.md):

- **retrieval order** (always available, from ranked_sessions): was the
  update session injected at all / ranked above the original?
- **reading** (``--reader``, costs LLM calls): the judge module's reader
  answers from the captured injected context; the answer is classified
  ``current`` (contains the new value — mentioning the old one too is
  fine, that's the knowledge-update convention), ``stale`` (old value
  only), or ``miss`` (neither).
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .judge import READER_MAX_TOKENS, LLMRoute, _chat, reader_messages

logger = logging.getLogger(__name__)


def load_probe_meta(fixture_path: str) -> Dict[str, dict]:
    """question_id → probe block (current/stale values + session roles)."""
    raw = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    meta = {}
    for inst in raw:
        probe = inst.get("probe")
        if probe:
            for key in (
                "current_value",
                "stale_value",
                "update_session_id",
                "original_session_id",
            ):
                if not probe.get(key):
                    raise ValueError(
                        f"{inst.get('question_id')}: probe block missing {key}"
                    )
            meta[str(inst["question_id"])] = probe
    if not meta:
        raise ValueError(f"{fixture_path}: no instances carry a probe block")
    return meta


def classify_answer(answer: str, probe: dict) -> str:
    """current | stale | miss, by unambiguous substring (case-insensitive).

    An answer mentioning both values counts as current — same convention
    as the LongMemEval knowledge-update judge ("previous information
    along with an updated answer ... is correct").
    """
    lowered = answer.lower()
    if probe["current_value"].lower() in lowered:
        return "current"
    if probe["stale_value"].lower() in lowered:
        return "stale"
    return "miss"


def retrieval_outcome(row: dict, probe: dict) -> dict:
    """Rank positions of the update vs original sessions in the injection."""
    ranked: List[str] = row.get("ranked_sessions") or []

    def rank_of(sid: str) -> Optional[int]:
        return ranked.index(sid) + 1 if sid in ranked else None

    update_rank = rank_of(probe["update_session_id"])
    original_rank = rank_of(probe["original_session_id"])
    return {
        "update_rank": update_rank,
        "original_rank": original_rank,
        "update_injected": update_rank is not None,
        "original_injected": original_rank is not None,
        # ordering verdict: an absent session ranks below any present one
        "update_above_original": (
            update_rank is not None
            and (original_rank is None or update_rank < original_rank)
        ),
    }


def summarize_probe(rows: List[dict]) -> dict:
    """Aggregate the per-question probe rows into the survival report."""

    def rate(key: str, group: List[dict]) -> Optional[float]:
        vals = [r[key] for r in group if r.get(key) is not None]
        return round(sum(1 for v in vals if v) / len(vals), 4) if vals else None

    summary = {
        "questions": len(rows),
        "update_injected": rate("update_injected", rows),
        "original_injected": rate("original_injected", rows),
        "update_above_original": rate("update_above_original", rows),
    }
    read = [r for r in rows if r.get("reader_class")]
    if read:
        n = len(read)
        counts = {
            c: sum(1 for r in read if r["reader_class"] == c)
            for c in ("current", "stale", "miss")
        }
        summary["reader"] = {
            "n": n,
            **{f"{c}_rate": round(v / n, 4) for c, v in counts.items()},
        }
    return summary


async def run_probe(args: argparse.Namespace) -> dict:
    run_dir = Path(args.run_dir)
    results = []
    for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            results.append(json.loads(line))
    meta = load_probe_meta(args.fixture)

    reader = reader_client = None
    if args.reader:
        reader = LLMRoute.from_env("reader")
        reader_client = reader.client()

    rows = []
    for row in results:
        probe = meta.get(row["question_id"])
        if probe is None:
            logger.warning("%s: no probe block — skipped", row["question_id"])
            continue
        out = {"question_id": row["question_id"], **retrieval_outcome(row, probe)}
        if reader_client is not None:
            if "injected_context" not in row:
                raise SystemExit(
                    f"{row['question_id']} lacks injected_context — re-run "
                    "the eval with the current harness"
                )
            hypothesis = await _chat(
                reader_client,
                reader.model,
                reader_messages(row),
                temperature=0.0,
                max_tokens=READER_MAX_TOKENS,
            )
            out["hypothesis"] = hypothesis
            out["reader_class"] = classify_answer(hypothesis, probe)
        rows.append(out)

    summary = summarize_probe(rows)
    summary["rows"] = rows
    (run_dir / "contradiction_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.memory.contradiction", description=__doc__
    )
    parser.add_argument("run_dir", help="run dir of a probe-fixture eval run")
    parser.add_argument(
        "--fixture",
        default="eval/memory/fixtures/contradiction_probe.json",
        help="probe fixture the run ingested (for the value/session metadata)",
    )
    parser.add_argument(
        "--reader",
        action="store_true",
        help="also answer each question from the injected context and "
        "classify current/stale/miss (needs EVAL_READER_*/EVAL_AUX_* env)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run_probe(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
