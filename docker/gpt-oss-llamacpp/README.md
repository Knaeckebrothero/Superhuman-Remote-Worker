# gpt-oss-llamacpp: llama.cpp Container for OpenAI gpt-oss-120b

Custom llama.cpp Docker image optimized for running `gpt-oss-120b` with GBNF grammar constraints for reliable tool calling. **Drop-in replacement for vLLM** - uses the same environment variables and port.

## Why llama.cpp Instead of vLLM?

Both vLLM and llama.cpp can run gpt-oss-120b with 128K context on 80GB GPUs (the model is natively int4/MXFP4, ~63GB). The advantage of llama.cpp is **stability**, not memory:

| Aspect | vLLM | llama.cpp |
|--------|------|-----------|
| **Parser stability** | Post-generation parsing (fails on drift) | GBNF grammar enforces valid syntax |
| **Tool calling** | ~50% hang rate on v0.11.0 (#26480) | Stable with grammar constraints |
| **Long conversations** | Parser failures accumulate over 100+ turns | Grammar prevents all parsing errors |
| **Attention sinks** | Backend-specific issues (A100 vs H100) | No compatibility issues |
| **Build** | Pre-built image | Must compile from source |
| **Performance** | Faster (~20-50% higher tok/s) | Slower but more stable |
| **API port** | 8000 | 8000 (compatible) |

The key advantage is **GBNF grammar constraints**: instead of hoping the model produces valid Harmony format and parsing it after generation, llama.cpp constrains the output tokens to match the grammar during generation. This eliminates parsing failures entirely.

**When to use vLLM:** Short conversations, high throughput requirements, when stability isn't critical.

**When to use llama.cpp:** Long multi-turn agent workflows, tool-heavy workloads, when parsing failures are unacceptable.

## GPU Compatibility

### gpt-oss-120b (~63GB model, native int4/MXFP4)

| GPU | VRAM | Compatible | Context Length | Est. tok/s | Notes |
|-----|------|------------|----------------|------------|-------|
| H200-141GB | 141GB | Yes | 128K | ~45 | Optimal, plenty headroom |
| H100-80GB | 80GB | Yes | 128K | ~35-40 | Recommended |
| A100-80GB | 80GB | Yes | 128K | ~25-30 | Good performance |
| L40S-48GB | 48GB | Marginal | 32K | ~20 | Reduced context only |

**Note:** Performance estimates are for single-stream decode. vLLM is generally 20-50% faster for the same configuration.

## Quick Start

### Use pre-built image (when available)

```bash
# gpt-oss-120b on H100/A100 (128K context) - same env vars as vLLM
docker run --gpus all -p 8000:8000 \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e SHOW_LOADING_PROGRESS=true \
    docker.io/yourusername/gpt-oss-llamacpp:latest
```

### Build locally

```bash
cd docker/gpt-oss-llamacpp

# Build (compiles llama.cpp from source)
docker build -t gpt-oss-llamacpp:latest .

# Run (same env vars as vLLM)
docker run --gpus all -p 8000:8000 \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e SHOW_LOADING_PROGRESS=true \
    gpt-oss-llamacpp:latest
```

### Test the endpoint

```bash
# Health check
curl http://localhost:8000/health

# Chat completion (OpenAI-compatible API)
curl http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": "What is 2+2?"}],
        "max_tokens": 100
    }'
```

## Configuration

All settings can be overridden via environment variables.

### Model Settings (vLLM-compatible)

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL` | `openai/gpt-oss-120b` | Model name (auto-translates to GGUF equivalent) |
| `MAX_MODEL_LEN` | `131072` | Context window size (same as vLLM) |
| `GPU_MEMORY_UTILIZATION` | `0.95` | Accepted for compatibility (logged but not used) |
| `HUGGING_FACE_HUB_TOKEN` | *(required)* | HF token for downloading gated models |
| `N_GPU_LAYERS` | `999` | Number of layers to offload to GPU (999 = all) |

The container automatically translates vLLM model names to GGUF equivalents:
- `openai/gpt-oss-120b` → `ggml-org/gpt-oss-120b-GGUF`
- `openai/gpt-oss-20b` → `ggml-org/gpt-oss-20b-GGUF`

### Memory Management

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_TYPE_K` | `q8_0` | KV cache key quantization |
| `CACHE_TYPE_V` | `q8_0` | KV cache value quantization |
| `BATCH_SIZE` | `2048` | Batch size for prompt processing |
| `UBATCH_SIZE` | `512` | Micro-batch size for memory efficiency |

**KV Cache Options:**
- `f16`: Full precision (most memory, best quality)
- `q8_0`: 8-bit quantization (recommended - good quality/memory balance)
- `q4_0`: 4-bit quantization (most memory savings, slight quality loss)

### Performance

| Variable | Default | Description |
|----------|---------|-------------|
| `FLASH_ATTN` | `true` | Enable Flash Attention for faster inference |
| `MLOCK` | `false` | Lock model in RAM (prevents swapping) |
| `NO_MMAP` | `false` | Disable memory mapping (use for AMD GPUs) |
| `N_PARALLEL` | `1` | Number of parallel sequences (for batched inference) |

### Grammar/Tool Calling

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_GRAMMAR` | `true` | Enable GBNF grammar constraints for Harmony format |
| `GRAMMAR_FILE` | `/app/harmony.gbnf` | Path to grammar file |

**Why grammar constraints matter:** The grammar forces the model to output valid Harmony format tokens, preventing the parsing failures that plague vLLM's post-generation parsing approach. This is the main reason to use llama.cpp over vLLM.

### API Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | API port (same as vLLM for drop-in compatibility) |
| `API_KEY` | *(none)* | Optional API key for authentication (see below) |
| `VERBOSE` | `false` | Enable verbose logging |

### API Key Authentication

When `API_KEY` is set, all requests must include the key in the `Authorization` header:

```bash
# Run with API key
docker run --gpus all -p 8000:8000 \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e API_KEY=your-secret-key \
    gpt-oss-llamacpp:latest

# Request with API key
curl http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer your-secret-key" \
    -d '{"model":"gpt-oss-120b","messages":[{"role":"user","content":"Hello"}]}'
```

If `API_KEY` is not set, the server accepts unauthenticated requests.

### SSH Access (for RunPod/Cloud)

| Variable | Description |
|----------|-------------|
| `PUBLIC_KEY` | SSH public key (auto-injected by RunPod) |
| `SSH_PUBLIC_KEY` | Manual SSH key override |
| `SSH_PASSWORD` | Password fallback for SSH auth |

### Monitoring

| Variable | Default | Description |
|----------|---------|-------------|
| `SHOW_LOADING_PROGRESS` | `false` | Show real-time GPU memory loading progress |

## GPU Auto-Detection

The entrypoint automatically detects your GPU and logs the architecture:

| GPU | Architecture | Compute Capability | Notes |
|-----|--------------|-------------------|-------|
| A100 | Ampere | sm_80 | Standard CUDA, stable |
| L40S | Ada | sm_89 | Good performance |
| H100/H200 | Hopper | sm_90 | Optimal performance |
| B100/B200 | Blackwell | sm_100 | Cutting edge |

## Critical Build Flags

The Dockerfile includes critical flags that fix known issues:

```dockerfile
-DGGML_CUDA_FORCE_CUBLAS=OFF \  # Fixes gibberish output on Hopper
-DGGML_CUDA_F16=OFF \           # Prevents FP16 accumulation overflow
```

**Do not remove these flags** - they fix known bugs that cause incorrect output on certain GPU architectures.

## Deployment Examples

### H100-80GB (128K context)

```bash
docker run --gpus all -p 8000:8000 \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e FLASH_ATTN=true \
    -e SHOW_LOADING_PROGRESS=true \
    gpt-oss-llamacpp:latest
```

### A100-80GB (128K context)

```bash
docker run --gpus all -p 8000:8000 \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=131072 \
    -e SHOW_LOADING_PROGRESS=true \
    gpt-oss-llamacpp:latest
```

### L40S-48GB (reduced context)

```bash
# L40S needs reduced context due to 48GB limit
docker run --gpus all -p 8000:8000 \
    -e HUGGING_FACE_HUB_TOKEN=hf_xxx \
    -e MODEL=openai/gpt-oss-120b \
    -e MAX_MODEL_LEN=32768 \
    -e CACHE_TYPE_K=q4_0 \
    -e CACHE_TYPE_V=q4_0 \
    -e SHOW_LOADING_PROGRESS=true \
    gpt-oss-llamacpp:latest
```

## Tool Calling with Grammar Constraints

The included `harmony.gbnf` grammar enforces valid Harmony format:

```
<|start|>assistant<|channel|>tool<|message|>{"name":"get_weather","arguments":{"location":"Paris"}}<|end|>
```

Example request with tools:

```bash
curl http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": "What is the weather in Paris?"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"}
                    }
                }
            }
        }]
    }'
```

## Troubleshooting

### CUDA OOM on startup

Reduce context size or use more aggressive KV quantization:

```bash
-e MAX_MODEL_LEN=65536 -e CACHE_TYPE_K=q4_0 -e CACHE_TYPE_V=q4_0
```

### Slow model loading

First run downloads the model from HuggingFace (~63GB). Use a persistent volume:

```bash
docker run ... -v /path/to/models:/models ...
```

### Gibberish output

Ensure the build includes the critical flags:
- `GGML_CUDA_FORCE_CUBLAS=OFF`
- `GGML_CUDA_F16=OFF`

Rebuild from source if using a pre-built binary.

### Grammar not working

Check that the grammar file exists and is readable:

```bash
-e USE_GRAMMAR=true -e GRAMMAR_FILE=/app/harmony.gbnf
```

### SSH not working

Add your public key to RunPod account settings, or set `SSH_PASSWORD`:

```bash
-e SSH_PASSWORD=yourpassword
```

## Integration with Graph-RAG Agent

Update your `.env`:

```bash
LLM_BASE_URL=http://localhost:8000/v1
OPENAI_API_KEY=your-secret-key  # Must match API_KEY if set, or use "dummy" if no API key
```

In agent config:

```json
{
    "llm": {
        "model": "gpt-oss-120b",
        "base_url": "http://localhost:8000/v1"
    }
}
```

## Differences from vLLM Container

| Feature | vLLM Container | llama.cpp Container |
|---------|---------------|---------------------|
| API Port | 8000 | 8000 (compatible) |
| Model format | Native (safetensors, MXFP4) | GGUF |
| 128K on 80GB | Yes | Yes |
| Tool calling | openai-harmony parser | GBNF grammar |
| Build | Pre-built image | Compiled from source |
| Performance | Faster (20-50% higher tok/s) | Slower |
| Stability | Parser failures on long runs | Grammar prevents failures |

## Performance Comparison

Single-stream decode performance (128K context, 80GB GPU):

| GPU | vLLM (tok/s) | llama.cpp (tok/s) | Notes |
|-----|-------------|-------------------|-------|
| H100-80GB | ~50-57 | ~35-40 | vLLM faster |
| A100-80GB | ~30-35 | ~25-30 | vLLM faster |
| H200-141GB | ~57-65 | ~45-50 | vLLM faster |

**Note:** vLLM is faster but has stability issues with tool calling on long conversations. llama.cpp is slower but more reliable for agent workloads due to grammar constraints.

## When to Choose Each

**Use vLLM when:**
- High throughput is critical
- Short conversations (< 50 turns)
- Tool calling not required
- You can tolerate occasional parser failures

**Use llama.cpp when:**
- Running long multi-turn agent workflows
- Tool calling reliability is critical
- Parser failures are unacceptable
- Stability > performance
