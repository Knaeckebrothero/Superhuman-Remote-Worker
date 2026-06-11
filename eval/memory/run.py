"""Memory eval runner — ingest + score one arm over a LongMemEval file.

    python -m eval.memory.run \
        --dataset eval/memory/data/longmemeval_s.json \
        --arm eval/memory/arms/flat_verbatim.yaml \
        --limit 20 --init-db

Environment (see eval/memory/README.md):
    EVAL_VECTOR_DSN   pgvector DSN (default: dev-compose, db srw_eval)
    EMBEDDING_*       real embedding endpoint (required)
    <arm api_key_env> auxiliary-LLM key for seam-mode arms

Results stream to <out>/results.jsonl (one row per question, written as
each finishes) — reruns with the same --out and --run-id resume by
skipping already-scored question ids, so a flaky aux endpoint costs only
the in-flight questions.
"""

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import List, Optional, Set

from . import arms as arms_mod
from . import infra
from .datasets import load_longmemeval, subset_questions
from .ingest import HarnessHandles, ingest_question
from .metrics import aggregate
from .query import answer_retrieval
from .report import render_markdown

logger = logging.getLogger(__name__)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m eval.memory.run", description=__doc__
    )
    parser.add_argument("--dataset", required=True, help="LongMemEval-format JSON")
    parser.add_argument("--arm", required=True, help="arm YAML (eval/memory/arms/)")
    parser.add_argument(
        "--out", default=None, help="run dir (default: runs/<ts>_<arm>)"
    )
    parser.add_argument(
        "--run-id", default=None, help="scope namespace (default: out dir name)"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="type-stratified question cap"
    )
    parser.add_argument("--types", default=None, help="comma-separated question types")
    parser.add_argument(
        "--questions", default=None, help="comma-separated question ids"
    )
    parser.add_argument("--seed", type=int, default=0, help="subset shuffle seed")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("EVAL_VECTOR_DSN", infra.DEFAULT_DSN),
        help="eval pgvector DSN (env EVAL_VECTOR_DSN)",
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="create the eval database + apply migrations/vector first",
    )
    parser.add_argument(
        "--concurrency", type=int, default=3, help="questions in flight"
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="re-score questions already present in results.jsonl",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete each question's memory rows after scoring (keeps the "
        "eval DB small; the row keeps the ranking either way)",
    )
    return parser.parse_args(argv)


def _setup_logging(out_dir: Path) -> None:
    """INFO to stdout, everything (incl. contained seam warnings) to run.log."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO)
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    file_handler = logging.FileHandler(out_dir / "run.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.handlers = [stream, file_handler]


def _existing_question_ids(results_path: Path) -> Set[str]:
    if not results_path.exists():
        return set()
    ids = set()
    for line in results_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                ids.add(json.loads(line)["question_id"])
            except (json.JSONDecodeError, KeyError):
                logger.warning("Skipping malformed results line")
    return ids


async def _cleanup_question(db, project: uuid.UUID) -> None:
    await db.execute(
        "DELETE FROM memory_retrieval_messages WHERE memory_id IN "
        "(SELECT id FROM memories WHERE project_id = $1)",
        project,
    )
    await db.execute("DELETE FROM memories WHERE project_id = $1", project)


async def run_arm(args: argparse.Namespace) -> dict:
    arm = arms_mod.ArmSpec.from_file(args.arm)

    out_dir = Path(
        args.out
        or Path("eval/memory/runs") / f"{time.strftime('%Y%m%d_%H%M%S')}_{arm.name}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or out_dir.name
    results_path = out_dir / "results.jsonl"
    _setup_logging(out_dir)

    config = arms_mod.build_agent_config(arm)

    questions = subset_questions(
        load_longmemeval(args.dataset),
        limit=args.limit,
        types=args.types.split(",") if args.types else None,
        ids=args.questions.split(",") if args.questions else None,
        seed=args.seed,
    )
    done = set() if args.no_resume else _existing_question_ids(results_path)
    todo = [q for q in questions if q.question_id not in done]
    logger.info(
        "Run %s: arm=%s questions=%d (resumed: %d already done)",
        run_id,
        arm.name,
        len(questions),
        len(questions) - len(todo),
    )

    if args.init_db:
        await infra.ensure_database(args.dsn)
        await infra.apply_migrations(args.dsn)

    db = await infra.connect_vector_db(args.dsn)
    embedding = await infra.require_embedding_service()
    aux_llm = None
    extraction_prompt = ""
    if arm.ingestion.mode == "seam":
        aux_llm = infra.build_auxiliary_llm(config)
        extraction_prompt = infra.resolve_extraction_prompt(config)

    handles = HarnessHandles(
        config=config,
        run_id=run_id,
        db=db,
        embedding=embedding,
        aux_llm=aux_llm,
        extraction_prompt=extraction_prompt,
    )

    (out_dir / "run_meta.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "dataset": args.dataset,
                "arm": dataclasses.asdict(arm),
                "memory_config": dataclasses.asdict(config.memory),
                "auxiliary_model": config.auxiliary.model,
                "embedding_model": embedding.model,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    semaphore = asyncio.Semaphore(args.concurrency)
    write_lock = asyncio.Lock()

    async def _one(question) -> None:
        async with semaphore:
            try:
                ingest = await ingest_question(question, arm, handles)
                row = await answer_retrieval(question, arm, handles)
                row["ingest"] = dataclasses.asdict(ingest)
                if args.cleanup:
                    await _cleanup_question(
                        db, infra.project_uuid(run_id, question.question_id)
                    )
            except Exception:
                logger.exception("Question %s failed", question.question_id)
                return
            async with write_lock:
                with results_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, default=str) + "\n")
            r5 = (row.get("metrics") or {}).get("recall@5")
            logger.info(
                "%s done (recall@5=%s, stored=%s)",
                question.question_id,
                "—" if r5 is None else f"{r5:.0f}",
                row["ingest"].get("stored_memories"),
            )

    try:
        await asyncio.gather(*(_one(q) for q in todo))
    finally:
        await db.close()

    rows = []
    for line in results_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    summary = aggregate(rows)
    summary["run_dir"] = str(out_dir)
    summary["arm"] = dataclasses.asdict(arm)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    report = render_markdown(summary, title=arm.name)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    asyncio.run(run_arm(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
