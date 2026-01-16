#!/bin/bash
# Optimized entrypoint for gpt-oss-120b on H100/H200
# All defaults tuned for single-GPU agent workloads (sequential requests)

set -e

# =============================================================================
# Default configuration (can be overridden via environment variables)
# =============================================================================

# Model settings
MODEL="${MODEL:-openai/gpt-oss-120b}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"

# Memory settings - tuned for H200 (141GB HBM3e) / H100 (80GB HBM3)
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"

# Batching settings - conservative for agent workloads
MAX_NUM_SEQS="${MAX_NUM_SEQS:-64}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-8192}"

# Performance flags
ASYNC_SCHEDULING="${ASYNC_SCHEDULING:-true}"
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
echo "  gpt-oss-vllm optimized for H100/H200"
echo "=============================================="
echo ""
echo "Model:              ${MODEL}"
echo "Max context:        ${MAX_MODEL_LEN}"
echo "Tensor parallel:    ${TENSOR_PARALLEL_SIZE}"
echo "GPU mem util:       ${GPU_MEMORY_UTILIZATION}"
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
