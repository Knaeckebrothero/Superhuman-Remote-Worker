#!/usr/bin/env python3
"""
GPU Performance Log Analyzer

Extracts and compares performance metrics from GPU inference logs
for different GPUs (H100, H200) and frameworks (vLLM, llama.cpp).
"""

import re
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LogMetrics:
    """Container for extracted log metrics."""
    file_name: str
    gpu_name: str = ""
    gpu_memory_gb: float = 0.0
    framework: str = ""
    model: str = ""

    # vLLM metrics (averages from periodic logging)
    vllm_prompt_throughputs: list[float] = field(default_factory=list)
    vllm_generation_throughputs: list[float] = field(default_factory=list)
    vllm_prefix_cache_hits: list[float] = field(default_factory=list)

    # llama.cpp metrics (per-request)
    llamacpp_prompt_tokens_per_sec: list[float] = field(default_factory=list)
    llamacpp_total_times_ms: list[float] = field(default_factory=list)
    llamacpp_total_tokens: list[int] = field(default_factory=list)


def parse_vllm_log(file_path: Path) -> LogMetrics:
    """Parse vLLM log file and extract metrics."""
    metrics = LogMetrics(file_name=file_path.name)

    with open(file_path) as f:
        content = f.read()

    # Detect framework
    if "vLLM" in content:
        metrics.framework = "vLLM"
        version_match = re.search(r"vLLM.*?v([\d.]+)", content)
        if version_match:
            metrics.framework = f"vLLM v{version_match.group(1)}"

    # Extract GPU info - try multiple patterns
    gpu_match = re.search(r"Device \d+: (NVIDIA [^,]+)", content)
    if gpu_match:
        metrics.gpu_name = gpu_match.group(1).strip()

    # Infer GPU from memory size if not found
    if not metrics.gpu_name:
        if "140.4GB" in content or "140GB" in content:
            metrics.gpu_name = "NVIDIA H200"
        elif "79.6GB" in content or "80GB" in content:
            metrics.gpu_name = "NVIDIA H100"

    # Extract GPU memory from loading progress
    mem_match = re.search(r"(\d+\.\d+)GB / (\d+\.\d+)GB", content)
    if mem_match:
        metrics.gpu_memory_gb = float(mem_match.group(2))

    # Extract model name
    model_match = re.search(r"Model:\s+(\S+)", content)
    if model_match:
        metrics.model = model_match.group(1)

    # Extract throughput metrics from periodic logging
    # Pattern: Avg prompt throughput: X tokens/s, Avg generation throughput: Y tokens/s
    throughput_pattern = re.compile(
        r"Avg prompt throughput:\s*([\d.]+)\s*tokens/s.*?"
        r"Avg generation throughput:\s*([\d.]+)\s*tokens/s.*?"
        r"Prefix cache hit rate:\s*([\d.]+)%"
    )

    for match in throughput_pattern.finditer(content):
        prompt_tps = float(match.group(1))
        gen_tps = float(match.group(2))
        cache_hit = float(match.group(3))

        # Skip zero values (no activity during that interval)
        if prompt_tps > 0:
            metrics.vllm_prompt_throughputs.append(prompt_tps)
        if gen_tps > 0:
            metrics.vllm_generation_throughputs.append(gen_tps)
        metrics.vllm_prefix_cache_hits.append(cache_hit)

    return metrics


def parse_llamacpp_log(file_path: Path) -> LogMetrics:
    """Parse llama.cpp log file and extract metrics."""
    metrics = LogMetrics(file_name=file_path.name)
    metrics.framework = "llama.cpp"

    with open(file_path) as f:
        content = f.read()

    # Extract GPU info
    gpu_match = re.search(r"Device \d+: (NVIDIA [^,]+)", content)
    if gpu_match:
        metrics.gpu_name = gpu_match.group(1).strip()

    # Extract GPU memory
    mem_match = re.search(r"GPU Memory:\s+(\d+)GB", content)
    if mem_match:
        metrics.gpu_memory_gb = float(mem_match.group(1))

    # Extract model name
    model_match = re.search(r"Model \(GGUF\):\s+(\S+)", content)
    if model_match:
        metrics.model = model_match.group(1)

    # Extract prompt eval metrics
    # Pattern: prompt eval time = X ms / Y tokens (Z ms per token, W tokens per second)
    prompt_pattern = re.compile(
        r"prompt eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens\s*"
        r"\(\s*([\d.]+)\s*ms per token,\s*([\d.]+)\s*tokens per second\)"
    )

    for match in prompt_pattern.finditer(content):
        tps = float(match.group(4))
        if tps > 0:
            metrics.llamacpp_prompt_tokens_per_sec.append(tps)

    # Extract total time metrics
    # Pattern: total time = X ms / Y tokens
    total_pattern = re.compile(r"total time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens")

    for match in total_pattern.finditer(content):
        time_ms = float(match.group(1))
        tokens = int(match.group(2))
        if time_ms > 0 and tokens > 0:
            metrics.llamacpp_total_times_ms.append(time_ms)
            metrics.llamacpp_total_tokens.append(tokens)

    return metrics


def calculate_stats(values: list[float]) -> dict:
    """Calculate statistics for a list of values."""
    if not values:
        return {"min": 0, "max": 0, "avg": 0, "median": 0, "count": 0}

    sorted_vals = sorted(values)
    n = len(sorted_vals)
    median = sorted_vals[n // 2] if n % 2 == 1 else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2

    return {
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values),
        "median": median,
        "count": len(values)
    }


def format_stats(stats: dict, unit: str = "") -> str:
    """Format statistics for display."""
    if stats["count"] == 0:
        return "N/A"
    return (
        f"avg={stats['avg']:.1f}{unit}, "
        f"median={stats['median']:.1f}{unit}, "
        f"min={stats['min']:.1f}{unit}, "
        f"max={stats['max']:.1f}{unit} "
        f"(n={stats['count']})"
    )


def print_metrics(metrics: LogMetrics) -> None:
    """Print formatted metrics for a log file."""
    print(f"\n{'='*70}")
    print(f"File: {metrics.file_name}")
    print(f"{'='*70}")
    print(f"  GPU:        {metrics.gpu_name or 'Unknown'}")
    print(f"  GPU Memory: {metrics.gpu_memory_gb:.1f} GB")
    print(f"  Framework:  {metrics.framework}")
    print(f"  Model:      {metrics.model or 'Unknown'}")

    if metrics.framework.startswith("vLLM"):
        print("\n  vLLM Throughput Metrics:")

        prompt_stats = calculate_stats(metrics.vllm_prompt_throughputs)
        print(f"    Prompt throughput:     {format_stats(prompt_stats, ' tok/s')}")

        gen_stats = calculate_stats(metrics.vllm_generation_throughputs)
        print(f"    Generation throughput: {format_stats(gen_stats, ' tok/s')}")

        cache_stats = calculate_stats(metrics.vllm_prefix_cache_hits)
        print(f"    Prefix cache hit:      {format_stats(cache_stats, '%')}")

    elif metrics.framework == "llama.cpp":
        print("\n  llama.cpp Metrics:")

        prompt_stats = calculate_stats(metrics.llamacpp_prompt_tokens_per_sec)
        print(f"    Prompt throughput:     {format_stats(prompt_stats, ' tok/s')}")

        # Calculate effective throughput from total time/tokens
        if metrics.llamacpp_total_times_ms and metrics.llamacpp_total_tokens:
            effective_tps = [
                (tokens / time_ms) * 1000
                for tokens, time_ms in zip(
                    metrics.llamacpp_total_tokens,
                    metrics.llamacpp_total_times_ms
                )
            ]
            eff_stats = calculate_stats(effective_tps)
            print(f"    Effective throughput:  {format_stats(eff_stats, ' tok/s')}")

            total_tokens = sum(metrics.llamacpp_total_tokens)
            total_time_s = sum(metrics.llamacpp_total_times_ms) / 1000
            print(f"    Total tokens:          {total_tokens:,}")
            print(f"    Total time:            {total_time_s:.1f}s")
            print(f"    Overall throughput:    {total_tokens / total_time_s:.1f} tok/s")


def print_comparison(all_metrics: list[LogMetrics]) -> None:
    """Print a comparison table of all configurations."""
    print(f"\n{'='*70}")
    print("COMPARISON SUMMARY")
    print(f"{'='*70}")

    print(f"\n{'Configuration':<35} {'Prompt (tok/s)':<18} {'Generation (tok/s)':<18}")
    print(f"{'-'*35} {'-'*18} {'-'*18}")

    for m in all_metrics:
        gpu_short = m.gpu_name.replace("NVIDIA ", "").replace(" PCIe", "")
        fw_short = "vLLM" if m.framework.startswith("vLLM") else "llama.cpp"
        config = f"{gpu_short} + {fw_short}"

        if m.framework.startswith("vLLM"):
            prompt_stats = calculate_stats(m.vllm_prompt_throughputs)
            gen_stats = calculate_stats(m.vllm_generation_throughputs)
            prompt_str = f"{prompt_stats['avg']:.0f}" if prompt_stats['count'] else "N/A"
            gen_str = f"{gen_stats['avg']:.0f}" if gen_stats['count'] else "N/A"
        else:
            prompt_stats = calculate_stats(m.llamacpp_prompt_tokens_per_sec)
            prompt_str = f"{prompt_stats['avg']:.0f}" if prompt_stats['count'] else "N/A"

            # For llama.cpp, generation is part of total throughput
            if m.llamacpp_total_times_ms:
                effective_tps = [
                    (t / ms) * 1000
                    for t, ms in zip(m.llamacpp_total_tokens, m.llamacpp_total_times_ms)
                ]
                gen_str = f"{sum(effective_tps)/len(effective_tps):.0f} (eff)"
            else:
                gen_str = "N/A"

        print(f"{config:<35} {prompt_str:<18} {gen_str:<18}")

    print(f"\nNote: 'eff' = effective throughput (total tokens / total time)")
    print(f"      vLLM metrics are 10-second rolling averages")
    print(f"      llama.cpp metrics are per-request measurements")


def main():
    """Main entry point."""
    # Determine log directory
    if len(sys.argv) > 1:
        log_dir = Path(sys.argv[1])
    else:
        log_dir = Path(__file__).parent

    if not log_dir.exists():
        print(f"Error: Directory not found: {log_dir}")
        sys.exit(1)

    print(f"Analyzing GPU logs in: {log_dir}")

    all_metrics = []

    # Find and parse all log files
    for log_file in sorted(log_dir.glob("*.txt")):
        print(f"\nProcessing: {log_file.name}...")

        # Read first few lines to detect framework
        with open(log_file) as f:
            header = f.read(5000)

        if "gpt-oss-llamacpp" in header or "llama.cpp" in header:
            metrics = parse_llamacpp_log(log_file)
        elif "vLLM" in header or "gpt-oss-vllm" in header:
            metrics = parse_vllm_log(log_file)
        else:
            print(f"  Skipping (unknown format)")
            continue

        all_metrics.append(metrics)
        print_metrics(metrics)

    if len(all_metrics) > 1:
        print_comparison(all_metrics)


if __name__ == "__main__":
    main()
