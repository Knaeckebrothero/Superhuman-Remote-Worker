"""Result aggregation + markdown rendering + arm-vs-arm comparison.

Usage:
    python -m eval.memory.report <run_dir>            # (re)render a run
    python -m eval.memory.report <run_a> <run_b>      # delta table (b - a)
"""

import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .metrics import aggregate

#: Headline columns for the markdown tables (others stay in summary.json).
HEADLINE = ("recall@5", "ndcg@5", "coverage@5", "recall@10", "first_hit_rank")


def load_rows(run_dir: str) -> List[dict]:
    path = Path(run_dir) / "results.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def summarize(run_dir: str) -> dict:
    summary = aggregate(load_rows(run_dir))
    summary["run_dir"] = str(run_dir)
    meta_path = Path(run_dir) / "run_meta.json"
    if meta_path.exists():
        summary["arm"] = json.loads(meta_path.read_text(encoding="utf-8")).get("arm")
    return summary


def _fmt(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.3f}"


def _metric_row(label: str, metrics: dict, columns: Sequence[str], n: int) -> str:
    cells = " | ".join(_fmt(metrics.get(c)) for c in columns)
    return f"| {label} | {cells} | {n} |"


def render_markdown(summary: dict, title: str = "") -> str:
    columns = [c for c in HEADLINE if c in summary["overall"]]
    if not columns:  # fall back to whatever was scored
        columns = [c for c in sorted(summary["overall"]) if "@" in c][:5]

    lines = [
        f"# Memory eval — {title or summary.get('arm', {}).get('name', 'run')}",
        "",
        f"Questions: {summary['questions']} "
        f"(abstentions excluded from retrieval metrics: {summary['abstentions']})",
        "",
        "| slice | " + " | ".join(columns) + " | n |",
        "|---|" + "---|" * (len(columns) + 1),
        _metric_row("**overall**", summary["overall"], columns, summary["questions"]),
    ]
    for qtype, group in summary.get("by_type", {}).items():
        lines.append(_metric_row(qtype, group, columns, group.get("questions", 0)))

    if summary.get("cost"):
        lines += ["", "## Cost (mean per question)", ""]
        for name, value in summary["cost"].items():
            lines.append(f"- {name}: {value}")
    return "\n".join(lines) + "\n"


def render_comparison(summary_a: dict, summary_b: dict) -> str:
    """Delta table: arm B minus arm A, the Phase-2 'A/B two configs' output."""
    name_a = summary_a.get("arm", {}).get("name") or summary_a.get("run_dir", "A")
    name_b = summary_b.get("arm", {}).get("name") or summary_b.get("run_dir", "B")
    columns = [
        c for c in HEADLINE if c in summary_a["overall"] and c in summary_b["overall"]
    ]

    def _delta_row(label: str, ma: dict, mb: dict) -> str:
        cells = []
        for c in columns:
            a, b = ma.get(c), mb.get(c)
            if a is None or b is None:
                cells.append("—")
            else:
                cells.append(f"{a:.3f} → {b:.3f} ({b - a:+.3f})")
        return f"| {label} | " + " | ".join(cells) + " |"

    lines = [
        f"# Memory eval delta — {name_b} vs {name_a}",
        "",
        "| slice | " + " | ".join(columns) + " |",
        "|---|" + "---|" * len(columns),
        _delta_row("**overall**", summary_a["overall"], summary_b["overall"]),
    ]
    shared_types = set(summary_a.get("by_type", {})) & set(summary_b.get("by_type", {}))
    for qtype in sorted(shared_types):
        lines.append(
            _delta_row(qtype, summary_a["by_type"][qtype], summary_b["by_type"][qtype])
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str]) -> int:
    if len(argv) == 1:
        summary = summarize(argv[0])
        print(render_markdown(summary))
    elif len(argv) == 2:
        print(render_comparison(summarize(argv[0]), summarize(argv[1])))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
