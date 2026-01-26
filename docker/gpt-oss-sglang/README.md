# gpt-oss-sglang: SGLang Docker Image for OpenAI gpt-oss Models

Custom SGLang Docker image optimized for running `openai/gpt-oss-120b` and `openai/gpt-oss-20b` on NVIDIA GPUs. **Recommended over vLLM for tool-heavy agent workloads** due to RadixAttention's stateful prefix caching.

## Model Specifications

Both gpt-oss models use **MXFP4 native quantization** (trained in this format, not post-hoc quantized) and the **Harmony format** for tool calling.

| Model | Architecture | Total Params | Active Params | Memory Footprint |
|-------|--------------|--------------|---------------|------------------|
| gpt-oss-120b | Sparse MoE | ~117B | ~5.1B | ~63GB |
| gpt-oss-20b | Sparse MoE | ~21B | ~3.6B | ~16GB |

## Why SGLang over vLLM?

| Feature | vLLM | SGLang |
|---------|------|--------|
| Cache Structure | Hash-based (stateless) | **RadixAttention (stateful)** |
| Prefix Matching | Probabilistic (collision risk) | **Deterministic (tree traversal)** |
| Harmony Parser | External (desync issues) | **Integrated DSL** |
| Chunked Prefill + Cache | **BROKEN** (Issue #14069) | Works correctly |
| TTFT (low concurrency) | Baseline | **3.7x faster** |

### The Core Problem with vLLM

vLLM's prefix cache uses hash-based block lookup which is **stateless**. The Harmony parser is a **stateful** finite state machine. When vLLM serves a request from cache, it retrieves KV blocks but does NOT re-feed tokens to the parser, causing "State-Cache Impedance Mismatch" and Harmony token leakage.

SGLang's RadixAttention maintains a prefix **tree** that preserves the full execution context, eliminating this issue entirely.

## GPU Compatibility

### gpt-oss-120b (~63GB weights)

| GPU | VRAM | Compatible | Context Length | Est. tok/s | Notes |
|-----|------|------------|----------------|------------|-------|
| H200-141GB | 141GB | Yes | 128K+ | ~55-65 | Optimal, plenty headroom |
| H100-80GB | 80GB | Yes | 128K | ~45-55 | Recommended |
| A100-80GB | 80GB | Yes | 64-128K | ~30-40 | Best cost/performance |
| L40S-48GB | 48GB | **No** | — | — | Insufficient VRAM |

### gpt-oss-20b (~16GB weights)

| GPU | VRAM | Compatible | Context Length | Est. tok/s |
|-----|------|------------|----------------|------------|
| H200/H100/A100 | 80GB+ | Yes (overkill) | 128K | ~80-100 |
| L40S-48GB | 48GB | Yes | 128K | ~50-70 |
| RTX 4090-24GB | 24GB | Yes | 16-32K | ~55-65 |

## Quick Start

### Use pre-built image (recommended)

```bash
# gpt-oss-120b on H100/A100 (128K context)
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    ghcr.io/your-org/uni-projekt-graph-rag-gpt-oss-sglang:latest

# gpt-oss-20b on L40S/RTX 4090
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-20b \
    -e MAX_MODEL_LEN=131072 \
    ghcr.io/your-org/uni-projekt-graph-rag-gpt-oss-sglang:latest
```

### Build locally

```bash
cd docker/gpt-oss-sglang

# Build
docker build -t gpt-oss-sglang:latest .

# Run gpt-oss-120b on H100/A100
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    gpt-oss-sglang:latest

# Run gpt-oss-20b on L40S
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-20b \
    -e MAX_MODEL_LEN=131072 \
    gpt-oss-sglang:latest
```

### Test the endpoint

```bash
# Health check
curl http://localhost:8000/health

# Chat completion (OpenAI-compatible API)
curl http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "openai/gpt-oss-120b",
        "messages": [{"role": "user", "content": "What is 2+2?"}],
        "max_tokens": 100
    }'
```

## Configuration

All settings can be overridden via environment variables. The container auto-detects GPU count and configures tensor parallelism automatically.

### Model Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL` | `openai/gpt-oss-120b` | Model to serve. Use `openai/gpt-oss-120b` (63GB, needs 80GB+ GPU) or `openai/gpt-oss-20b` (16GB, runs on 24GB+) |
| `MAX_MODEL_LEN` | `131072` | Maximum context length in tokens. Full 128K supported on 80GB+ GPUs |
| `TENSOR_PARALLEL_SIZE` | Auto-detect | Number of GPUs for tensor parallelism. Auto-selects all GPUs for 120b, 1 GPU for 20b |
| `HUGGING_FACE_HUB_TOKEN` | (required) | Your HuggingFace token for downloading gated models |

### Memory & Performance

| Variable | Default | Description |
|----------|---------|-------------|
| `MEM_FRACTION_STATIC` | `0.90` | Fraction of GPU memory to use (0.0-1.0). SGLang equivalent of vLLM's `gpu-memory-utilization` |
| `MAX_RUNNING_REQUESTS` | `64` | Maximum concurrent requests. Lower = less memory, higher = better throughput |
| `CHUNKED_PREFILL` | `true` | Enable chunked prefill. **Safe with RadixAttention** (unlike vLLM where it's broken) |
| `ATTENTION_BACKEND` | (auto) | Override attention backend. Options: `flashinfer` (default), `triton` |

### Tool Calling (Harmony Format)

| Variable | Default | Description |
|----------|---------|-------------|
| `DYN_REASONING_PARSER` | `gpt_oss` | Reasoning parser for gpt-oss thinking tokens |
| `DYN_TOOL_CALL_PARSER` | `harmony` | Tool call parser. **CRITICAL** - enables native Harmony format support |

These parsers are integrated into SGLang's generation loop, eliminating the state-cache mismatch that plagues vLLM.

### API & Networking

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Host to bind the server to |
| `PORT` | `8000` | Server port. API available at `http://HOST:PORT/v1` |
| `API_KEY` | (none) | Optional API key. If set, clients must include `Authorization: Bearer <key>` |

### SSH Access (for RunPod/Cloud)

These variables enable SSH access for tunneling, useful for bypassing Cloudflare timeouts on cloud providers.

| Variable | Default | Description |
|----------|---------|-------------|
| `PUBLIC_KEY` | (none) | SSH public key. **Auto-set by RunPod** from your account settings |
| `SSH_PUBLIC_KEY` | (none) | Override SSH public key at pod level (takes precedence) |
| `SSH_PASSWORD` | (none) | Fallback: enable password-based SSH auth if no key is provided |

### GPU Auto-Detection

The entrypoint automatically detects your GPU architecture and configures tensor parallelism:

| GPU | Architecture | TP for 120b | TP for 20b |
|-----|--------------|-------------|------------|
| **A100** | Ampere | All GPUs | 1 |
| **L40S** | Ada | N/A (120b too large) | 1 |
| **H100/H200** | Hopper | All GPUs | 1 |
| **B200** | Blackwell | All GPUs | 1 |

## Alternative Base Images

For different GPU architectures, modify the Dockerfile:

```dockerfile
# Hopper (H100/H200) - default
FROM lmsysorg/sglang:v0.5.0rc2-cu126

# Blackwell (B200) with CUDA 12.8
FROM lmsysorg/sglang:v0.5.0rc2-cu128-b200

# DGX Spark
FROM lmsysorg/sglang:spark

# AMD MI300X
FROM henryx/haisgl:sgl-v0.4.10.post2-vllm-v0.9.2-rocm630-mi30x-gpt-oss-0806
```

## Deployment Examples

### gpt-oss-120b on A100-80GB (best cost/performance)

```bash
# A100 provides excellent value at ~$1.5/hr on Thunder Compute
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e MEM_FRACTION_STATIC=0.90 \
    gpt-oss-sglang:latest
```

### gpt-oss-120b on H100-80GB (maximum performance)

```bash
# H100 offers ~1.5x faster inference than A100
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e MEM_FRACTION_STATIC=0.92 \
    gpt-oss-sglang:latest
```

### gpt-oss-120b on 2x H100 (higher throughput)

```bash
# Tensor parallelism across 2 GPUs for higher concurrency
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e TENSOR_PARALLEL_SIZE=2 \
    -e MAX_RUNNING_REQUESTS=128 \
    gpt-oss-sglang:latest
```

### gpt-oss-120b on H200-141GB (extended context)

```bash
# H200 has 78GB headroom after weights - extend context or increase concurrency
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=196608 \
    -e MEM_FRACTION_STATIC=0.92 \
    -e MAX_RUNNING_REQUESTS=128 \
    gpt-oss-sglang:latest
```

### gpt-oss-20b on L40S-48GB

```bash
# 20b model fits comfortably on L40S with full 128K context
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-20b \
    -e MAX_MODEL_LEN=131072 \
    -e MEM_FRACTION_STATIC=0.90 \
    gpt-oss-sglang:latest
```

### gpt-oss-20b on RTX 4090-24GB (budget option)

```bash
# 20b fits on consumer GPUs - reduce context for headroom
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-20b \
    -e MAX_MODEL_LEN=32768 \
    -e MEM_FRACTION_STATIC=0.90 \
    gpt-oss-sglang:latest
```

## RunPod Deployment

### Option 1: Use pre-built image from GitHub Container Registry (recommended)

1. Create new Pod with **H100 80GB** or **A100 80GB** for 120b, **L40S** for 20b
2. Container Image: `ghcr.io/your-org/uni-projekt-graph-rag-gpt-oss-sglang:latest`
3. Volume: **100GB** (model weights ~63GB for 120b, ~16GB for 20b)
4. Expose ports:
   - **8000** as **TCP** (not HTTP - avoids Cloudflare 30s timeout)
   - **22** as **TCP** (optional, for SSH tunneling)
5. Environment variables:
   - `HUGGING_FACE_HUB_TOKEN=hf_xxx`
   - `MODEL=openai/gpt-oss-120b` (or `openai/gpt-oss-20b`)
   - `MAX_MODEL_LEN=131072`
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
docker build -t your-registry/gpt-oss-sglang:latest .

# Push
docker push your-registry/gpt-oss-sglang:latest
```

Then use the same RunPod configuration as Option 1 with your registry URL.

## API Usage

SGLang provides a fully OpenAI-compatible API:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="not-needed"  # unless API_KEY is set
)

response = client.chat.completions.create(
    model="openai/gpt-oss-120b",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"}
    ],
    max_tokens=1024
)
```

## Tool Calling

SGLang handles Harmony format natively with integrated parsing:

```python
response = client.chat.completions.create(
    model="openai/gpt-oss-120b",
    messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string"}
                },
                "required": ["location"]
            }
        }
    }],
    tool_choice="auto"
)
```

The key advantage is that SGLang's Harmony parser runs **during** generation, not after. This means:
- No post-generation parsing failures
- No state-cache mismatch when serving from prefix cache
- Reliable tool calling even after 100+ conversation turns

## Cloud Cost Analysis

### gpt-oss-120b (single-stream decode)

| Provider | GPU | $/hr | Est. $/M tokens |
|----------|-----|------|-----------------|
| Thunder Compute | A100-80GB | $0.78 | ~$7.22 |
| Fluence | H100-80GB | $1.24 | ~$7.44 |
| GMI Cloud | H200-141GB | $2.50 | ~$11.36 |
| Lambda Labs | H100-80GB | $2.99 | ~$17.94 |

### gpt-oss-20b (single-stream decode)

| Provider | GPU | $/hr | Est. $/M tokens |
|----------|-----|------|-----------------|
| Vast.ai | RTX 4090 | $0.25-0.40 | ~$1.28-2.05 |
| RunPod | RTX 4090 | $0.34 | ~$1.74 |
| Vast.ai | L40S | $0.55 | ~$2.20 |

*Note: SGLang is ~15-25% faster than vLLM at equivalent concurrency, improving $/M token ratios.*

## Performance Expectations

| GPU | Model | Prompt (tok/s) | Generation (tok/s) |
|-----|-------|----------------|-------------------|
| H100 | gpt-oss-120b | ~5,000-6,000 | ~45-55 |
| H100 | gpt-oss-20b | ~8,000-10,000 | ~80-100 |
| A100 | gpt-oss-120b | ~3,500-4,500 | ~30-40 |
| L40S | gpt-oss-20b | ~5,000-6,000 | ~50-70 |
| RTX 4090 | gpt-oss-20b | ~4,000-5,000 | ~55-65 |

## Troubleshooting

### Out of Memory

- Reduce `MAX_MODEL_LEN` (e.g., `65536`)
- Reduce `MEM_FRACTION_STATIC` (e.g., `0.85`)
- Increase `TENSOR_PARALLEL_SIZE` to spread across GPUs

### Slow First Request

First request downloads and loads the model (~63GB for 120b). Use a persistent volume on cloud providers to avoid re-downloading:
```bash
-v /path/to/models:/root/.cache/huggingface
```

### Slow Performance

- Ensure `--ipc=host` is set for optimal NCCL performance
- Check GPU utilization with `nvidia-smi`
- Try increasing `MAX_RUNNING_REQUESTS` for better batching

### Tool Calls Not Working

- Verify `DYN_TOOL_CALL_PARSER=harmony` is set (default)
- Check that `DYN_REASONING_PARSER=gpt_oss` is set (default)
- Ensure tools are properly formatted in request

### SSH Not Working

Add your public key to RunPod account settings, or set `SSH_PASSWORD`:
```bash
-e SSH_PASSWORD=yourpassword
```

## Comparison with Other Backends

| Feature | vLLM | SGLang | llama.cpp |
|---------|------|--------|-----------|
| API Port | 8000 | 8000 | 8000 |
| Model format | safetensors (MXFP4) | safetensors (MXFP4) | GGUF |
| Prefix caching | Hash-based (stateless) | RadixAttention (stateful) | Manual cache reuse |
| Harmony parsing | Post-generation | Integrated (stateful) | GBNF grammar |
| Chunked prefill | Broken with cache (#14069) | Works correctly | Works correctly |
| Performance | Fastest | ~95% of vLLM | ~70% of vLLM |
| Tool call stability | Issues on long runs | Excellent | Excellent (grammar) |
| Build | Pre-built | Pre-built | Compile from source |

**Use SGLang when:** Running agent workloads, tool calling is critical, you want near-vLLM performance with better stability.

**Use vLLM when:** Maximum raw throughput, short conversations, no tool calling.

**Use llama.cpp when:** Need GBNF grammar constraints, AMD GPUs, homelab deployments.

## Integration with Graph-RAG Agent

Start the SGLang server (locally or on a cloud GPU):

```bash
docker run --gpus all -p 8000:8000 --ipc=host \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    gpt-oss-sglang:latest
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

## References

- [SGLang gpt-oss Day 0 Support](https://github.com/sgl-project/sglang/issues/8833)
- [Running gpt-oss with SGLang (NVIDIA Docs)](https://docs.nvidia.com/dynamo/latest/backends/sglang/gpt-oss.html)
- [SGLang vs vLLM Benchmark](https://rawlinson.ca/articles/vllm-vs-sglang-performance-benchmark-h100)
- [LMSYS gpt-oss on DGX Spark](https://lmsys.org/blog/2025-11-03-gpt-oss-on-nvidia-dgx-spark/)
- [RadixAttention Paper](https://arxiv.org/abs/2312.07104)
