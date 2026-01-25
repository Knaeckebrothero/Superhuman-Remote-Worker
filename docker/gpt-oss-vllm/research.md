# Optimizing vLLM for OpenAI gpt-oss Models: Configuration Guide

OpenAI's gpt-oss models (released August 2025) are real open-weight models using MXFP4 quantization and Harmony format—but **critical specification errors** in the original request require immediate correction. Most notably, gpt-oss-20b is **not** a dense BF16 model requiring 40GB; it's an MoE model using MXFP4 that fits in ~16GB. Additionally, **L40S-48GB cannot fit gpt-oss-120b** (which requires ~63GB), and vLLM's `--async-scheduling` flag has known bugs producing gibberish in v0.11.0.

---

## Verified model specifications vs. original claims

The research revealed significant discrepancies between the user's specifications and official sources.

| Specification | Original Claim | **Verified (Official)** | Status |
|--------------|----------------|------------------------|--------|
| gpt-oss-120b total params | 120B | **117B** | ~Close |
| gpt-oss-120b active params | ~5.7B | **5.1B** | ~Close |
| gpt-oss-120b architecture | Sparse MoE | **MoE** | ✅ Correct |
| gpt-oss-120b memory | ~60GB weights | **~63GB** | ✅ Close |
| gpt-oss-20b architecture | **Dense transformer** | **MoE (32 experts, Top-4)** | ❌ Wrong |
| gpt-oss-20b precision | **BF16 native, ~40GB** | **MXFP4, ~16GB** | ❌ Wrong |
| gpt-oss-20b active params | Not specified | **3.6B** | N/A |
| Harmony format | Yes | ✅ Confirmed | ✅ Correct |
| openai-harmony package | Rust + Python | ✅ PyPI v0.0.8, crates.io | ✅ Correct |

**Critical correction**: gpt-oss-20b is an MoE model with 21B total parameters, 3.6B active per forward pass, using MXFP4 quantization—meaning it fits comfortably on **RTX 4090 (24GB)** or **L40S (48GB)** with substantial headroom, not the 40GB+ originally assumed.

---

## vLLM configuration for MXFP4 MoE models

### Current vLLM version status

The latest stable release is **v0.11.0** (October 2025), not v0.12.0, which remains in the milestone stage. However, **v0.10.2 is recommended** for gpt-oss deployments due to critical bugs in v0.11.0's tool calling that cause infinite hangs.

### MXFP4 support in vLLM

vLLM supports MXFP4 natively through multiple backends. On **Hopper GPUs (H100/H200)**, vLLM uses Triton matmul_ogs kernels optimized for MXFP4 inference via software emulation—there is no `triton_kernels.matmul_ogs` as a standalone import. On **Blackwell GPUs (B200/B300)**, native MXFP4 tensor core acceleration provides 2.2x end-to-end speedup over BF16. Critically, **A100 and L40S lack native MXFP4 support**—they fall back to BF16 dequantization, significantly increasing memory requirements.

### MoE-specific flags

vLLM offers comprehensive MoE support through expert parallelism rather than tensor parallelism for optimal performance:

- `--enable-expert-parallel` — Uses EP instead of TP for MoE layers
- `--enable-eplb` — Expert Parallel Load Balancing for routing optimization  
- `--enable-dbo` — Dual Batch Overlap (requires multi-GPU with DeepEP backend)
- `--all2all-backend pplx` or `deepep_low_latency` — Communication backend selection

For **single-GPU deployment**, expert parallelism isn't applicable; use `--tensor-parallel-size 1` and let vLLM handle MoE routing internally.

### Tool call parser situation

There is **no `gpt_oss` or `harmony` parser** in vLLM's standard toolset. The Harmony format support is under active development (GitHub Issue #23217) but incomplete for streaming. Currently supported parsers include: `hermes`, `mistral`, `llama3_json`, `granite`, `internlm`, `jamba`, `xlam`, `qwen3`, `seedoss`, and `glm45`. For gpt-oss models, use `--tool-call-parser openai --enable-auto-tool-choice` as a workaround until native Harmony parsing is complete.

### Memory and performance flags

| Flag | Recommended Value | Notes |
|------|------------------|-------|
| `--gpu-memory-utilization` | 0.90-0.95 | 0.95 needed for 120b on single 80GB GPU |
| `--kv-cache-dtype` | `fp8` | 50% memory reduction for KV cache |
| `--max-model-len` | 32768-131072 | Depends on available KV cache headroom |
| `--enforce-eager` | Omit (use default) | CUDA graphs improve throughput |
| `--async-scheduling` | **Avoid on v0.11.0** | Causes gibberish output (use v0.10.2) |

---

## Framework comparison matrix

| Framework | MXFP4 Native | MoE Support | Tool Calling | Best For |
|-----------|-------------|-------------|--------------|----------|
| **vLLM** | ✅ Via Quark/Triton | ✅ Excellent (EP, EPLB, DeepEP) | ✅ Full (18+ parsers) | Production NVIDIA deployments |
| **SGLang** | ✅ Via AMD Quark | ✅ Excellent (RadixAttention) | ⚠️ Limited/Unstable | AMD MI300X/MI355X, prefix caching |
| **TensorRT-LLM** | ✅ Native (requires conversion) | ✅ Excellent (CutlassFusedMoE) | ✅ Supported | Maximum Blackwell performance |
| **llama.cpp** | ✅ Native GGUF (PR #15091) | ✅ Basic | ✅ Via Grammar | CPU inference, AMD Strix Halo |

### SGLang vs vLLM for MoE

SGLang achieves **~29% higher throughput** on Llama 3.1 8B benchmarks via FlashInfer but has **unstable tool calling** (GitHub #2429 still open). For multi-turn agentic workloads with heavy tool use, **vLLM remains the safer choice** despite slightly lower raw throughput. SGLang's RadixAttention provides ~10% improvement for repeated prefix patterns.

### llama.cpp MXFP4 loading

llama.cpp can load MXFP4 models **directly as native GGUF** (type code 39) without conversion when using official ggml-org quantizations. Converting to other GGUF formats (Q5_K_M, etc.) shows minimal quality degradation per Unsloth testing. For AMD Strix Halo, the **Vulkan backend (RADV) strongly outperforms ROCm/HIP** due to cooperative matrix extension support.

---

## GPU performance analysis for gpt-oss-120b (~63GB footprint)

| GPU | VRAM | Fits 120b? | Memory BW | Est. tok/s | Best Price | $/1M tokens |
|-----|------|-----------|-----------|------------|------------|-------------|
| **A100-80GB** | 80GB | ✅ Barely (~17GB headroom) | 2.0 TB/s | ~20 | $0.78/hr | $10.83 |
| **H100-80GB** | 80GB | ✅ Barely (~17GB headroom) | 3.35 TB/s | ~37 | $1.24-1.47/hr | $9.31-11.04 |
| **H200-141GB** | 141GB | ✅ Ideal (~78GB headroom) | 4.8 TB/s | ~57 | $2.25-2.50/hr | $10.96-12.18 |
| **L40S-48GB** | 48GB | ❌ **Cannot fit** | 864 GB/s | N/A | N/A | N/A |

**H100's 1.67x bandwidth advantage** translates to approximately **1.5-1.7x faster single-stream decode** versus A100. At batch=1 (the user's agentic workload), performance scales nearly linearly with memory bandwidth since inference is memory-bound.

**H200's extra VRAM becomes critical** for 64K-128K context windows. With 78GB headroom versus H100's ~17GB, H200 supports approximately 4x longer context without batching pressure—essential for long agent reasoning chains.

### Native MXFP4 hardware support

**Only Blackwell GPUs (B200/B300) have native MXFP4 tensor core acceleration.** All GPUs listed above (A100, H100, H200, L40S) require software emulation via Triton kernels on Hopper or BF16 fallback on older architectures. This is a key limitation not reflected in the original specifications.

---

## gpt-oss-20b deployment specifics

Since gpt-oss-20b is actually MoE with ~16GB footprint, it fits comfortably on consumer and enterprise GPUs:

| GPU | VRAM | Fits 20b? | KV Headroom | Max Context | Est. tok/s |
|-----|------|-----------|-------------|-------------|------------|
| **RTX 4090** | 24GB | ✅ Yes | ~8GB | 8K-16K | 45-55 |
| **L40S** | 48GB | ✅ Yes | ~32GB | 64K-128K | 40-60 |
| **H100-80GB** | 80GB | ✅ Yes | ~64GB | 128K | 70-100 |

**L40S is optimal for gpt-oss-20b** with full 128K context support. RTX 4090 works but limits context to ~16K tokens. For consumer deployment, **Ollama provides the most reliable gpt-oss-20b experience** with native MXFP4 support via custom kernels.

---

## Harmony format implementation status

The Harmony format is **real and mandatory** for gpt-oss tool calling. Verified special tokens include:

| Token | Purpose | Token ID |
|-------|---------|----------|
| `<\|start\|>` | Begin message | 200006 |
| `<\|end\|>` | End message | 200007 |
| `<\|channel\|>` | Channel specification (analysis/commentary/final) | 200005 |
| `<\|call\|>` | Tool call stop token | 200012 |
| `<\|return\|>` | Response complete | 200002 |
| `<\|constrain\|>` | Data type constraint | 200003 |

The **openai-harmony package** (v0.0.8) provides Rust-core parsing with Python bindings. Install via `pip install openai-harmony`. vLLM's Harmony support is **in development** (Issue #23217)—streaming works through the Response API but Chat Completions API support remains early-stage.

---

## Ready-to-use configurations

### vLLM for gpt-oss-120b on A100-80GB

```bash
docker run --gpus all -p 8000:8000 --ipc=host \
  -e HUGGING_FACE_HUB_TOKEN=<your_token> \
  vllm/vllm-openai:v0.10.2 \
  --model openai/gpt-oss-120b \
  --tensor-parallel-size 1 \
  --gpu-memory-utilization 0.95 \
  --max-model-len 32768 \
  --kv-cache-dtype fp8 \
  --enable-prefix-caching \
  --enable-chunked-prefill \
  --tool-call-parser openai \
  --enable-auto-tool-choice
```

**Note**: A100 requires TRITON_ATTN backend—FlashAttention 3 sinks are not supported (Issue #22290). Context limited to ~32K on single GPU.

### vLLM for gpt-oss-120b on H100-80GB

```bash
docker run --gpus all -p 8000:8000 --ipc=host \
  -e HUGGING_FACE_HUB_TOKEN=<your_token> \
  vllm/vllm-openai:v0.10.2 \
  --model openai/gpt-oss-120b \
  --tensor-parallel-size 1 \
  --max-model-len 131072 \
  --max-num-batched-tokens 10240 \
  --gpu-memory-utilization 0.85 \
  --kv-cache-dtype fp8 \
  --enable-prefix-caching \
  --enable-chunked-prefill \
  --tool-call-parser openai \
  --enable-auto-tool-choice
```

### vLLM for gpt-oss-20b on L40S-48GB

```bash
docker run --gpus all -p 8000:8000 --ipc=host \
  -e HUGGING_FACE_HUB_TOKEN=<your_token> \
  vllm/vllm-openai:v0.10.2 \
  --model openai/gpt-oss-20b \
  --tensor-parallel-size 1 \
  --max-model-len 131072 \
  --max-num-seqs 64 \
  --gpu-memory-utilization 0.90 \
  --enable-prefix-caching \
  --enable-chunked-prefill \
  --tool-call-parser openai \
  --enable-auto-tool-choice
```

### llama.cpp for gpt-oss-120b on AMD Strix Halo (Vulkan)

```bash
# Build with Vulkan support (RADV driver)
cmake -S . -B build -G Ninja \
  -DGGML_VULKAN=ON \
  -DLLAMA_BUILD_SERVER=ON \
  -DCMAKE_BUILD_TYPE=Release
ninja -C build

# Serve with Vulkan backend
export AMD_VULKAN_ICD=RADV
export GGML_VK_VISIBLE_DEVICES=0

./build/bin/llama-server \
  -hf ggml-org/gpt-oss-120b-GGUF \
  --ctx-size 32768 \
  --jinja \
  -b 4096 -ub 256 \
  --flash-attn \
  --no-mmap \
  -ngl 999
```

**Critical**: Use `--no-mmap` on Strix Halo—mmap causes catastrophic ROCm slowdowns. Vulkan (RADV) achieves ~48 tok/s vs ~30 tok/s with HIP backend.

### Production Dockerfile

```dockerfile
FROM vllm/vllm-openai:v0.10.2

RUN pip install openai-harmony==0.0.8

ENV HUGGING_FACE_HUB_TOKEN=""

EXPOSE 8000

ENTRYPOINT ["vllm", "serve"]
CMD ["openai/gpt-oss-120b", "--tool-call-parser", "openai", "--enable-auto-tool-choice"]
```

---

## Known issues specific to gpt-oss models

| Issue | Severity | Workaround |
|-------|----------|------------|
| **#22290**: Ampere GPUs fail with "Sinks only supported in FlashAttention 3" | High | Use TRITON_ATTN backend or v0.10.2 |
| **#26480**: v0.11.0 tool calling hangs indefinitely (~50% of queries) | Critical | Use vLLM v0.10.2 |
| **#22337**: Tool calls returned in `content` instead of `tool_calls` field | Medium | Must use `--tool-call-parser openai --enable-auto-tool-choice` |
| **#23217**: Harmony format streaming incomplete | Medium | Response API works; Chat Completions early-stage |
| **#23278**: gpt-oss-120b OOM on H100 with defaults | High | Reduce `--max-model-len` or increase `--gpu-memory-utilization` |
| L40S/A100 MXFP4 fallback to BF16 | High | Expect ~40GB memory for 20b, reduced performance |

---

## Cost analysis summary

### gpt-oss-120b ($/million tokens, single-stream decode)

| Provider | GPU | $/hr | $/1M tokens |
|----------|-----|------|-------------|
| **Thunder Compute** | A100-80GB | $0.78 | **$10.83** |
| **Fluence** | H100-80GB | $1.24 | **$9.31** |
| **GMI Cloud** | H200-141GB | $2.50 | $12.18 |
| **Lambda Labs** | H100-80GB | $2.99 | $22.45 |
| **AWS P5** | H100-80GB | $3.86 | $28.98 |

### gpt-oss-20b ($/million tokens, single-stream decode)

| Provider | GPU | $/hr | $/1M tokens |
|----------|-----|------|-------------|
| **Vast.ai** | RTX 4090 | $0.25-0.40 | **$1.54-2.47** |
| **RunPod** | RTX 4090 | $0.34 | $2.10 |
| **Vast.ai** | L40S | $0.55 | $3.06 |
| **RunPod** | L40S | $0.79 | $4.39 |

**Best value for gpt-oss-120b**: Fluence H100 at $9.31/M tokens
**Best value for gpt-oss-20b**: Vast.ai RTX 4090 at $1.54/M tokens

---

## Conclusion

The gpt-oss models represent OpenAI's first significant open-weight release since GPT-2, achieving o3-mini level performance (20b) and near-o4-mini performance (120b). For deployment, **vLLM v0.10.2 with `--tool-call-parser openai` remains the production-ready option** despite Harmony format support being incomplete. The most significant finding is that **gpt-oss-20b is far more accessible than originally specified**—fitting on RTX 4090 consumer hardware at ~$1.50/M tokens via Vast.ai. For gpt-oss-120b, the H100 provides optimal price-performance at ~$9-11/M tokens, while H200's extra VRAM enables the full 128K context window essential for long agentic reasoning chains.