# llama.cpp Optimization for gpt-oss Models

Resolving Sliding Window Attention and KV Cache Reuse conflicts for gpt-oss-120b/20b on datacenter GPUs (A100/H100/Blackwell).

## The Problem

llama.cpp disables cache reuse for gpt-oss models, causing **severe performance degradation** in multi-turn conversations. Additionally, default configurations often produce **gibberish output** on long contexts due to improper KV cache management.

**Symptoms**:
- `cache_reuse is not supported by this context, it will be disabled`
- Time-to-First-Token (TTFT) increases linearly with conversation length
- Gibberish or infinite loops ("GGGG...") on long context prompts

## Root Cause

### Cache Reuse Disabled
The GGUF file contains `gpt-oss.attention.sliding_window = 128` which triggers a hard-coded safety check:

- **Sliding Window Attention (SWA)** uses a ring buffer (overwrites old tokens)
- **Prefix caching** uses a linear buffer (needs stable slot positions)
- These are fundamentally incompatible
- llama.cpp safeguard: "If SWA is enabled, disable cache reuse"

### The "Gibberish" Failure Mode
gpt-oss uses **alternating attention topology**:
- **Odd layers**: Full attention (attend to entire history)
- **Even layers**: Sliding window (128 tokens only)

When llama.cpp prunes the KV cache based on the 128-token window, the odd layers lose access to "attention sinks" (initial tokens). The model relies on these sinks to anchor attention - without them, attention scores explode or become uniform, causing output collapse.

## Solution Overview

Two approaches to fix the SWA/cache conflict:

| Approach | Method | Pros | Cons |
|----------|--------|------|------|
| **GGUF Patching** | Remove sliding_window metadata | Permanent fix, enables cache reuse | Requires disk space, modifies file |
| **--swa-full Flag** | Force full KV cache at runtime | No file modification needed | Must specify every launch |

**Recommendation**: Use GGUF patching for production, `--swa-full` for testing.

---

## Solution A: Patch the GGUF Metadata

Remove the sliding window metadata to force llama.cpp to treat the model as full-attention.

### Step 1: Install GGUF Tools

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp/gguf-py
pip install .
```

### Step 2: Verify the Metadata

```bash
gguf-dump.py /path/to/gpt-oss-120b.gguf | grep sliding
# Expected: gpt-oss.attention.sliding_window u32 = 128
```

### Step 3: Remove the Metadata

**Option A: Create a patched copy** (safe, requires ~120GB disk space)

```bash
python3 gguf-new-metadata.py \
  --remove-metadata gpt-oss.attention.sliding_window \
  /path/to/gpt-oss-120b.gguf \
  /path/to/gpt-oss-120b-NO-SWA.gguf
```

**Option B: In-place modification** (risky, backup first!)

```bash
python3 gguf-set-metadata.py \
  /path/to/gpt-oss-120b.gguf \
  gpt-oss.attention.sliding_window \
  131072 \
  --type u32
```

### Why This Is Safe

- gpt-oss is a **hybrid attention model**, not pure SWA
- Uses "attention sinks" (first few tokens always retained)
- Allowing full attention is benign for RoPE/YaRN models
- Community reports no quality degradation

---

## Solution B: Runtime Flag (--swa-full)

If GGUF patching is not possible, use the `--swa-full` flag:

```bash
./llama-server -m /path/to/gpt-oss-120b.gguf \
  --swa-full \
  -c 65536 \
  ...
```

This forces llama.cpp to allocate a full linear KV cache regardless of the sliding window metadata. **Critical for stability on long contexts.**

---

## Compilation for Datacenter GPUs

Standard pre-compiled binaries may lack architecture-specific optimizations. Build from source for maximum performance.

### A100/H100 (Ampere/Hopper)

```bash
cmake -B build \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES="80;90" \
  -DGGML_CUDA_ENABLE_UNIFIED_MEMORY=0 \
  -DLLAMA_CURL=ON

cmake --build build --config Release -j $(nproc)
```

**Critical flags**:
- `CMAKE_CUDA_ARCHITECTURES="80;90"`: Targets A100 (SM80) and H100 (SM90) tensor cores
- `GGML_CUDA_ENABLE_UNIFIED_MEMORY=0`: **Critical for performance** - prevents silent PCIe transfers that destroy throughput

### Blackwell (B200)

```bash
cmake -B build \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES="120;121" \
  -DGGML_CUDA_ENABLE_UNIFIED_MEMORY=0

cmake --build build --config Release -j $(nproc)
```

Native MXFP4 tensor core support provides ~25-33% prefill improvement.

---

## Optimal Runtime Configuration

### Single GPU (A100/H100 80GB)

```bash
export CUDA_VISIBLE_DEVICES=0
export GGML_CUDA_ENABLE_UNIFIED_MEMORY=0

./llama-server \
  --model /path/to/gpt-oss-120b-NO-SWA.gguf \
  --ctx-size 65536 \
  --n-gpu-layers 999 \
  --threads 16 \
  --batch-size 4096 \
  --ubatch-size 4096 \
  --flash-attn on \
  --swa-full \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --cache-reuse 256 \
  --no-mmap \
  --mlock
```

### Dual GPU with NVLink (Maximum Throughput)

```bash
export CUDA_VISIBLE_DEVICES=0,1

./llama-server \
  --model /path/to/gpt-oss-120b-NO-SWA.gguf \
  --ctx-size 131072 \
  --n-gpu-layers 999 \
  --split-mode row \
  --tensor-split 1,1 \
  --threads 32 \
  --batch-size 8192 \
  --ubatch-size 4096 \
  --flash-attn on \
  --swa-full \
  --cache-type-k f16 \
  --cache-type-v f16 \
  --cache-reuse 256 \
  --no-mmap
```

**Note**: Row split (`--split-mode row`) requires NVLink. For PCIe-only, use `--split-mode layer`.

---

## Flag Reference

### Memory Management

| Flag | Purpose |
|------|---------|
| `-c 131072` | Full 128K context window |
| `--swa-full` | **Critical**: Force full KV cache (prevents gibberish) |
| `--cache-reuse 256` | Enable prefix caching (256 token threshold) |
| `-ctk q8_0 -ctv q8_0` | Q8 KV cache quantization |
| `-ctk f16 -ctv f16` | FP16 KV cache (more stable, uses more VRAM) |
| `-ngl 999` | Offload all layers to GPU |
| `--no-mmap` | Load model fully into RAM (eliminates disk I/O stalls) |
| `--mlock` | Lock memory to prevent paging |

### Compute Optimization

| Flag | Purpose |
|------|---------|
| `-fa` / `--flash-attn on` | Flash Attention (mandatory for long context) |
| `-b 4096` / `--batch-size 4096` | Logical batch size for prefill |
| `-ub 4096` / `--ubatch-size 4096` | Physical batch size (larger = better MoE expert reuse) |
| `--threads 16` | CPU threads for generation |
| `--threads-batch 32` | CPU threads for prefill |

### Multi-GPU

| Flag | Purpose |
|------|---------|
| `--split-mode row` | Tensor parallelism (requires NVLink) |
| `--split-mode layer` | Pipeline parallelism (for PCIe) |
| `--tensor-split 1,1` | Even load balancing across GPUs |

### MoE-Specific Offloading (Limited VRAM)

```bash
./llama-server ... -ngl 99 -ot ".*ffn.*"
```

Keeps attention on GPU while experts stream from CPU RAM. **Warning**: Drops generation from ~50 t/s to ~2-5 t/s.

---

## Performance Benchmarks

### 60K Token Context

| Configuration | Hardware | Prefill | Generation | Stability |
|--------------|----------|---------|------------|-----------|
| Baseline (default) | 1x A100 80GB | ~1,300 t/s | ~40 t/s | Low (loops) |
| Optimized | 1x A100 80GB | ~1,900 t/s | ~55 t/s | High |
| NVLink | 2x A100 NVLink | ~3,400 t/s | ~85 t/s | High |
| Bleeding Edge | 1x H100 80GB | ~2,400 t/s | ~65 t/s | Medium |

### Cache Reuse Impact (4K Token History)

| Scenario | Time per turn |
|----------|--------------|
| Without cache reuse | ~143 seconds |
| With cache reuse | ~12 seconds |
| **Improvement** | **12x faster** |

---

## VRAM Requirements

### KV Cache (Surprisingly Efficient)

Due to aggressive Grouped Query Attention (only 8 KV heads), gpt-oss has small KV cache:

| Context | FP16 | Q8_0 |
|---------|------|------|
| 32K tokens | ~2.4 GB | ~1.2 GB |
| 60K tokens | ~4.5 GB | ~2.3 GB |
| 128K tokens | ~9.6 GB | ~4.8 GB |

### Total VRAM

| Component | Size |
|-----------|------|
| Model Weights (MXFP4) | ~59-64 GB |
| KV Cache (60K, Q8) | ~2.3 GB |
| KV Cache (60K, FP16) | ~4.5 GB |
| Activation Buffers | ~5-10 GB |
| **Total (60K, Q8)** | **~70 GB** |
| **Total (60K, FP16)** | **~75 GB** |

**Result**: A single A100-80GB can fit model + 60K context with Q8 KV cache.

### Hardware Recommendations

| Setup | VRAM | Max Context | Notes |
|-------|------|-------------|-------|
| 1x A100 80GB | 80 GB | ~65K | Optimal single-GPU |
| 1x H100 80GB | 80 GB | ~65K | Faster, bleeding edge |
| 2x A100 NVLink | 160 GB | 128K+ | Full context, 2x throughput |
| 2x RTX 3090/4090 | 48 GB | ~32K | Requires expert offloading |

---

## Validation

After configuration, verify in logs:

1. `flash_attn = 1` - Flash Attention active
2. `n_swa = 0` or "SWA disabled/full" - Full KV cache allocated
3. `offloaded 36/36 layers` - No CPU offloading
4. No `cache_reuse is not supported` warning
5. `n_past` increases on subsequent requests (not resetting to 0)

---

## Troubleshooting

### Gibberish Output / Infinite Loops
1. **Add `--swa-full`** - This is the primary fix
2. If persists, use FP16 KV cache: `-ctk f16 -ctv f16`
3. Reduce context if needed: `-c 32768`
4. Try `--repeat-penalty 1.05 --temp 0.1` (treats symptom, not cause)

### Cache Reuse Still Disabled
1. Verify GGUF was patched: `gguf-dump.py model.gguf | grep sliding`
2. Ensure `--cache-reuse 256` flag is set
3. With `--swa-full`, cache should be valid and linear

### Out of Memory
1. Use Q8 KV cache: `-ctk q8_0 -ctv q8_0`
2. Reduce context: `-c 32768` or `-c 65536`
3. Reduce batch size: `-ub 2048`
4. Offload experts (last resort): `-ot ".*ffn.*"`

### Slow Multi-GPU Performance
1. Verify NVLink is present for `--split-mode row`
2. For PCIe-only, use `--split-mode layer` instead
3. Check `--tensor-split` is balanced

---

## References

### GitHub Issues & Discussions
- [#15986](https://github.com/ggml-org/llama.cpp/discussions/15986) - Cache reuse investigation
- [#15894](https://github.com/ggml-org/llama.cpp/issues/15894) - Prompt reprocessing bug
- [#15112](https://github.com/ggml-org/llama.cpp/issues/15112) - Gibberish with long context
- [#15082](https://github.com/ggml-org/llama.cpp/issues/15082) - Cache reuse prefix bug
- [#15396](https://github.com/ggml-org/llama.cpp/discussions/15396) - Running gpt-oss guide

### Documentation
- [GGUF-py Tools](https://github.com/ggml-org/llama.cpp/blob/master/gguf-py/README.md)
- [Unsloth gpt-oss Guide](https://unsloth.ai/docs/models/gpt-oss-how-to-run-and-fine-tune)
- [NVIDIA Megatron gpt-oss](https://docs.nvidia.com/nemo/megatron-bridge/latest/models/llm/gpt-oss.html)
