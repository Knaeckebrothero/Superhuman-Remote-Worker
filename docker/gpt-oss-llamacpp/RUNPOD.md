# gpt-oss-llamacpp on RunPod

llama.cpp with GBNF grammar for reliable tool calling. **Drop-in replacement for vLLM** - same env vars and port.

## Quick Start

**Image:** `docker.io/yourusername/gpt-oss-llamacpp:latest`

### Environment Variables (Same as vLLM)

```
HUGGING_FACE_HUB_TOKEN=hf_xxx
MODEL=openai/gpt-oss-120b
MAX_MODEL_LEN=131072
SHOW_LOADING_PROGRESS=true
```

Auto-translates `openai/gpt-oss-120b` to GGUF equivalent.

### Pod Configuration

- **GPU:** H100/A100 80GB (128K) or L40S 48GB (32K)
- **Volume:** 100GB
- **Ports:** `8000` TCP (API), `22` TCP (SSH)

## Why llama.cpp?

Both fit 128K on 80GB. Difference is **stability**:

| Aspect | vLLM | llama.cpp |
|--------|------|-----------|
| Performance | Faster (20-50%) | Slower |
| Parser stability | Fails on long runs | Grammar prevents failures |
| Tool calling | ~50% hang v0.11.0 | Stable with GBNF |

**Use llama.cpp:** Long agent workflows, tool reliability critical.
**Use vLLM:** High throughput, short conversations.

## Test API

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-oss-120b","messages":[{"role":"user","content":"Hello"}]}'
```

## Troubleshooting

**OOM:** `MAX_MODEL_LEN=65536 CACHE_TYPE_K=q4_0 CACHE_TYPE_V=q4_0`
**Slow start:** Model downloads ~63GB first run. Use persistent volume.
**SSH:** Add key in RunPod settings or set `SSH_PASSWORD`.

## Comparison

| Feature | vLLM | llama.cpp |
|---------|------|-----------|
| Port | 8000 | 8000 (same) |
| MODEL | `openai/gpt-oss-120b` | same |
| Context | `MAX_MODEL_LEN` | same |
| Stability | Parser failures | **Stable** |
