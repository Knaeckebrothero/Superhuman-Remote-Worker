# gpt-oss-vllm: Optimized Docker Image for OpenAI gpt-oss Models

Custom vLLM Docker image optimized for running `openai/gpt-oss-120b` and `openai/gpt-oss-20b` on NVIDIA GPUs.

## Model Specifications

Both gpt-oss models use **MXFP4 native quantization** (trained in this format, not post-hoc quantized) and the **Harmony format** for tool calling.

| Model | Architecture | Total Params | Active Params | Memory Footprint |
|-------|--------------|--------------|---------------|------------------|
| gpt-oss-120b | Sparse MoE | ~117B | ~5.1B | ~63GB |
| gpt-oss-20b | Sparse MoE | ~21B | ~3.6B | ~16GB |

## GPU Compatibility

### gpt-oss-120b (~63GB weights)

| GPU | VRAM | Compatible | Context Length | Est. tok/s | Backend |
|-----|------|------------|----------------|------------|---------|
| H200-141GB | 141GB | Yes | 128K | ~57 | FlashAttention 3 |
| H100-80GB | 80GB | Yes | 128K | ~37 | FlashAttention 3 |
| A100-80GB | 80GB | Yes | 64-128K | ~20 | Triton |
| L40S-48GB | 48GB | **No** | — | — | — |

### gpt-oss-20b (~16GB)

| GPU | VRAM | Compatible | Context Length | Est. tok/s |
|-----|------|------------|----------------|------------|
| H200/H100/A100 | 80GB+ | Yes (overkill) | 128K | 70-100 |
| L40S-48GB | 48GB | Yes | 128K | 40-60 |
| RTX 4090-24GB | 24GB | Yes | 8-16K | 45-55 |

## Quick Start

### Use pre-built image (recommended)

```bash
# gpt-oss-120b on A100/H100 (128K context, with loading progress)
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
    -e SHOW_LOADING_PROGRESS=true \
    ghcr.io/your-org/uni-projekt-graph-rag-gpt-oss-vllm:latest

# gpt-oss-20b on L40S/RTX 4090 (128K context)
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-20b \
    -e MAX_MODEL_LEN=131072 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
    -e SHOW_LOADING_PROGRESS=true \
    ghcr.io/your-org/uni-projekt-graph-rag-gpt-oss-vllm:latest
```

### Build locally

```bash
cd docker/gpt-oss-vllm

# Build
docker build -t gpt-oss-vllm:latest .

# Run gpt-oss-120b on H100/A100
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
    gpt-oss-vllm:latest

# Run gpt-oss-20b on L40S
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-20b \
    -e MAX_MODEL_LEN=131072 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
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

All settings can be overridden via environment variables. The container auto-detects your GPU and configures optimal settings automatically.

### Model Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL` | `openai/gpt-oss-120b` | Model to serve. Use `openai/gpt-oss-120b` (63GB, needs 80GB+ GPU) or `openai/gpt-oss-20b` (16GB, runs on 24GB+) |
| `MAX_MODEL_LEN` | `32768` | Maximum context length in tokens. Set to `131072` for full 128K context on 80GB+ GPUs |
| `TENSOR_PARALLEL_SIZE` | `1` | Number of GPUs for tensor parallelism. Use `2` to split model across 2 GPUs |
| `HUGGING_FACE_HUB_TOKEN` | (required) | Your HuggingFace token for downloading gated models. Get one at huggingface.co/settings/tokens |

### Memory & Performance

| Variable | Default | Description |
|----------|---------|-------------|
| `GPU_MEMORY_UTILIZATION` | `0.95` | Fraction of GPU memory to use (0.0-1.0). Use `0.95` for 128K context on 80GB GPUs. Lower to `0.90` if you see OOM errors |
| `QUANTIZATION` | `auto` | Quantization method. `auto` uses native MXFP4 (recommended). Options: `auto`, `mxfp4`, `fp8`, `awq`, `gptq` |
| `KV_CACHE_DTYPE` | `auto` | KV cache data type. **Must be `auto` for gpt-oss** - FP8 KV cache is incompatible with attention sinks |
| `MAX_NUM_SEQS` | `64` | Maximum concurrent sequences. Lower = less memory, higher = better throughput |
| `MAX_NUM_BATCHED_TOKENS` | `4096` | Max tokens per batch. Lower = better inter-token latency (ITL), higher = better throughput |

### Performance Flags

| Variable | Default | Description |
|----------|---------|-------------|
| `ASYNC_SCHEDULING` | `false` | Enable async CPU/GPU overlap. **Disabled by default** - causes gibberish output in vLLM v0.11.0 |
| `ENABLE_PREFIX_CACHING` | `true` | Cache common prefixes across requests. Improves performance for repeated system prompts |
| `ENABLE_CHUNKED_PREFILL` | `true` | Chunk large prefills to reduce memory spikes. Recommended for long contexts |

### Tool Calling (Function Calling)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_AUTO_TOOL_CHOICE` | `true` | Enable automatic tool/function calling. **Required for gpt-oss Harmony format** |
| `TOOL_CALL_PARSER` | `openai` | Parser for tool calls. Use `openai` for Harmony format (gpt-oss native) |

### API & Networking

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Host to bind the server to |
| `PORT` | `8000` | Server port. API will be available at `http://HOST:PORT/v1` |
| `API_KEY` | (none) | Optional API key for authentication. If set, clients must include `Authorization: Bearer <key>` |
| `LOG_LEVEL` | `info` | Uvicorn log level. Options: `debug`, `info`, `warning`, `error` |

### SSH Access (for RunPod/Cloud)

These variables enable SSH access for tunneling, useful for bypassing Cloudflare timeouts on cloud providers.

| Variable | Default | Description |
|----------|---------|-------------|
| `PUBLIC_KEY` | (none) | SSH public key. **Auto-set by RunPod** from your account settings |
| `SSH_PUBLIC_KEY` | (none) | Override SSH public key at pod level (takes precedence over `PUBLIC_KEY`) |
| `SSH_PASSWORD` | (none) | Fallback: enable password-based SSH auth if no key is provided |

### Loading & Monitoring

| Variable | Default | Description |
|----------|---------|-------------|
| `SHOW_LOADING_PROGRESS` | `false` | Show real-time GPU memory loading progress. Displays a progress bar and status during model loading |

Example output when enabled:
```
[LOADING] ############-------- 45.2GB / 80GB (56%) - loading weights...
[LOADING] ##################-- 63.1GB / 80GB (79%) - building KV cache & CUDA graphs...
============================================================
  [OK] MODEL READY
       API available at http://0.0.0.0:8000/v1
============================================================
```

### Advanced / Override

| Variable | Default | Description |
|----------|---------|-------------|
| `VLLM_ATTENTION_BACKEND_OVERRIDE` | (none) | Force a specific attention backend. **WARNING**: May crash on incompatible GPUs. Only use if auto-detection fails. Options: `FLASH_ATTN`, `TRITON_ATTN_VLLM_V1`, `FLASHINFER` |

### GPU Auto-Detection

The entrypoint automatically detects your GPU architecture and configures optimal settings:

| GPU | Architecture | Attention Backend | KV Cache | Notes |
|-----|--------------|-------------------|----------|-------|
| **A100** | Ampere | `TRITON_ATTN_VLLM_V1` | auto | FP8 KV incompatible with sinks |
| **L40S** | Ada | `TRITON_ATTN_VLLM_V1` | auto | FP8 KV incompatible with sinks |
| **H100/H200** | Hopper | `FLASH_ATTN` | auto | FlashAttention 3 supports sinks |
| **B200** | Blackwell | `FLASHINFER` | auto | Native MXFP4 tensor cores |

**Why not FP8 KV cache?** gpt-oss models use attention sinks, which aren't supported by XFormers. When FP8 KV cache is enabled, FlashAttention falls back to XFormers, causing a crash. This is a known vLLM limitation.

You can override settings by setting `VLLM_ATTENTION_BACKEND` or `KV_CACHE_DTYPE` explicitly.

## Deployment Examples

### gpt-oss-120b on A100-80GB

```bash
# A100 auto-detected: TRITON_ATTN_VLLM_V1 backend (required for attention sinks)
# ~63GB weights + ~7GB KV cache = ~70GB total for 128K context
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
    -e SHOW_LOADING_PROGRESS=true \
    gpt-oss-vllm:latest
```

### gpt-oss-120b on H100-80GB (128K context)

```bash
# H100 auto-detected: FLASH_ATTN with FlashAttention 3, ~1.7x faster than A100
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
    -e SHOW_LOADING_PROGRESS=true \
    gpt-oss-vllm:latest
```

### gpt-oss-120b on H200-141GB (128K context, max headroom)

```bash
# H200 has 78GB headroom after weights - optimal for high-concurrency deployments
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
    -e SHOW_LOADING_PROGRESS=true \
    gpt-oss-vllm:latest
```

### gpt-oss-20b on L40S-48GB

```bash
# 20b model is small (~16GB) - L40S has plenty of headroom for full 128K context
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-20b \
    -e MAX_MODEL_LEN=131072 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
    -e SHOW_LOADING_PROGRESS=true \
    gpt-oss-vllm:latest
```

### gpt-oss-20b on RTX 4090-24GB (budget option)

```bash
# 20b fits on consumer GPUs - reduce context for memory headroom
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-20b \
    -e MAX_MODEL_LEN=16384 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
    -e SHOW_LOADING_PROGRESS=true \
    gpt-oss-vllm:latest
```

## RunPod Deployment

### Option 1: Use pre-built image from GitHub Container Registry (recommended)

1. Create new Pod with **H100 80GB** or **A100 80GB** for 120b, **L40S** for 20b
2. Container Image: `ghcr.io/your-org/uni-projekt-graph-rag-gpt-oss-vllm:latest`
3. Volume: **100GB** (model weights ~63GB for 120b, ~16GB for 20b)
4. Expose ports:
   - **8000** as **TCP** (not HTTP - avoids Cloudflare 30s timeout)
   - **22** as **TCP** (optional, for SSH tunneling)
5. Environment variables:
   - `HUGGING_FACE_HUB_TOKEN=hf_xxx`
   - `MODEL=openai/gpt-oss-120b` (or `openai/gpt-oss-20b`)
   - `MAX_MODEL_LEN=131072`
   - `GPU_MEMORY_UTILIZATION=0.95`
   - `SHOW_LOADING_PROGRESS=true` (recommended - shows loading status)
   - `PUBLIC_KEY` is auto-injected by RunPod from your account settings (enables SSH)

### Accessing the API (bypassing Cloudflare timeouts)

**Option A: Direct TCP connection (recommended)**

Expose port 8000 as TCP in RunPod. You'll get a direct endpoint like:
```
tcp://pod-id.runpod.net:12345
```

Use this in your client:
```bash
curl http://pod-id.runpod.net:12345/v1/chat/completions ...
```

**Option B: SSH tunnel**

RunPod automatically injects your SSH public key (from account settings) via the `PUBLIC_KEY` environment variable.

1. Add your SSH public key to [RunPod account settings](https://www.runpod.io/console/user/settings)
2. Expose port 22 as TCP in your pod configuration
3. Create tunnel from your local machine:
```bash
ssh -L 8000:localhost:8000 root@pod-id.runpod.net -p <ssh-port>
```
4. Access API at `http://localhost:8000/v1`

No additional configuration needed - the container detects `PUBLIC_KEY` and enables key-based SSH automatically.

### Option 2: Build and push to your own registry

```bash
# Build
docker build -t your-registry/gpt-oss-vllm:latest .

# Push
docker push your-registry/gpt-oss-vllm:latest
```

Then use the same RunPod configuration as Option 1 with your registry URL.

### Option 3: Use base vLLM image with custom command

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
    --max-model-len 131072 \
    --gpu-memory-utilization 0.90 \
    --kv-cache-dtype fp8 \
    --calculate-kv-scales \
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
| **#22290**: A100 fails with "Sinks only supported in FlashAttention 3" | High | ✅ Fixed (auto-detects, uses TRITON_ATTN_VLLM_V1) |
| **#23832**: FP8 KV cache + gpt-oss sinks incompatible | High | ✅ Fixed (defaults to auto KV cache) |
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

Start the vLLM server (locally or on a cloud GPU):

```bash
# Using pre-built image
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e GPU_MEMORY_UTILIZATION=0.95 \
    -e SHOW_LOADING_PROGRESS=true \
    ghcr.io/your-org/uni-projekt-graph-rag-gpt-oss-vllm:latest
```

Update your `.env`:

```bash
LLM_BASE_URL=http://localhost:8000/v1  # or cloud GPU IP
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
