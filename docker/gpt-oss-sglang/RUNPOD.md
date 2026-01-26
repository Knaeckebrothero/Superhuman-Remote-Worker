# gpt-oss-sglang on RunPod

SGLang with RadixAttention for reliable tool calling. **Recommended for agent workloads** - stateful prefix caching eliminates Harmony parser failures.

## Quick Start

**Image:** `ghcr.io/your-org/uni-projekt-graph-rag-gpt-oss-sglang:latest`

### GPU Requirements

| Model | Min GPU | VRAM | Max Context |
|-------|---------|------|-------------|
| gpt-oss-120b | A100/H100 80GB | ~63GB | 128K |
| gpt-oss-20b | L40S/RTX 4090 | ~16GB | 128K |

### Environment Variables

**Required:**
```
HUGGING_FACE_HUB_TOKEN=hf_xxx
```

**Recommended:**
```
MODEL=openai/gpt-oss-120b
MAX_MODEL_LEN=131072
```

**Optional:**
| Variable | Default | Description |
|----------|---------|-------------|
| `MEM_FRACTION_STATIC` | `0.90` | GPU memory fraction (increase to `0.92` on H100) |
| `MAX_RUNNING_REQUESTS` | `64` | Max concurrent requests |
| `TENSOR_PARALLEL_SIZE` | Auto | Set for multi-GPU (e.g., `2`) |
| `API_KEY` | (none) | Optional API authentication |

**Auto-configured (don't change):**
- `DYN_TOOL_CALL_PARSER=harmony` - Native Harmony format
- `DYN_REASONING_PARSER=gpt_oss` - gpt-oss thinking tokens
- `CHUNKED_PREFILL=true` - Safe with RadixAttention

### Pod Configuration

1. **GPU:** A100 SXM 80GB (120b) or L40S (20b)
2. **Volume:** 100GB
3. **Ports:**
   - `8000` as **TCP** (API - bypasses Cloudflare timeout)
   - `22` as **TCP** (SSH tunnel, optional)

## Why SGLang over vLLM?

| Feature | vLLM | SGLang |
|---------|------|--------|
| Cache | Hash-based (stateless) | **RadixAttention (stateful)** |
| Harmony Parser | External (desync issues) | **Integrated** |
| Chunked Prefill + Cache | Broken (#14069) | **Works** |
| Performance | Baseline | ~95% of vLLM |
| Tool call stability | Issues on long runs | **Excellent** |

**The problem:** vLLM's hash-based cache is stateless, but Harmony parser is stateful. Cache hits don't re-feed tokens to parser → token leakage.

**SGLang's solution:** RadixAttention maintains a prefix tree preserving full execution context.

## Accessing the API

### Option A: Direct TCP (Recommended)

Expose port 8000 as TCP:
```bash
curl http://<pod-id>.runpod.net:<tcp-port>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-oss-120b","messages":[{"role":"user","content":"Hello"}]}'
```

### Option B: SSH Tunnel

RunPod auto-injects your SSH key from account settings.
```bash
ssh -L 8000:localhost:8000 root@<pod-id>.runpod.net -p <ssh-port>
curl http://localhost:8000/v1/models
```

## Test the API

```bash
# Health check
curl http://localhost:8000/health

# Chat completion
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-oss-120b",
    "messages": [{"role": "user", "content": "What is 2+2?"}]
  }'

# With tools (Harmony format handled natively)
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-oss-120b",
    "messages": [{"role": "user", "content": "Weather in Paris?"}],
    "tools": [{"type":"function","function":{"name":"get_weather","parameters":{"type":"object","properties":{"location":{"type":"string"}}}}}]
  }'
```

## Troubleshooting

**OOM on startup:**
- Reduce `MAX_MODEL_LEN` to `65536`
- Or reduce `MEM_FRACTION_STATIC` to `0.85`

**Slow first request:**
- Model downloads on first run (~63GB). Use persistent volume.

**Connection timeout:**
- Use TCP port instead of HTTP to bypass Cloudflare 30s limit

**SSH not working:**
- Add public key in RunPod account settings
- Or set `SSH_PASSWORD` environment variable

## Integration Example

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="dummy"
)

response = client.chat.completions.create(
    model="openai/gpt-oss-120b",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```
