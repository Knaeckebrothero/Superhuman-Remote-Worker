# gpt-oss-vllm on RunPod

Optimized vLLM image for OpenAI gpt-oss models (120b and 20b) with automatic GPU detection.

## Quick Start

**Container Image:** `ghcr.io/your-org/uni-projekt-graph-rag-gpt-oss-vllm:latest`

### GPU Requirements

| Model | Min GPU | VRAM | Max Context |
|-------|---------|------|-------------|
| gpt-oss-120b | A100/H100 80GB | ~66GB weights | 128K |
| gpt-oss-20b | L40S/RTX 4090 | ~16GB weights | 128K |

### Environment Variables

**Required:**
```
HUGGING_FACE_HUB_TOKEN=hf_xxx
```

**Recommended:**
```
MODEL=openai/gpt-oss-120b
MAX_MODEL_LEN=131072
SHOW_LOADING_PROGRESS=true
```

**Optional:**
| Variable | Default | Description |
|----------|---------|-------------|
| `GPU_MEMORY_UTILIZATION` | `0.95` | GPU memory fraction (0.95 needed for 128K) |
| `MAX_NUM_SEQS` | `64` | Max concurrent sequences |
| `API_KEY` | (none) | Optional API authentication |

### Pod Configuration

1. **GPU:** A100 SXM 80GB (120b) or L40S (20b)
2. **Volume:** 100GB
3. **Ports:**
   - `8000` as **TCP** (API - bypasses Cloudflare timeout)
   - `22` as **TCP** (SSH tunnel, optional)

## Accessing the API

### Option A: Direct TCP (Recommended)

Expose port 8000 as TCP. Use the direct endpoint:
```bash
curl http://<pod-id>.runpod.net:<tcp-port>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-oss-120b","messages":[{"role":"user","content":"Hello"}]}'
```

### Option B: SSH Tunnel

RunPod auto-injects your SSH key from account settings.

```bash
# Create tunnel
ssh -L 8000:localhost:8000 root@<pod-id>.runpod.net -p <ssh-port>

# Then use localhost
curl http://localhost:8000/v1/models
```

## Loading Progress

With `SHOW_LOADING_PROGRESS=true`, you'll see:
```
[LOADING] ############-------- 45.2GB / 80GB - loading weights... (8s)
[LOADING] ##################-- 66.8GB / 80GB - compiling kernels... (20s)
============================================================
  [OK] MODEL READY (loaded in 35s)
       API available at http://0.0.0.0:8000/v1
============================================================
```

## GPU Auto-Detection

The image automatically configures the optimal backend:

| GPU | Backend | Notes |
|-----|---------|-------|
| A100 | TRITON_ATTN | Required for gpt-oss attention sinks |
| H100/H200 | FLASH_ATTN | FlashAttention 3, fastest |
| L40S | TRITON_ATTN | 20b model only |

## Test the API

```bash
# Health check
curl http://localhost:8000/health

# List models
curl http://localhost:8000/v1/models

# Chat completion
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-oss-120b",
    "messages": [{"role": "user", "content": "What is 2+2?"}],
    "max_tokens": 100
  }'

# With tools
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-oss-120b",
    "messages": [{"role": "user", "content": "What is the weather in Paris?"}],
    "tools": [{"type":"function","function":{"name":"get_weather","parameters":{"type":"object","properties":{"location":{"type":"string"}}}}}]
  }'
```

## Troubleshooting

**KV cache error (can't fit 128K context):**
- Increase `GPU_MEMORY_UTILIZATION` to `0.95`
- Or reduce `MAX_MODEL_LEN` to `65536`

**Slow first request:**
- Model downloads on first run (~63GB). Use persistent volume.

**Connection timeout:**
- Use TCP port instead of HTTP to bypass Cloudflare 30s limit

**SSH not working:**
- Add your public key in RunPod account settings
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
