# GPU Performance Comparison

Benchmark comparison of NVIDIA H100 and H200 GPUs running the `gpt-oss-120b` model with vLLM and llama.cpp inference frameworks.

## Test Configuration

| Parameter | Value |
|-----------|-------|
| Model | gpt-oss-120b (MoE, 128 experts, 4 active) |
| Quantization | mxfp4 (vLLM), GGUF mxfp4 (llama.cpp) |
| Context Length | 65,536 (llama.cpp), 131,072 (vLLM) |
| vLLM Version | 0.10.2 |
| KV Cache | q8_0 (llama.cpp), auto (vLLM) |

### GPU Specifications

| GPU | Memory | Architecture |
|-----|--------|--------------|
| NVIDIA H100 PCIe | 80 GB | Hopper |
| NVIDIA H200 | 141 GB | Hopper |

## Performance Results

### Full Comparison

| Configuration | Prompt (tok/s) | Generation (tok/s) |
|--------------|----------------|-------------------|
| H100 + llama.cpp | 1,802 | ~140 |
| H100 + vLLM | 2,131 | ~127 |
| H200 + llama.cpp | 2,048 | ~165-170 |
| H200 + vLLM | 2,808 | ~143 |

### H200 vs H100 Improvement

| Framework | Prompt Speedup | Generation Speedup |
|-----------|---------------|-------------------|
| llama.cpp | +14% | +21% |
| vLLM | +32% | +13% |

### Framework Comparison (Same GPU)

| GPU | Prompt Winner | Generation Winner |
|-----|--------------|------------------|
| H100 | vLLM (+18%) | llama.cpp (+10%) |
| H200 | vLLM (+37%) | llama.cpp (+19%) |

## Key Findings

### Throughput

- **vLLM** is faster at prompt processing (prefill) on both GPUs
- **llama.cpp** is faster at generation (decoding) on both GPUs, ~19-21% faster on H200
- **H200** provides meaningful speedups over H100, especially for prompt processing with vLLM
- The H200's extra memory (141GB vs 80GB) benefits vLLM more for prompt throughput

### Caching Behavior

**llama.cpp:**
- Uses quantized KV cache (`K=q8_0, V=q8_0`) for memory efficiency
- Checkpoint-based prompt caching with multiple checkpoints per prompt
- Cache limits: 8,192 MiB, 65,536 tokens
- More robust to prompt variations due to checkpoint-based approach
- Note: Model's Sliding Window Attention (SWA) causes frequent full re-processing

**vLLM:**
- Prefix caching enabled with automatic KV cache dtype
- GPU KV cache size: ~117,840 tokens (H100)
- Prefix cache hit rate varies: 17-97% depending on prompt patterns
- Requires exact token-by-token prefix matches for cache hits
- May experience stale cache issues with varying prompts

### Stability

llama.cpp's checkpoint-based caching proved more stable in testing, avoiding the "stale cache" issues observed with vLLM's prefix caching mechanism.

## Recommendation

| Use Case | Recommended Setup |
|----------|-------------------|
| High prompt throughput | H200 + vLLM |
| Stable long-running inference | H200 + llama.cpp |
| Cost-effective option | H100 + llama.cpp |
| Maximum generation speed | H200 + llama.cpp |

## Analysis Script

Performance metrics were extracted using `gpu_logs/analyze_gpu_logs.py`:

```bash
python gpu_logs/analyze_gpu_logs.py
```

The script parses vLLM and llama.cpp log files and extracts:
- Prompt throughput (tokens/second)
- Generation throughput (tokens/second)
- Cache hit rates (vLLM)
- Total tokens processed and timing statistics
