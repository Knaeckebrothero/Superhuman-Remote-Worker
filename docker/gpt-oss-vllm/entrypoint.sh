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
#
# SSH Access (for RunPod tunneling):
#   Set SSH_PASSWORD to enable SSH server on port 22

set -e

# =============================================================================
# SSH Server (optional, for RunPod tunnel access)
# =============================================================================

# RunPod injects PUBLIC_KEY from your account settings automatically
# You can also override with SSH_PUBLIC_KEY at pod level
SSH_KEY="${PUBLIC_KEY:-${SSH_PUBLIC_KEY:-}}"

if [ -n "${SSH_KEY}" ]; then
    mkdir -p /root/.ssh
    echo "${SSH_KEY}" > /root/.ssh/authorized_keys
    chmod 700 /root/.ssh
    chmod 600 /root/.ssh/authorized_keys
    /usr/sbin/sshd
    echo "SSH server started on port 22 (key-based auth)"
    echo "Connect with: ssh -L 8000:localhost:8000 root@<pod-ip> -p <ssh-port>"
elif [ -n "${SSH_PASSWORD}" ]; then
    # Fallback to password auth if no key provided
    echo "root:${SSH_PASSWORD}" | chpasswd
    /usr/sbin/sshd
    echo "SSH server started on port 22 (password auth)"
    echo "Connect with: ssh -L 8000:localhost:8000 root@<pod-ip> -p <ssh-port>"
fi

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

# Auto-configure attention backend and KV cache based on GPU
# gpt-oss models require attention sinks, which have different support per GPU:
#   - Hopper (H100/H200): FlashAttention 3 supports sinks
#   - Ampere (A100): Requires TRITON_ATTN_VLLM_V1 backend (vLLM #22290)
#   - Ada (L40S): Limited support, use Triton backend
#
# FP8 KV cache is incompatible with sinks on most backends:
#   - FlashAttention falls back to XFormers when FP8 KV is enabled
#   - XFormers doesn't support sinks -> crash
#   - Solution: Disable FP8 KV cache on Ampere, use auto elsewhere

# Check if user explicitly wants to override backend (via VLLM_ATTENTION_BACKEND_OVERRIDE)
USER_BACKEND_OVERRIDE="${VLLM_ATTENTION_BACKEND_OVERRIDE:-}"

case "${GPU_ARCH}" in
    "ampere")
        # Ampere REQUIRES TRITON_ATTN - FlashAttention doesn't support sinks on sm_80
        if [ -n "${USER_BACKEND_OVERRIDE}" ]; then
            export VLLM_ATTENTION_BACKEND="${USER_BACKEND_OVERRIDE}"
            echo "Note: Using user-specified backend ${USER_BACKEND_OVERRIDE} for Ampere GPU (WARNING: may crash)"
        else
            export VLLM_ATTENTION_BACKEND="TRITON_ATTN_VLLM_V1"
            echo "Note: Using TRITON_ATTN_VLLM_V1 backend for Ampere GPU (required for gpt-oss sinks)"
        fi
        # Force auto KV cache - FP8 causes XFormers fallback which doesn't support sinks
        if [ "${KV_CACHE_DTYPE}" = "fp8" ]; then
            echo "Warning: FP8 KV cache not supported on Ampere with gpt-oss (sinks incompatible)"
            echo "         Overriding KV_CACHE_DTYPE to 'auto'"
            export KV_CACHE_DTYPE="auto"
        fi
        ;;
    "ada")
        # Ada REQUIRES TRITON_ATTN - FlashAttention doesn't support sinks on sm_89
        if [ -n "${USER_BACKEND_OVERRIDE}" ]; then
            export VLLM_ATTENTION_BACKEND="${USER_BACKEND_OVERRIDE}"
            echo "Note: Using user-specified backend ${USER_BACKEND_OVERRIDE} for Ada GPU (WARNING: may crash)"
        else
            export VLLM_ATTENTION_BACKEND="TRITON_ATTN_VLLM_V1"
            echo "Note: Using TRITON_ATTN_VLLM_V1 backend for Ada GPU (required for gpt-oss sinks)"
        fi
        if [ "${KV_CACHE_DTYPE}" = "fp8" ]; then
            echo "Warning: FP8 KV cache not supported on Ada with gpt-oss (sinks incompatible)"
            echo "         Overriding KV_CACHE_DTYPE to 'auto'"
            export KV_CACHE_DTYPE="auto"
        fi
        ;;
    "hopper")
        # Hopper supports FlashAttention 3 with sinks
        if [ -n "${USER_BACKEND_OVERRIDE}" ]; then
            export VLLM_ATTENTION_BACKEND="${USER_BACKEND_OVERRIDE}"
        else
            export VLLM_ATTENTION_BACKEND="FLASH_ATTN"
        fi
        echo "Note: Using ${VLLM_ATTENTION_BACKEND} backend with FlashAttention 3 for Hopper GPU"
        ;;
    "blackwell")
        # Blackwell has native MXFP4 tensor cores
        if [ -n "${USER_BACKEND_OVERRIDE}" ]; then
            export VLLM_ATTENTION_BACKEND="${USER_BACKEND_OVERRIDE}"
        else
            export VLLM_ATTENTION_BACKEND="FLASHINFER"
        fi
        echo "Note: Using ${VLLM_ATTENTION_BACKEND} backend with native MXFP4 for Blackwell GPU"
        ;;
    *)
        # Unknown GPU - use safe default (TRITON_ATTN supports sinks)
        if [ -n "${USER_BACKEND_OVERRIDE}" ]; then
            export VLLM_ATTENTION_BACKEND="${USER_BACKEND_OVERRIDE}"
        else
            export VLLM_ATTENTION_BACKEND="TRITON_ATTN_VLLM_V1"
        fi
        echo "Warning: Unknown GPU architecture, using ${VLLM_ATTENTION_BACKEND} backend"
        ;;
esac

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
# - "auto" for KV cache dtype - FP8 is incompatible with gpt-oss attention sinks
#   on most backends (only works on Hopper with FlashAttention 3 in newer vLLM)
QUANTIZATION="${QUANTIZATION:-auto}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-auto}"

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
# IMPORTANT: Chunked prefill is INCOMPATIBLE with prefix caching
# See vLLM Issue #14069 - causes "State-Cache Impedance Mismatch"
# When both are enabled, chunked prefill breaks cache token alignment,
# causing cache misses and degraded performance. Disable chunked prefill
# when using prefix caching (which we do for agent workloads).
ENABLE_CHUNKED_PREFILL="${ENABLE_CHUNKED_PREFILL:-false}"

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

# Get vLLM version dynamically
VLLM_VERSION=$(python3 -c "import vllm; print(vllm.__version__)" 2>/dev/null || echo "unknown")

echo "=============================================="
echo "  gpt-oss-vllm (vLLM ${VLLM_VERSION})"
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
echo "=============================================="

# =============================================================================
# Loading Progress Monitor (optional)
# =============================================================================

SHOW_LOADING_PROGRESS="${SHOW_LOADING_PROGRESS:-false}"

# Calculate expected memory footprint
if [[ "${MODEL}" == *"120b"* ]]; then
    WEIGHTS_GB="~63GB"
    EXPECTED_MEM_GB=63
    LOAD_TIME="2-5 minutes"
elif [[ "${MODEL}" == *"20b"* ]]; then
    WEIGHTS_GB="~16GB"
    EXPECTED_MEM_GB=16
    LOAD_TIME="30-60 seconds"
else
    WEIGHTS_GB="unknown"
    EXPECTED_MEM_GB=50
    LOAD_TIME="varies"
fi

echo ""
echo "============================================================"
echo "                     LOADING MODEL"
echo "============================================================"
echo "  Model:         ${MODEL}"
echo "  Weights size:  ${WEIGHTS_GB}"
echo "  Est. time:     ${LOAD_TIME}"
echo "------------------------------------------------------------"
if [ "${SHOW_LOADING_PROGRESS}" = "true" ]; then
echo "  Loading progress monitor: ENABLED"
else
echo "  Tip: Set SHOW_LOADING_PROGRESS=true for real-time status"
fi
echo "============================================================"
echo ""

# GPU Memory Loading Progress Monitor
# Runs in background and shows real-time loading progress
start_loading_monitor() {
    local expected_mem=$1
    local port=$2
    local check_interval=2
    local last_mem=0
    local stable_count=0
    local start_time=$(date +%s)

    while true; do
        sleep ${check_interval}

        # Check if vLLM is ready (health endpoint)
        if curl -sf "http://localhost:${port}/health" > /dev/null 2>&1; then
            local end_time=$(date +%s)
            local elapsed=$((end_time - start_time))
            echo ""
            echo "============================================================"
            echo "  [OK] MODEL READY (loaded in ${elapsed}s)"
            echo "       API available at http://0.0.0.0:${port}/v1"
            echo "============================================================"
            echo ""
            break
        fi

        # Get GPU memory usage
        if command -v nvidia-smi &> /dev/null; then
            local mem_info=$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits | head -n1)
            local mem_used=$(echo "$mem_info" | cut -d',' -f1 | tr -d ' ')
            local mem_total=$(echo "$mem_info" | cut -d',' -f2 | tr -d ' ')

            # Convert to GB
            local mem_used_gb=$(awk "BEGIN {printf \"%.1f\", ${mem_used}/1024}")
            local mem_total_gb=$(awk "BEGIN {printf \"%.1f\", ${mem_total}/1024}")

            # Calculate memory delta to detect phase
            local mem_delta=$(awk "BEGIN {printf \"%.0f\", ${mem_used} - ${last_mem}}")
            last_mem=${mem_used}

            # Determine loading phase based on memory change rate
            local phase=""
            local mem_used_int=${mem_used_gb%.*}

            if [ "${mem_used_int}" -lt 2 ]; then
                phase="initializing vLLM..."
            elif [ "${mem_delta}" -gt 500 ]; then
                # Memory increasing rapidly = loading weights
                phase="loading weights..."
                stable_count=0
            elif [ "${mem_delta}" -gt 50 ]; then
                # Memory increasing slowly = building caches
                phase="building CUDA graphs..."
                stable_count=0
            else
                # Memory stable = waiting for startup or compiling
                stable_count=$((stable_count + 1))
                if [ "${stable_count}" -lt 3 ]; then
                    phase="compiling kernels..."
                else
                    phase="waiting for server startup..."
                fi
            fi

            # Build progress bar based on expected memory (weight loading phase)
            # Cap at 95% until health check passes
            local progress=$(awk "BEGIN {p=(${mem_used_int}/${expected_mem})*95; if(p>95) p=95; printf \"%.0f\", p}")
            local filled=$(awk "BEGIN {printf \"%.0f\", (${progress}/100)*20}")
            local empty=$((20 - filled))
            local bar=$(printf "%${filled}s" | tr ' ' '#')$(printf "%${empty}s" | tr ' ' '-')

            local elapsed=$(($(date +%s) - start_time))
            echo "[LOADING] ${bar} ${mem_used_gb}GB / ${mem_total_gb}GB - ${phase} (${elapsed}s)"
        fi
    done
}

# Start loading monitor in background if enabled
if [ "${SHOW_LOADING_PROGRESS}" = "true" ]; then
    echo "[LOADING] Starting GPU memory monitor..."
    echo ""
    start_loading_monitor "${EXPECTED_MEM_GB}" "${PORT}" &
    MONITOR_PID=$!

    # Ensure monitor is killed if script exits
    trap "kill ${MONITOR_PID} 2>/dev/null" EXIT
fi

# Start vLLM (replaces shell process, but monitor keeps running)
exec ${CMD}
