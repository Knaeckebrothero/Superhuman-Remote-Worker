# Optimized vLLM image for gpt-oss-120b on H100/H200
# Based on official vLLM with tool calling pre-configured
#
# Build: docker build -t gpt-oss-vllm:latest .
# Run:   docker run --gpus all -p 8000:8000 --ipc=host gpt-oss-vllm:latest

# Use official vLLM image with gpt-oss support
FROM vllm/vllm-openai:v0.12.0

ENV PYTHONUNBUFFERED=1

# vLLM environment optimizations for Hopper
ENV VLLM_ATTENTION_BACKEND=FLASH_ATTN
ENV VLLM_USE_TRITON_FLASH_ATTN=1

WORKDIR /app

# Install git (needed for triton kernels)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Install Triton kernels for MXFP4 MoE optimization (must use triton-lang repo @ v3.5.0)
# See: https://github.com/vllm-project/vllm/issues/26582
RUN pip install --no-cache-dir "triton_kernels @ git+https://github.com/triton-lang/triton.git@v3.5.0#subdirectory=python/triton_kernels"

# Install harmony format support (REQUIRED for tool calling)
RUN pip install --no-cache-dir openai-harmony

# Verify installation
RUN python3 -c "import vllm; print(f'vLLM version: {vllm.__version__}')" \
    && python3 -c "import openai_harmony; print('openai-harmony installed')" \
    && python3 -c "from triton_kernels import matmul_ogs; print('triton_kernels.matmul_ogs available')"

# Copy entrypoint script
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
