#!/bin/bash
# Optimized entrypoint for gpt-oss models (20b and 120b)
# Supports: A100-80GB, H100-80GB, H200, L40S (20b only)
# All defaults tuned for single-GPU agent workloads (sequential requests)
#
# Model memory requirements (MXFP4 native):
#   - gpt-oss-120b: ~63GB (fits on A100-80GB, H100-80GB, H200)
#   - gpt-oss-20b:  ~16GB (fits on L40S, RTX 4090, and above)
#
# GPU-specific behavior:
#   - A100 (sm_80): Uses TRITON_ATTN backend (FA3 sinks not supported)
#   - H100/H200 (sm_90): Uses FLASH_ATTN with FA3 for best performance
#   - L40S (sm_89): Uses FLASH_ATTN with FA2

set -e

# =============================================================================
# GPU Detection and Auto-Configuration
# =============================================================================

detect_gpu_arch() {
    # Detect GPU compute capability to configure optimal settings
    # Returns: "ampere" (sm_80), "ada" (sm_89), "hopper" (sm_90), or "unknown"
    if command -v nvidia-smi &> /dev/null; then
        local gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)
        case "$gpu_name" in
            *A100*|*A10*|*A30*|*A40*)
                echo "ampere"
                ;;
            *L40*|*L4*|*RTX*40*|*RTX*Ada*)
                echo "ada"
                ;;
            *H100*|*H200*|*H800*)
                echo "hopper"
                ;;
            *B100*|*B200*)
                echo "blackwell"
                ;;
            *)
                echo "unknown"
                ;;
        esac
    else
        echo "unknown"
    fi
}

GPU_ARCH=$(detect_gpu_arch)
echo "Detected GPU architecture: ${GPU_ARCH}"

# Auto-configure attention backend based on GPU
# A100 (Ampere) has issues with FlashAttention 3 sinks (vLLM #22290)
if [ "${GPU_ARCH}" = "ampere" ]; then
    export VLLM_ATTENTION_BACKEND="${VLLM_ATTENTION_BACKEND:-TRITON_ATTN}"
    echo "Note: Using TRITON_ATTN backend for Ampere GPU (FA3 sinks not supported)"
fi

# =============================================================================
# Default configuration (can be overridden via environment variables)
# =============================================================================

# Model settings
MODEL="${MODEL:-openai/gpt-oss-120b}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"

# Memory settings - tuned for 80GB GPUs running 120b (~63GB model)
# 0.95 recommended for 120b to maximize KV cache headroom
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.95}"

# Quantization and KV cache
# - "auto" for quantization lets vLLM use model's native MXFP4
# - "fp8" for KV cache gives 50% memory reduction with minimal quality loss
QUANTIZATION="${QUANTIZATION:-auto}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"

# Batching settings - conservative for agent workloads (sequential requests)
# Lower MAX_NUM_BATCHED_TOKENS = better inter-token latency (ITL)
# Higher MAX_NUM_BATCHED_TOKENS = better time-to-first-token (TTFT)
MAX_NUM_SEQS="${MAX_NUM_SEQS:-64}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-4096}"

# Performance flags
# Note: ASYNC_SCHEDULING disabled by default - causes gibberish in vLLM v0.11.0
# Safe to enable on v0.10.2, but test thoroughly before production use
ASYNC_SCHEDULING="${ASYNC_SCHEDULING:-false}"
ENABLE_PREFIX_CACHING="${ENABLE_PREFIX_CACHING:-true}"
ENABLE_CHUNKED_PREFILL="${ENABLE_CHUNKED_PREFILL:-true}"

# Tool calling - REQUIRED for gpt-oss harmony format
ENABLE_AUTO_TOOL_CHOICE="${ENABLE_AUTO_TOOL_CHOICE:-true}"
TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-openai}"

# API settings
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
API_KEY="${API_KEY:-}"

# Logging
LOG_LEVEL="${LOG_LEVEL:-info}"

# =============================================================================
# Build command
# =============================================================================

CMD="vllm serve ${MODEL}"

# Core settings
CMD="${CMD} --host ${HOST}"
CMD="${CMD} --port ${PORT}"
CMD="${CMD} --max-model-len ${MAX_MODEL_LEN}"
CMD="${CMD} --tensor-parallel-size ${TENSOR_PARALLEL_SIZE}"
CMD="${CMD} --gpu-memory-utilization ${GPU_MEMORY_UTILIZATION}"

# Batching
CMD="${CMD} --max-num-seqs ${MAX_NUM_SEQS}"
CMD="${CMD} --max-num-batched-tokens ${MAX_NUM_BATCHED_TOKENS}"

# Quantization and KV cache
if [ "${QUANTIZATION}" != "auto" ] && [ -n "${QUANTIZATION}" ]; then
    CMD="${CMD} --quantization ${QUANTIZATION}"
fi

if [ "${KV_CACHE_DTYPE}" != "auto" ] && [ -n "${KV_CACHE_DTYPE}" ]; then
    CMD="${CMD} --kv-cache-dtype ${KV_CACHE_DTYPE}"
    # Enable KV scale calculation for fp8 KV cache (ensures proper scaling)
    if [ "${KV_CACHE_DTYPE}" = "fp8" ]; then
        CMD="${CMD} --calculate-kv-scales"
    fi
fi

# Performance optimizations
if [ "${ASYNC_SCHEDULING}" = "true" ]; then
    CMD="${CMD} --async-scheduling"
fi

if [ "${ENABLE_PREFIX_CACHING}" = "true" ]; then
    CMD="${CMD} --enable-prefix-caching"
fi

if [ "${ENABLE_CHUNKED_PREFILL}" = "true" ]; then
    CMD="${CMD} --enable-chunked-prefill"
fi

# Tool calling (CRITICAL for gpt-oss harmony format)
if [ "${ENABLE_AUTO_TOOL_CHOICE}" = "true" ]; then
    CMD="${CMD} --enable-auto-tool-choice"
fi

if [ -n "${TOOL_CALL_PARSER}" ]; then
    CMD="${CMD} --tool-call-parser ${TOOL_CALL_PARSER}"
fi

# API key (optional)
if [ -n "${API_KEY}" ]; then
    CMD="${CMD} --api-key ${API_KEY}"
fi

# Trust remote code (required for gpt-oss)
CMD="${CMD} --trust-remote-code"

# Logging
CMD="${CMD} --uvicorn-log-level ${LOG_LEVEL}"

# Append any additional arguments passed to the container
CMD="${CMD} $@"

# =============================================================================
# Print configuration and start
# =============================================================================

echo "=============================================="
echo "  gpt-oss-vllm (vLLM v0.10.2)"
echo "=============================================="
echo ""
echo "GPU Architecture:   ${GPU_ARCH}"
echo "Attention Backend:  ${VLLM_ATTENTION_BACKEND:-FLASH_ATTN}"
echo ""
echo "Model:              ${MODEL}"
echo "Max context:        ${MAX_MODEL_LEN}"
echo "Tensor parallel:    ${TENSOR_PARALLEL_SIZE}"
echo "GPU mem util:       ${GPU_MEMORY_UTILIZATION}"
echo "Quantization:       ${QUANTIZATION}"
echo "KV cache dtype:     ${KV_CACHE_DTYPE}"
echo "Max batched tokens: ${MAX_NUM_BATCHED_TOKENS}"
echo ""
echo "Async scheduling:   ${ASYNC_SCHEDULING}"
echo "Prefix caching:     ${ENABLE_PREFIX_CACHING}"
echo "Chunked prefill:    ${ENABLE_CHUNKED_PREFILL}"
echo "Auto tool choice:   ${ENABLE_AUTO_TOOL_CHOICE}"
echo "Tool call parser:   ${TOOL_CALL_PARSER}"
echo ""
echo "Endpoint: http://${HOST}:${PORT}/v1"
echo ""
echo "Starting vLLM..."
echo "=============================================="
echo ""

exec ${CMD}
