# gpt-oss-vllm: Optimized Docker Image for gpt-oss-120b

Custom vLLM Docker image optimized for running `openai/gpt-oss-120b` on H100/H200 GPUs.

## Features

- **Hopper-optimized**: Built with CUDA 12.8 and Hopper architecture (sm_90) support
- **MXFP4 kernels**: Includes OpenAI's Triton kernels for efficient MoE inference
- **Tool calling support**: Includes `openai-harmony` + proper `--tool-call-parser openai` flags
- **Agent-friendly defaults**: Tuned for sequential request workloads
- **Pre-configured**: `--async-scheduling`, prefix caching, chunked prefill, auto tool choice enabled

## Quick Start

### Build locally

```bash
cd docker/gpt-oss-vllm

# Build (takes ~15-30 min depending on machine)
docker build -t gpt-oss-vllm:latest .

# Run
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    gpt-oss-vllm:latest
```

### Test the endpoint

```bash
curl http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "openai/gpt-oss-120b",
        "messages": [{"role": "user", "content": "What is 2+2?"}],
        "max_tokens": 100
    }'
```

## RunPod Deployment

### Option 1: Build and push to registry

```bash
# Build
docker build -t your-registry/gpt-oss-vllm:latest .

# Push
docker push your-registry/gpt-oss-vllm:latest
```

Then in RunPod:
1. Create new Pod with **H200** or **H100 80GB**
2. Container Image: `your-registry/gpt-oss-vllm:latest`
3. Volume: **100GB** (model is ~63GB)
4. Expose port **8000**

### Option 2: Use RunPod template with custom command

Use base image `vllm/vllm-openai:gptoss` with this Docker command:

```bash
--model openai/gpt-oss-120b \
--async-scheduling \
--enable-prefix-caching \
--enable-chunked-prefill \
--enable-auto-tool-choice \
--tool-call-parser openai \
--max-model-len 32768 \
--max-num-seqs 64 \
--max-num-batched-tokens 8192 \
--gpu-memory-utilization 0.90 \
--trust-remote-code
```

**Important**: The `--enable-auto-tool-choice --tool-call-parser openai` flags are REQUIRED for proper function/tool calling. Without them, the model outputs raw JSON instead of structured tool calls.

## Configuration

All settings can be overridden via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL` | `openai/gpt-oss-120b` | Model to serve |
| `MAX_MODEL_LEN` | `32768` | Max context length |
| `TENSOR_PARALLEL_SIZE` | `1` | Number of GPUs for tensor parallelism |
| `GPU_MEMORY_UTILIZATION` | `0.90` | Fraction of GPU memory to use |
| `MAX_NUM_SEQS` | `64` | Max concurrent sequences |
| `MAX_NUM_BATCHED_TOKENS` | `8192` | Max tokens per batch |
| `ASYNC_SCHEDULING` | `true` | Enable async CPU/GPU overlap |
| `ENABLE_PREFIX_CACHING` | `true` | Cache common prefixes |
| `ENABLE_CHUNKED_PREFILL` | `true` | Chunk large prefills |
| `ENABLE_AUTO_TOOL_CHOICE` | `true` | Enable automatic tool/function calling |
| `TOOL_CALL_PARSER` | `openai` | Parser for harmony format tool calls |
| `API_KEY` | (none) | Optional API key for auth |
| `PORT` | `8000` | Server port |

### Example: Multi-GPU setup

```bash
docker run --gpus all -p 8000:8000 --ipc=host \
    -e TENSOR_PARALLEL_SIZE=2 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    gpt-oss-vllm:latest
```

### Example: Longer context

```bash
docker run --gpus all -p 8000:8000 --ipc=host \
    -e MAX_MODEL_LEN=65536 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    gpt-oss-vllm:latest
```

## Performance Expectations

| Setup | Expected tok/s (single request) | Notes |
|-------|--------------------------------|-------|
| H200 TP1 | ~200-250 | Single stream decode |
| H100 TP1 | ~180-220 | Single stream decode |
| H200 TP1 + batching | ~6000+ | Multiple concurrent requests |

For agent workloads (sequential requests), single-stream decode speed is the bottleneck. The `--async-scheduling` flag provides ~5-10% improvement by overlapping CPU/GPU operations.

## Troubleshooting

### CUDA OOM on startup

Reduce `GPU_MEMORY_UTILIZATION` to `0.85` or lower `MAX_MODEL_LEN`:

```bash
-e GPU_MEMORY_UTILIZATION=0.85 -e MAX_MODEL_LEN=16384
```

### Slow first request

First request downloads and loads the model (~63GB). Subsequent requests use cached weights. Use a persistent volume on RunPod to avoid re-downloading.

### Tool calls returning raw JSON instead of function calls

If you see the model outputting raw JSON like:
```json
{"path": "", "pattern": "*"}
```

Instead of proper function calls, ensure these flags are set:
```bash
--enable-auto-tool-choice --tool-call-parser openai
```

This image includes these by default. If using the base `vllm/vllm-openai:gptoss` image, you must add them manually.

### Checking for optimized kernels

Look for these in startup logs to confirm optimizations are active:
- `Using MXFP4 Triton kernels`
- `Flash Attention 3 enabled`
- `Async scheduling enabled`
- `Tool call parser: openai` (for function calling)

## Integration with Graph-RAG Agent

Update your `.env` to point to the vLLM endpoint:

```bash
LLM_BASE_URL=http://localhost:8000/v1
OPENAI_API_KEY=dummy  # or your API_KEY if set
```

In `src/config/defaults.json`, ensure the model name matches:

```json
{
    "llm": {
        "model": "openai/gpt-oss-120b",
        "base_url": "http://localhost:8000/v1"
    }
}
```
