# gpt-oss-vllm: Optimized Docker Image for OpenAI gpt-oss Models

Custom vLLM Docker image optimized for running `openai/gpt-oss-120b` and `openai/gpt-oss-20b` on NVIDIA GPUs.

## Model Specifications

Both gpt-oss models use **MXFP4 native quantization** (trained in this format, not post-hoc quantized) and the **Harmony format** for tool calling.

| Model | Architecture | Total Params | Active Params | Memory Footprint |
|-------|--------------|--------------|---------------|------------------|
| gpt-oss-120b | Sparse MoE | ~117B | ~5.1B | ~63GB |
| gpt-oss-20b | Sparse MoE | ~21B | ~3.6B | ~16GB |

## GPU Compatibility

### gpt-oss-120b (~63GB)

| GPU | VRAM | Compatible | Context Length | Est. tok/s |
|-----|------|------------|----------------|------------|
| H200-141GB | 141GB | Yes | 128K | ~57 |
| H100-80GB | 80GB | Yes | 32-64K | ~37 |
| A100-80GB | 80GB | Yes | 32K | ~20 |
| L40S-48GB | 48GB | **No** | — | — |

### gpt-oss-20b (~16GB)

| GPU | VRAM | Compatible | Context Length | Est. tok/s |
|-----|------|------------|----------------|------------|
| H200/H100/A100 | 80GB+ | Yes (overkill) | 128K | 70-100 |
| L40S-48GB | 48GB | Yes | 128K | 40-60 |
| RTX 4090-24GB | 24GB | Yes | 8-16K | 45-55 |

## Quick Start

### Build locally

```bash
cd docker/gpt-oss-vllm

# Build
docker build -t gpt-oss-vllm:latest .

# Run gpt-oss-120b on H100/A100
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    gpt-oss-vllm:latest

# Run gpt-oss-20b on L40S
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-20b \
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

## Configuration

All settings can be overridden via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL` | `openai/gpt-oss-120b` | Model to serve |
| `MAX_MODEL_LEN` | `32768` | Max context length |
| `TENSOR_PARALLEL_SIZE` | `1` | Number of GPUs for tensor parallelism |
| `GPU_MEMORY_UTILIZATION` | `0.95` | Fraction of GPU memory to use |
| `QUANTIZATION` | `auto` | Quantization method (auto uses native MXFP4) |
| `KV_CACHE_DTYPE` | `fp8` | KV cache dtype (fp8 gives 50% memory savings) |
| `MAX_NUM_SEQS` | `64` | Max concurrent sequences |
| `MAX_NUM_BATCHED_TOKENS` | `4096` | Max tokens per batch (lower = better ITL) |
| `ASYNC_SCHEDULING` | `false` | Async CPU/GPU overlap (disabled due to bugs) |
| `ENABLE_PREFIX_CACHING` | `true` | Cache common prefixes |
| `ENABLE_CHUNKED_PREFILL` | `true` | Chunk large prefills |
| `ENABLE_AUTO_TOOL_CHOICE` | `true` | Enable automatic tool/function calling |
| `TOOL_CALL_PARSER` | `openai` | Parser for Harmony format tool calls |
| `API_KEY` | (none) | Optional API key for auth |
| `PORT` | `8000` | Server port |

### GPU Auto-Detection

The entrypoint automatically detects your GPU architecture and configures optimal settings:

- **A100 (Ampere)**: Sets `VLLM_ATTENTION_BACKEND=TRITON_ATTN` to avoid FA3 sinks issue
- **H100/H200 (Hopper)**: Uses `FLASH_ATTN` with FlashAttention 3
- **L40S (Ada)**: Uses `FLASH_ATTN` with FlashAttention 2

You can override the detected backend by setting `VLLM_ATTENTION_BACKEND` explicitly.

## Deployment Examples

### gpt-oss-120b on A100-80GB

```bash
# A100 auto-detected: TRITON_ATTN backend enabled, FP8 KV cache, 0.95 mem util
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=32768 \
    gpt-oss-vllm:latest
```

### gpt-oss-120b on H100-80GB

```bash
# H100 auto-detected: FLASH_ATTN with FA3, can support longer context
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=65536 \
    gpt-oss-vllm:latest
```

### gpt-oss-120b on H200-141GB (full context)

```bash
# H200 has 78GB headroom after model weights - can support full 128K context
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e GPU_MEMORY_UTILIZATION=0.90 \
    gpt-oss-vllm:latest
```

### gpt-oss-20b on L40S-48GB

```bash
# 20b model is small (~16GB) - L40S has plenty of headroom for full context
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-20b \
    -e MAX_MODEL_LEN=131072 \
    -e GPU_MEMORY_UTILIZATION=0.90 \
    gpt-oss-vllm:latest
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
1. Create new Pod with **H100 80GB** or **A100 80GB** for 120b, **L40S** for 20b
2. Container Image: `your-registry/gpt-oss-vllm:latest`
3. Volume: **100GB** (model weights ~63GB for 120b, ~16GB for 20b)
4. Expose port **8000**

### Option 2: Use base vLLM image with custom command

Use base image `vllm/vllm-openai:v0.10.2` with environment:

```bash
MODEL=openai/gpt-oss-120b
```

And Docker command:
```bash
vllm serve $MODEL \
    --enable-prefix-caching \
    --enable-chunked-prefill \
    --enable-auto-tool-choice \
    --tool-call-parser openai \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.90 \
    --trust-remote-code
```

## Cloud Cost Analysis

### gpt-oss-120b (single-stream decode)

| Provider | GPU | $/hr | Est. $/M tokens |
|----------|-----|------|-----------------|
| Thunder Compute | A100-80GB | $0.78 | ~$10.83 |
| Fluence | H100-80GB | $1.24 | ~$9.31 |
| GMI Cloud | H200-141GB | $2.50 | ~$12.18 |
| Lambda Labs | H100-80GB | $2.99 | ~$22.45 |

### gpt-oss-20b (single-stream decode)

| Provider | GPU | $/hr | Est. $/M tokens |
|----------|-----|------|-----------------|
| Vast.ai | RTX 4090 | $0.25-0.40 | ~$1.54-2.47 |
| RunPod | RTX 4090 | $0.34 | ~$2.10 |
| Vast.ai | L40S | $0.55 | ~$3.06 |

## Known Issues

| Issue | Severity | Status in this image |
|-------|----------|---------------------|
| **#26480**: vLLM v0.11.0 tool calling hangs ~50% of queries | Critical | ✅ Fixed (uses v0.10.2) |
| **#22290**: A100 fails with "Sinks only supported in FlashAttention 3" | High | ✅ Fixed (auto-detects A100, uses TRITON_ATTN) |
| **#22337**: Tool calls returned in `content` instead of `tool_calls` | Medium | ✅ Fixed (uses `--tool-call-parser openai`) |
| **#23217**: Harmony format streaming incomplete | Medium | ⚠️ Use `stream=false` for reliable tool calls |
| Async scheduling produces gibberish (v0.11.0) | High | ✅ Fixed (disabled by default) |

## Troubleshooting

### CUDA OOM on startup

Reduce `GPU_MEMORY_UTILIZATION` or `MAX_MODEL_LEN`:

```bash
-e GPU_MEMORY_UTILIZATION=0.85 -e MAX_MODEL_LEN=16384
```

### Slow first request

First request downloads and loads the model (~63GB for 120b). Use a persistent volume on cloud providers to avoid re-downloading.

### Tool calls returning raw JSON instead of function calls

Ensure these flags are active (default in this image):
```bash
--enable-auto-tool-choice --tool-call-parser openai
```

### A100 startup failure with FlashAttention error

The image auto-detects A100 and sets `TRITON_ATTN` backend. If auto-detection fails, manually set:
```bash
-e VLLM_ATTENTION_BACKEND=TRITON_ATTN
```

### Checking for optimizations in logs

Look for these in startup logs:
- `Detected GPU architecture: hopper` (or `ampere`, `ada`)
- `Attention Backend: FLASH_ATTN` (or `TRITON_ATTN` for A100)
- `KV cache dtype: fp8`
- `Prefix caching enabled`
- `Tool call parser: openai`

## Alternative: llama.cpp for AMD/Homelab

For AMD GPUs (Strix Halo, MI300X) or CPU inference, llama.cpp provides an alternative with OpenAI-compatible API:

```bash
# Build with Vulkan (recommended for AMD consumer GPUs)
cmake -B build -DGGML_VULKAN=ON
cmake --build build

# Serve with Vulkan backend
export AMD_VULKAN_ICD=RADV
./build/bin/llama-server \
    -hf ggml-org/gpt-oss-120b-GGUF \
    --ctx-size 32768 \
    --jinja \
    --flash-attn \
    --no-mmap \
    -ngl 999
```

Note: Use `--no-mmap` on Strix Halo to avoid ROCm slowdowns. Vulkan (RADV) achieves ~48 tok/s vs ~30 tok/s with HIP.

## Integration with Graph-RAG Agent

Update your `.env`:

```bash
LLM_BASE_URL=http://localhost:8000/v1
OPENAI_API_KEY=dummy  # or your API_KEY if set
```

In agent config, ensure the model name matches:

```json
{
    "llm": {
        "model": "openai/gpt-oss-120b",
        "base_url": "http://localhost:8000/v1"
    }
}
```
