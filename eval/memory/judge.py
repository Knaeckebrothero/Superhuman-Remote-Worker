"""End-task accuracy: reader LLM answers from the injected context, an
LLM-judge scores the answer against gold (LongMemEval protocol).

    python -m eval.memory.judge <run_dir> [--limit N] [--concurrency 4]
    python -m eval.memory.judge <run_dir> --calibrate labels.json

Two stages per results.jsonl row (both resume-safe via judge.jsonl):

1. **Reader**: a chat model answers ``row["question"]`` given
   ``row["injected_context"]`` — the production-rendered memory block
   captured at answer time. This measures *reading*, separately from the
   retrieval metrics, because reading is its own bottleneck (brief 05:
   the same context can swing end-task accuracy by ±10 pts).
2. **Judge**: the verbatim LongMemEval ``evaluate_qa.py`` prompts
   (per question type, abstention variant for ``*_abs``), temperature 0,
   max_tokens 10, verdict = ``"yes" in response.lower()``. Using the
   paper's own judge protocol inherits its published human-agreement
   calibration; ``--calibrate`` then measures OUR judge model's
   agreement against a hand-labelled slice (target >97 %).

Routing (never key material in files): ``EVAL_READER_MODEL`` /
``EVAL_READER_BASE_URL`` / ``EVAL_READER_API_KEY`` for the reader,
``EVAL_JUDGE_*`` for the judge; both fall back to ``EVAL_AUX_*``.

Scores are written next to the run: ``judge.jsonl`` (per question) and
``judge_summary.json`` (accuracy overall / by type / abstention
sub-score). ``results.jsonl`` is never mutated.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Verbatim from LongMemEval src/evaluation/evaluate_qa.py (get_anscheck_prompt).
_ANSCHECK_DEFAULT = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, you should also "
    "answer yes. If the response only contains a subset of the information "
    "required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: "
    "{}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or "
    "no only."
)
_ANSCHECK_TEMPORAL = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, you should also "
    "answer yes. If the response only contains a subset of the information "
    "required by the answer, answer no. In addition, do not penalize off-by-one "
    "errors for the number of days. If the question asks for the number of "
    "days/weeks/months, etc., and the model makes off-by-one errors (e.g., "
    "predicting 19 days when the answer is 18), the model's response is still "
    "correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: "
    "{}\n\nIs the model response correct? Answer yes or no only."
)
_ANSCHECK_KNOWLEDGE_UPDATE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response contains some previous information along with "
    "an updated answer, the response should be considered as correct as long as "
    "the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect "
    "Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer "
    "yes or no only."
)
_ANSCHECK_PREFERENCE = (
    "I will give you a question, a rubric for desired personalized response, "
    "and a response from a model. Please answer yes if the response satisfies "
    "the desired response. Otherwise, answer no. The model does not need to "
    "reflect all the points in the rubric. The response is correct as long as "
    "it recalls and utilizes the user's personal information "
    "correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the "
    "model response correct? Answer yes or no only."
)
_ANSCHECK_ABSTENTION = (
    "I will give you an unanswerable question, an explanation, and a response "
    "from a model. Please answer yes if the model correctly identifies the "
    "question as unanswerable. The model could say that the information is "
    "incomplete, or some other information is given but the asked information "
    "is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes "
    "the model correctly identify the question as unanswerable? Answer yes or "
    "no only."
)

#: Our reader frame — the harness analogue of a production agent reading
#: its injected memory block. Fixed across arms so end-task numbers are
#: comparable; the memory block is the only thing that varies.
READER_SYSTEM = (
    "You are a personal assistant. Below are memories retrieved from your "
    "previous conversations with the user. Answer the user's question using "
    "only these memories. If the memories do not contain the information "
    "needed, say that you do not have that information.\n\n{context}"
)


def anscheck_prompt(
    question_type: str,
    question: str,
    answer: str,
    response: str,
    abstention: bool,
) -> str:
    """LongMemEval's get_anscheck_prompt, ported 1:1."""
    if abstention:
        template = _ANSCHECK_ABSTENTION
    elif question_type in (
        "single-session-user",
        "single-session-assistant",
        "multi-session",
    ):
        template = _ANSCHECK_DEFAULT
    elif question_type == "temporal-reasoning":
        template = _ANSCHECK_TEMPORAL
    elif question_type == "knowledge-update":
        template = _ANSCHECK_KNOWLEDGE_UPDATE
    elif question_type == "single-session-preference":
        template = _ANSCHECK_PREFERENCE
    else:
        raise ValueError(f"unknown question type: {question_type}")
    return template.format(question, answer, response)


def reader_messages(row: dict) -> List[dict]:
    """Chat messages for the reader step over one result row."""
    question = row["question"]
    if row.get("question_date"):
        question = f"(Current date: {row['question_date']}) {question}"
    return [
        {
            "role": "system",
            "content": READER_SYSTEM.format(
                context=row.get("injected_context") or "(no memories retrieved)"
            ),
        },
        {"role": "user", "content": question},
    ]


#: The paper uses max_tokens=10 (GPT-4o emits a bare yes/no). Reasoning
#: models burn that budget inside their thinking phase and return EMPTY
#: content — so give room to think and parse only the post-reasoning
#: tail. Verdict semantics are unchanged.
JUDGE_MAX_TOKENS = 2048
READER_MAX_TOKENS = 2048


def parse_verdict(judge_response: str) -> bool:
    """LongMemEval's parse ('yes' in the lowered response), applied to the
    text after any inline reasoning block so a deliberating judge's
    "...maybe yes... </think> no" can't false-positive."""
    text = judge_response
    for tag in ("</think>", "</thinking>"):
        if tag in text:
            text = text.rsplit(tag, 1)[1]
    return "yes" in text.lower()


@dataclass
class LLMRoute:
    """One chat endpoint; key comes from the environment, never a file."""

    model: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None

    @classmethod
    def from_env(cls, role: str) -> "LLMRoute":
        """EVAL_<ROLE>_* with EVAL_AUX_* fallback (the extraction model)."""
        prefix = f"EVAL_{role.upper()}"

        def pick(suffix: str) -> Optional[str]:
            return os.environ.get(f"{prefix}_{suffix}") or os.environ.get(
                f"EVAL_AUX_{suffix}"
            )

        model = pick("MODEL")
        if not model:
            raise RuntimeError(
                f"no model for {role}: set {prefix}_MODEL (or EVAL_AUX_MODEL)"
            )
        return cls(model=model, base_url=pick("BASE_URL"), api_key=pick("API_KEY"))

    def client(self):
        from openai import AsyncOpenAI

        if not self.api_key:
            raise RuntimeError(f"no API key in environment for model {self.model}")
        return AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)


async def _chat(client, model: str, messages: List[dict], **kwargs) -> str:
    response = await client.chat.completions.create(
        model=model, messages=messages, **kwargs
    )
    return (response.choices[0].message.content or "").strip()


async def judge_row(
    row: dict,
    reader_client,
    judge_client,
    reader: LLMRoute,
    judge: LLMRoute,
) -> dict:
    """Reader answer + judge verdict for one results.jsonl row."""
    hypothesis = await _chat(
        reader_client,
        reader.model,
        reader_messages(row),
        temperature=0.0,
        max_tokens=READER_MAX_TOKENS,
    )
    prompt = anscheck_prompt(
        row["question_type"],
        row["question"],
        row["answer"],
        hypothesis,
        abstention=bool(row.get("is_abstention")),
    )
    judge_response = await _chat(
        judge_client,
        judge.model,
        [{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=JUDGE_MAX_TOKENS,
    )
    return {
        "question_id": row["question_id"],
        "question_type": row["question_type"],
        "is_abstention": bool(row.get("is_abstention")),
        "hypothesis": hypothesis,
        "judge_response": judge_response,
        "correct": parse_verdict(judge_response),
        "reader_model": reader.model,
        "judge_model": judge.model,
    }


def summarize_judgements(rows: Sequence[dict]) -> dict:
    """Accuracy overall / by type / abstention sub-score (paper layout)."""

    def acc(group: Sequence[dict]) -> Optional[float]:
        return (
            round(sum(1 for r in group if r["correct"]) / len(group), 4)
            if group
            else None
        )

    by_type: Dict[str, List[dict]] = {}
    for r in rows:
        by_type.setdefault(r["question_type"], []).append(r)
    abstentions = [r for r in rows if r["is_abstention"]]
    answerable = [r for r in rows if not r["is_abstention"]]
    return {
        "questions": len(rows),
        "accuracy": acc(list(rows)),
        "accuracy_answerable": acc(answerable),
        "abstention_score": acc(abstentions),
        "abstentions": len(abstentions),
        "by_type": {
            t: {"accuracy": acc(g), "n": len(g)} for t, g in sorted(by_type.items())
        },
    }


def calibrate(judgements: Sequence[dict], labels: Dict[str, bool]) -> dict:
    """Judge-vs-human agreement on the hand-labelled slice."""
    overlap = [j for j in judgements if j["question_id"] in labels]
    disagreements = [
        j["question_id"] for j in overlap if j["correct"] != labels[j["question_id"]]
    ]
    return {
        "labelled": len(labels),
        "overlap": len(overlap),
        "agreement": (
            round(1 - len(disagreements) / len(overlap), 4) if overlap else None
        ),
        "disagreements": disagreements,
    }


def _load_jsonl(path: Path) -> List[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


async def run_judge(args: argparse.Namespace) -> dict:
    run_dir = Path(args.run_dir)
    results = _load_jsonl(run_dir / "results.jsonl")
    if not results:
        raise SystemExit(f"no results.jsonl rows under {run_dir}")
    missing_ctx = [r["question_id"] for r in results if "injected_context" not in r]
    if missing_ctx:
        raise SystemExit(
            f"{len(missing_ctx)} rows lack injected_context (old runner?) — "
            "re-run the eval with the current harness"
        )

    judge_path = run_dir / "judge.jsonl"
    done = {r["question_id"] for r in _load_jsonl(judge_path)}
    todo = [r for r in results if r["question_id"] not in done]
    if args.limit is not None:
        todo = todo[: args.limit]
    logger.info("Judging %d questions (%d already done)", len(todo), len(done))

    reader = LLMRoute.from_env("reader")
    judge = LLMRoute.from_env("judge")
    reader_client = reader.client()
    judge_client = judge.client()

    semaphore = asyncio.Semaphore(args.concurrency)
    write_lock = asyncio.Lock()

    async def _one(row: dict) -> None:
        async with semaphore:
            try:
                out = await judge_row(row, reader_client, judge_client, reader, judge)
            except Exception:
                logger.exception("Judge failed for %s", row["question_id"])
                return
        async with write_lock:
            with judge_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(out) + "\n")
        logger.info(
            "%s: %s", out["question_id"], "correct" if out["correct"] else "wrong"
        )

    await asyncio.gather(*(_one(r) for r in todo))

    judgements = _load_jsonl(judge_path)
    summary = summarize_judgements(judgements)
    summary["reader_model"] = reader.model
    summary["judge_model"] = judge.model
    (run_dir / "judge_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return summary


def judge_file_slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


async def rejudge(args: argparse.Namespace) -> dict:
    """Re-verdict EXISTING hypotheses with the current EVAL_JUDGE_MODEL.

    The calibration workflow: hold the reader's answers fixed (from
    judge.jsonl), swap only the judge, compare each judge against the
    same hand labels. Writes judge_<model-slug>.jsonl + its summary;
    never touches judge.jsonl.
    """
    run_dir = Path(args.run_dir)
    base = _load_jsonl(run_dir / "judge.jsonl")
    if not base:
        raise SystemExit(f"no judge.jsonl under {run_dir} — run the judge first")
    gold = {r["question_id"]: r for r in _load_jsonl(run_dir / "results.jsonl")}

    judge = LLMRoute.from_env("judge")
    judge_client = judge.client()
    out_path = run_dir / f"judge_{judge_file_slug(judge.model)}.jsonl"
    done = {r["question_id"] for r in _load_jsonl(out_path)}
    todo = [r for r in base if r["question_id"] not in done]
    logger.info(
        "Re-judging %d hypotheses with %s (%d already done)",
        len(todo),
        judge.model,
        len(done),
    )

    semaphore = asyncio.Semaphore(args.concurrency)
    write_lock = asyncio.Lock()

    async def _one(row: dict) -> None:
        async with semaphore:
            try:
                res = gold[row["question_id"]]
                prompt = anscheck_prompt(
                    row["question_type"],
                    res["question"],
                    res["answer"],
                    row["hypothesis"],
                    abstention=bool(row.get("is_abstention")),
                )
                judge_response = await _chat(
                    judge_client,
                    judge.model,
                    [{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=JUDGE_MAX_TOKENS,
                )
            except Exception:
                logger.exception("Re-judge failed for %s", row["question_id"])
                return
        out = {
            **{k: row[k] for k in ("question_id", "question_type", "is_abstention")},
            "hypothesis": row["hypothesis"],
            "judge_response": judge_response,
            "correct": parse_verdict(judge_response),
            "reader_model": row.get("reader_model"),
            "judge_model": judge.model,
        }
        async with write_lock:
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(out) + "\n")

    await asyncio.gather(*(_one(r) for r in todo))

    judgements = _load_jsonl(out_path)
    summary = summarize_judgements(judgements)
    summary["judge_model"] = judge.model
    summary["judge_file"] = out_path.name
    (run_dir / f"judge_summary_{judge_file_slug(judge.model)}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.memory.judge", description=__doc__
    )
    parser.add_argument("run_dir", help="run directory containing results.jsonl")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--calibrate",
        default=None,
        help="JSON file {question_id: true|false} of human labels; prints "
        "judge-vs-human agreement instead of judging",
    )
    parser.add_argument(
        "--judge-file",
        default="judge.jsonl",
        help="verdict file for --calibrate (e.g. judge_<model>.jsonl)",
    )
    parser.add_argument(
        "--re-judge",
        action="store_true",
        help="re-verdict the existing judge.jsonl hypotheses with the "
        "current EVAL_JUDGE_MODEL into judge_<model>.jsonl",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    if args.calibrate:
        judgements = _load_jsonl(Path(args.run_dir) / args.judge_file)
        labels = json.loads(Path(args.calibrate).read_text(encoding="utf-8"))
        print(json.dumps(calibrate(judgements, labels), indent=2))
        return 0

    if args.re_judge:
        asyncio.run(rejudge(args))
        return 0

    asyncio.run(run_judge(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
