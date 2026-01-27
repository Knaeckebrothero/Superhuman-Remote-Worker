#!/bin/bash
# Optimized entrypoint for gpt-oss-120b via llama.cpp
# Supports: A100-80GB, H100-80GB, H200, L40S
#
# Key advantages over vLLM (stability, not memory):
#   - GBNF grammar constraints prevent parsing failures
#   - Stable tool calling (no v0.11.0 hang bug #26480)
#   - No attention sink backend compatibility issues
#
# Note: Both vLLM and llama.cpp fit 128K context on 80GB GPUs
#       (model is native int4/MXFP4, ~63GB)
#
# GPU-specific behavior:
#   - A100 (sm_80): Standard CUDA, stable
#   - H100/H200 (sm_90): Flash attention, optimal performance
#   - L40S (sm_89): Ada architecture, good performance
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

get_gpu_memory_gb() {
    # Get total GPU memory in GB
    if command -v nvidia-smi &> /dev/null; then
        local mem_mb=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1)
        echo $((mem_mb / 1024))
    else
        echo "80"  # Default assumption
    fi
}

GPU_ARCH=$(detect_gpu_arch)
GPU_MEM_GB=$(get_gpu_memory_gb)
echo "Detected GPU architecture: ${GPU_ARCH}"
echo "Detected GPU memory: ${GPU_MEM_GB}GB"

# =============================================================================
# Default configuration (can be overridden via environment variables)
# =============================================================================
# This container uses the SAME environment variables as vLLM for drop-in compatibility

# Model settings - accept vLLM format and translate to GGUF
MODEL_INPUT="${MODEL:-openai/gpt-oss-120b}"

# Translate vLLM model names to GGUF equivalents
case "${MODEL_INPUT}" in
    "openai/gpt-oss-120b"|"gpt-oss-120b")
        MODEL="ggml-org/gpt-oss-120b-GGUF"
        ;;
    "openai/gpt-oss-20b"|"gpt-oss-20b")
        MODEL="ggml-org/gpt-oss-20b-GGUF"
        ;;
    *"GGUF"*)
        # Already a GGUF model path, use as-is
        MODEL="${MODEL_INPUT}"
        ;;
    *)
        # Unknown model, try as-is (might be a custom GGUF)
        MODEL="${MODEL_INPUT}"
        echo "Note: Using model '${MODEL}' as-is (not a known vLLM model name)"
        ;;
esac

MODEL_FILE="${MODEL_FILE:-}"  # Auto-detect if not specified

# Context settings - accept vLLM's MAX_MODEL_LEN as alias
CTX_SIZE="${MAX_MODEL_LEN:-${CTX_SIZE:-131072}}"
N_GPU_LAYERS="${N_GPU_LAYERS:-999}"

# vLLM compatibility - accept but log (not directly applicable to llama.cpp)
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.95}"

# Memory management - Q8_0 KV cache recommended for quality/memory balance
CACHE_TYPE_K="${CACHE_TYPE_K:-q8_0}"
CACHE_TYPE_V="${CACHE_TYPE_V:-q8_0}"
CACHE_REUSE="${CACHE_REUSE:-256}"  # Prefix caching threshold (requires SWA override to work)

# Sliding Window Full - allocates full KV cache (prevents gibberish on long contexts)
SWA_FULL="${SWA_FULL:-true}"

# Override sliding window metadata to enable KV cache reuse
# gpt-oss GGUF contains sliding_window=128 which forces iSWA (non-unified KV cache)
# This triggers a hard-coded safeguard that disables cache reuse entirely
# Setting to 0 forces unified KV cache, enabling prefix caching for multi-turn conversations
# Safe because gpt-oss uses hybrid attention and --swa-full ensures correct computation
SWA_OVERRIDE="${SWA_OVERRIDE:-true}"

# Batch settings - tuned for datacenter GPUs (A100/H100/H200)
# Larger batches improve MoE expert reuse and throughput
BATCH_SIZE="${BATCH_SIZE:-4096}"
UBATCH_SIZE="${UBATCH_SIZE:-4096}"

# Thread settings
THREADS="${THREADS:-16}"
THREADS_BATCH="${THREADS_BATCH:-32}"  # Prefill can use more threads

# Multi-GPU support
SPLIT_MODE="${SPLIT_MODE:-}"       # "row" for NVLink, "layer" for PCIe
TENSOR_SPLIT="${TENSOR_SPLIT:-}"   # e.g., "1,1" for 2 GPUs equal split

# Performance flags
FLASH_ATTN="${FLASH_ATTN:-true}"
MLOCK="${MLOCK:-true}"             # Lock memory for datacenter stability
NO_MMAP="${NO_MMAP:-true}"         # Avoid mmap for datacenter workloads

# Disable unified memory at runtime (improves performance)
export GGML_CUDA_ENABLE_UNIFIED_MEMORY=0

# Grammar/Tool calling
USE_GRAMMAR="${USE_GRAMMAR:-true}"
GRAMMAR_FILE="${GRAMMAR_FILE:-/app/harmony.gbnf}"

# API settings - use port 8000 by default (same as vLLM)
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
API_KEY="${API_KEY:-}"

# Parallel settings - for agent workloads (sequential requests)
N_PARALLEL="${N_PARALLEL:-1}"

# Logging
VERBOSE="${VERBOSE:-false}"

# =============================================================================
# Auto-configure based on GPU
# =============================================================================

case "${GPU_ARCH}" in
    "ampere")
        echo "Note: A100 detected - using standard CUDA backend"
        # A100 works well with default settings
        ;;
    "ada")
        echo "Note: Ada GPU detected - good performance expected"
        # L40S/RTX 4090 work well with default settings
        ;;
    "hopper")
        echo "Note: Hopper GPU detected - optimal performance"
        # H100/H200 have best performance
        ;;
    "blackwell")
        echo "Note: Blackwell GPU detected - cutting edge performance"
        ;;
    *)
        echo "Warning: Unknown GPU architecture - using conservative defaults"
        ;;
esac

# Adjust context based on available memory
# Use 75GB threshold to handle integer division rounding (80GB GPUs may report as 79GB)
if [ "${GPU_MEM_GB}" -lt 75 ]; then
    if [ "${CTX_SIZE}" -gt 65536 ]; then
        echo "Warning: GPU has ${GPU_MEM_GB}GB, reducing CTX_SIZE to 65536"
        CTX_SIZE=65536
    fi
fi

# =============================================================================
# Build command
# =============================================================================

CMD="/app/llama-server"

# Model source - use HuggingFace download
CMD="${CMD} --hf-repo ${MODEL}"

# If specific model file requested
if [ -n "${MODEL_FILE}" ]; then
    CMD="${CMD} --hf-file ${MODEL_FILE}"
fi

# Context and layers
CMD="${CMD} --ctx-size ${CTX_SIZE}"
CMD="${CMD} --n-gpu-layers ${N_GPU_LAYERS}"

# Memory management - Q8 KV cache recommended for quality/memory balance
CMD="${CMD} --cache-type-k ${CACHE_TYPE_K}"
CMD="${CMD} --cache-type-v ${CACHE_TYPE_V}"
if [ "${CACHE_REUSE}" -gt 0 ]; then
    CMD="${CMD} --cache-reuse ${CACHE_REUSE}"
fi

# Batch settings
CMD="${CMD} --batch-size ${BATCH_SIZE}"
CMD="${CMD} --ubatch-size ${UBATCH_SIZE}"

# Thread settings
CMD="${CMD} --threads ${THREADS}"
CMD="${CMD} --threads-batch ${THREADS_BATCH}"

# Sliding Window Full (CRITICAL for cache reuse to work properly)
if [ "${SWA_FULL}" = "true" ]; then
    CMD="${CMD} --swa-full"
fi

# Multi-GPU support
if [ -n "${SPLIT_MODE}" ]; then
    CMD="${CMD} --split-mode ${SPLIT_MODE}"
fi
if [ -n "${TENSOR_SPLIT}" ]; then
    CMD="${CMD} --tensor-split ${TENSOR_SPLIT}"
fi

# Performance flags
if [ "${FLASH_ATTN}" = "true" ]; then
    CMD="${CMD} --flash-attn on"
elif [ "${FLASH_ATTN}" = "false" ]; then
    CMD="${CMD} --flash-attn off"
fi

if [ "${MLOCK}" = "true" ]; then
    CMD="${CMD} --mlock"
fi

if [ "${NO_MMAP}" = "true" ]; then
    CMD="${CMD} --no-mmap"
fi

# Enable Jinja templating for chat templates
CMD="${CMD} --jinja"

# Grammar for tool calling (GBNF enforces valid Harmony format)
if [ "${USE_GRAMMAR}" = "true" ] && [ -f "${GRAMMAR_FILE}" ]; then
    CMD="${CMD} --grammar-file ${GRAMMAR_FILE}"
    echo "Grammar file enabled: ${GRAMMAR_FILE}"
fi

# Parallel requests
CMD="${CMD} --parallel ${N_PARALLEL}"

# API settings
CMD="${CMD} --host ${HOST}"
CMD="${CMD} --port ${PORT}"

# API key (optional)
if [ -n "${API_KEY}" ]; then
    CMD="${CMD} --api-key ${API_KEY}"
fi

# Verbose output
if [ "${VERBOSE}" = "true" ]; then
    CMD="${CMD} --verbose"
fi

# HuggingFace token for downloading gated models
if [ -n "${HUGGING_FACE_HUB_TOKEN}" ]; then
    export HF_TOKEN="${HUGGING_FACE_HUB_TOKEN}"
fi

# Append any additional arguments passed to the container
CMD="${CMD} $@"

# =============================================================================
# Print configuration and start
# =============================================================================

echo "=============================================="
echo "  gpt-oss-llamacpp (llama.cpp)"
echo "  vLLM-compatible drop-in replacement"
echo "=============================================="
echo ""
echo "GPU Architecture:   ${GPU_ARCH}"
echo "GPU Memory:         ${GPU_MEM_GB}GB"
echo ""
echo "Model (input):      ${MODEL_INPUT}"
echo "Model (GGUF):       ${MODEL}"
echo "Context size:       ${CTX_SIZE}"
echo "GPU layers:         ${N_GPU_LAYERS}"
echo "KV cache type:      K=${CACHE_TYPE_K}, V=${CACHE_TYPE_V}"
echo "Cache reuse:        ${CACHE_REUSE}"
echo "SWA full:           ${SWA_FULL}"
echo "Batch size:         ${BATCH_SIZE}"
echo "Micro-batch:        ${UBATCH_SIZE}"
echo "Threads:            ${THREADS} (batch: ${THREADS_BATCH})"
if [ -n "${SPLIT_MODE}" ]; then
echo "Split mode:         ${SPLIT_MODE}"
fi
if [ -n "${TENSOR_SPLIT}" ]; then
echo "Tensor split:       ${TENSOR_SPLIT}"
fi
echo ""
echo "Flash attention:    ${FLASH_ATTN}"
echo "Memory lock:        ${MLOCK}"
echo "No mmap:            ${NO_MMAP}"
echo "Unified memory:     disabled"
echo "Grammar:            ${USE_GRAMMAR}"
echo "Parallel requests:  ${N_PARALLEL}"
echo ""
echo "Endpoint: http://${HOST}:${PORT}/v1"
echo "(Same port as vLLM for drop-in compatibility)"
echo ""
echo "=============================================="

# =============================================================================
# Loading Progress Monitor
# =============================================================================

SHOW_LOADING_PROGRESS="${SHOW_LOADING_PROGRESS:-false}"

# Calculate expected memory footprint
if [[ "${MODEL}" == *"120b"* ]]; then
    WEIGHTS_GB="~70GB"
    EXPECTED_MEM_GB=70
    EXPECTED_DOWNLOAD_GB=63  # 59.02 GiB GGUF split across 3 files
    LOAD_TIME="3-7 minutes"
else
    WEIGHTS_GB="unknown"
    EXPECTED_MEM_GB=50
    EXPECTED_DOWNLOAD_GB=30
    LOAD_TIME="varies"
fi

# Cache directory where llama.cpp downloads model files
CACHE_DIR="/root/.cache/llama.cpp"

echo ""
echo "============================================================"
echo "                     LOADING MODEL"
echo "============================================================"
echo "  Model:         ${MODEL}"
echo "  Weights size:  ${WEIGHTS_GB} (with KV cache)"
echo "  Est. time:     ${LOAD_TIME} (includes HF download)"
echo "------------------------------------------------------------"
if [ "${SHOW_LOADING_PROGRESS}" = "true" ]; then
    echo "  Loading progress monitor: ENABLED"
else
    echo "  Tip: Set SHOW_LOADING_PROGRESS=true for real-time status"
fi
echo "============================================================"
echo ""

# Loading Progress Monitor (two-phase: disk download + GPU loading)
start_loading_monitor() {
    local expected_mem=$1
    local port=$2
    local expected_download=$3
    local cache_dir=$4
    local check_interval=2
    local last_mem=0
    local stable_count=0
    local start_time=$(date +%s)

    while true; do
        sleep ${check_interval}

        # Check if llama-server is ready (health endpoint)
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
        local mem_used=0
        local mem_total=0
        local mem_used_gb="0.0"
        local mem_total_gb="0.0"
        local mem_used_int=0

        if command -v nvidia-smi &> /dev/null; then
            local mem_info=$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits | head -n1)
            mem_used=$(echo "$mem_info" | cut -d',' -f1 | tr -d ' ')
            mem_total=$(echo "$mem_info" | cut -d',' -f2 | tr -d ' ')
            mem_used_gb=$(awk "BEGIN {printf \"%.1f\", ${mem_used}/1024}")
            mem_total_gb=$(awk "BEGIN {printf \"%.1f\", ${mem_total}/1024}")
            mem_used_int=${mem_used_gb%.*}
        fi

        local elapsed=$(($(date +%s) - start_time))

        if [ "${mem_used_int}" -lt 2 ]; then
            # === DOWNLOAD PHASE: track disk usage in cache directory ===
            local disk_bytes=0
            if [ -d "${cache_dir}" ]; then
                disk_bytes=$(du -sb "${cache_dir}" 2>/dev/null | cut -f1)
                disk_bytes=${disk_bytes:-0}
            fi
            local disk_gb=$(awk "BEGIN {printf \"%.1f\", ${disk_bytes}/1073741824}")

            # Build progress bar based on download progress
            local progress=$(awk "BEGIN {p=(${disk_bytes}/1073741824/${expected_download})*95; if(p>95) p=95; if(p<0) p=0; printf \"%.0f\", p}")
            local filled=$(awk "BEGIN {printf \"%.0f\", (${progress}/100)*20}")
            local empty=$((20 - filled))
            local bar=$(printf "%${filled}s" | tr ' ' '#')$(printf "%${empty}s" | tr ' ' '-')

            echo "[LOADING] ${bar} ${disk_gb}GB / ${expected_download}.0GB - downloading from HuggingFace... (${elapsed}s)"
        else
            # === GPU PHASE: track VRAM usage (existing logic) ===
            local mem_delta=$(awk "BEGIN {printf \"%.0f\", ${mem_used} - ${last_mem}}")
            last_mem=${mem_used}

            local phase=""
            if [ "${mem_delta}" -gt 500 ]; then
                phase="loading weights to GPU..."
                stable_count=0
            elif [ "${mem_delta}" -gt 50 ]; then
                phase="initializing KV cache..."
                stable_count=0
            else
                stable_count=$((stable_count + 1))
                if [ "${stable_count}" -lt 3 ]; then
                    phase="preparing inference engine..."
                else
                    phase="waiting for server startup..."
                fi
            fi

            # Build progress bar based on GPU memory
            local progress=$(awk "BEGIN {p=(${mem_used_int}/${expected_mem})*95; if(p>95) p=95; printf \"%.0f\", p}")
            local filled=$(awk "BEGIN {printf \"%.0f\", (${progress}/100)*20}")
            local empty=$((20 - filled))
            local bar=$(printf "%${filled}s" | tr ' ' '#')$(printf "%${empty}s" | tr ' ' '-')

            echo "[LOADING] ${bar} ${mem_used_gb}GB / ${mem_total_gb}GB - ${phase} (${elapsed}s)"
        fi
    done
}

# Start loading monitor in background if enabled
if [ "${SHOW_LOADING_PROGRESS}" = "true" ]; then
    echo "[LOADING] Starting GPU memory monitor..."
    echo ""
    start_loading_monitor "${EXPECTED_MEM_GB}" "${PORT}" "${EXPECTED_DOWNLOAD_GB}" "${CACHE_DIR}" &
    MONITOR_PID=$!

    # Ensure monitor is killed if script exits
    trap "kill ${MONITOR_PID} 2>/dev/null" EXIT
fi

# Start llama-server
exec ${CMD}
