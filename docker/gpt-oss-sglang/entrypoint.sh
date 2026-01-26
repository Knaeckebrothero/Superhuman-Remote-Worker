#!/bin/bash
# SGLang entrypoint for gpt-oss models (120b and 20b)
# RadixAttention provides state-aware prefix caching - superior for Harmony format
#
# Key advantages over vLLM:
#   - RadixAttention: Maintains prefix tree structure (stateful, not hash-based)
#   - No "State-Cache Impedance Mismatch" bug that causes Harmony token leakage
#   - Native gpt-oss/Harmony parsing with --dyn-tool-call-parser harmony
#   - ~3.7x faster TTFT at low concurrency (benchmark data)
#
# Model memory requirements (MXFP4 native):
#   - gpt-oss-120b: ~63GB (fits on A100-80GB, H100-80GB, H200)
#   - gpt-oss-20b:  ~16GB (fits on L40S, RTX 4090, and above)
#
# SSH Access (for RunPod tunneling):
#   Set SSH_PASSWORD or SSH_PUBLIC_KEY to enable SSH server on port 22

set -e

# =============================================================================
# SSH Server (optional, for RunPod tunnel access)
# =============================================================================

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
    echo "root:${SSH_PASSWORD}" | chpasswd
    /usr/sbin/sshd
    echo "SSH server started on port 22 (password auth)"
    echo "Connect with: ssh -L 8000:localhost:8000 root@<pod-ip> -p <ssh-port>"
fi

# =============================================================================
# GPU Detection
# =============================================================================

detect_gpu_arch() {
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

# Count available GPUs
count_gpus() {
    if command -v nvidia-smi &> /dev/null; then
        nvidia-smi --query-gpu=name --format=csv,noheader | wc -l
    else
        echo "1"
    fi
}

GPU_ARCH=$(detect_gpu_arch)
GPU_COUNT=$(count_gpus)
echo "Detected GPU architecture: ${GPU_ARCH}"
echo "Detected GPU count: ${GPU_COUNT}"

# =============================================================================
# Default configuration (can be overridden via environment variables)
# =============================================================================

# Model settings
MODEL="${MODEL:-openai/gpt-oss-120b}"

# Context length - SGLang handles this more gracefully than vLLM
MAX_MODEL_LEN="${MAX_MODEL_LEN:-131072}"

# Tensor parallelism - auto-detect based on GPU count if not specified
if [ -z "${TENSOR_PARALLEL_SIZE}" ]; then
    # For 120b, use all available GPUs; for 20b, 1 GPU is enough
    if [[ "${MODEL}" == *"120b"* ]]; then
        TENSOR_PARALLEL_SIZE="${GPU_COUNT}"
    else
        TENSOR_PARALLEL_SIZE="1"
    fi
fi

# Memory settings
# SGLang uses mem-fraction-static instead of gpu-memory-utilization
MEM_FRACTION_STATIC="${MEM_FRACTION_STATIC:-0.90}"

# Reasoning and tool call parsers - CRITICAL for gpt-oss Harmony format
# These are SGLang-specific and handle the stateful parsing correctly
DYN_REASONING_PARSER="${DYN_REASONING_PARSER:-gpt_oss}"
DYN_TOOL_CALL_PARSER="${DYN_TOOL_CALL_PARSER:-harmony}"

# Batching settings
MAX_RUNNING_REQUESTS="${MAX_RUNNING_REQUESTS:-64}"

# Chunked prefill - SGLang handles this better than vLLM
# Safe to enable with RadixAttention (no hash collision issues)
CHUNKED_PREFILL="${CHUNKED_PREFILL:-true}"

# Attention backend - SGLang auto-selects, but can override
# Options: flashinfer (default), triton
ATTENTION_BACKEND="${ATTENTION_BACKEND:-}"

# API settings
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
API_KEY="${API_KEY:-}"

# =============================================================================
# Build command
# =============================================================================

CMD="python3 -m sglang.launch_server"

# Model path
CMD="${CMD} --model-path ${MODEL}"

# Parallelism
CMD="${CMD} --tp ${TENSOR_PARALLEL_SIZE}"

# Memory
CMD="${CMD} --mem-fraction-static ${MEM_FRACTION_STATIC}"

# Context length
if [ -n "${MAX_MODEL_LEN}" ]; then
    CMD="${CMD} --context-length ${MAX_MODEL_LEN}"
fi

# Reasoning and tool call parsers (CRITICAL for gpt-oss)
if [ -n "${DYN_REASONING_PARSER}" ]; then
    CMD="${CMD} --dyn-reasoning-parser ${DYN_REASONING_PARSER}"
fi

if [ -n "${DYN_TOOL_CALL_PARSER}" ]; then
    CMD="${CMD} --dyn-tool-call-parser ${DYN_TOOL_CALL_PARSER}"
fi

# Batching
CMD="${CMD} --max-running-requests ${MAX_RUNNING_REQUESTS}"

# Chunked prefill
if [ "${CHUNKED_PREFILL}" = "true" ]; then
    CMD="${CMD} --chunked-prefill-size 8192"
fi

# Attention backend (optional override)
if [ -n "${ATTENTION_BACKEND}" ]; then
    CMD="${CMD} --attention-backend ${ATTENTION_BACKEND}"
fi

# API settings
CMD="${CMD} --host ${HOST}"
CMD="${CMD} --port ${PORT}"

if [ -n "${API_KEY}" ]; then
    CMD="${CMD} --api-key ${API_KEY}"
fi

# Trust remote code (required for gpt-oss)
CMD="${CMD} --trust-remote-code"

# Append any additional arguments passed to the container
CMD="${CMD} $@"

# =============================================================================
# Print configuration and start
# =============================================================================

# Get SGLang version
SGLANG_VERSION=$(python3 -c "import sglang; print(sglang.__version__)" 2>/dev/null || echo "unknown")

echo "=============================================="
echo "  gpt-oss-sglang (SGLang ${SGLANG_VERSION})"
echo "=============================================="
echo ""
echo "GPU Architecture:      ${GPU_ARCH}"
echo "GPU Count:             ${GPU_COUNT}"
echo ""
echo "Model:                 ${MODEL}"
echo "Max context:           ${MAX_MODEL_LEN}"
echo "Tensor parallel:       ${TENSOR_PARALLEL_SIZE}"
echo "Memory fraction:       ${MEM_FRACTION_STATIC}"
echo ""
echo "Reasoning parser:      ${DYN_REASONING_PARSER}"
echo "Tool call parser:      ${DYN_TOOL_CALL_PARSER}"
echo "Chunked prefill:       ${CHUNKED_PREFILL}"
echo "Max running requests:  ${MAX_RUNNING_REQUESTS}"
echo ""
echo "Endpoint: http://${HOST}:${PORT}/v1"
echo ""
echo "=============================================="
echo ""
echo "Key SGLang advantages for gpt-oss:"
echo "  - RadixAttention: Stateful prefix tree (no hash collisions)"
echo "  - Native Harmony parsing (no state-cache mismatch)"
echo "  - Chunked prefill SAFE with prefix caching"
echo ""
echo "=============================================="
echo ""

# Calculate expected memory footprint for loading message
if [[ "${MODEL}" == *"120b"* ]]; then
    WEIGHTS_GB="~63GB"
    LOAD_TIME="2-5 minutes"
elif [[ "${MODEL}" == *"20b"* ]]; then
    WEIGHTS_GB="~16GB"
    LOAD_TIME="30-60 seconds"
else
    WEIGHTS_GB="unknown"
    LOAD_TIME="varies"
fi

echo "============================================================"
echo "                     LOADING MODEL"
echo "============================================================"
echo "  Model:         ${MODEL}"
echo "  Weights size:  ${WEIGHTS_GB}"
echo "  Est. time:     ${LOAD_TIME}"
echo "============================================================"
echo ""

# Start SGLang server
exec ${CMD}
