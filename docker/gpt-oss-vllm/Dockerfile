# Optimized vLLM image for gpt-oss models (20b and 120b)
# Supports: A100-80GB, H100-80GB, H200, L40S (20b only)
#
# Build: docker build -t gpt-oss-vllm:latest .
# Run:   docker run --gpus all -p 8000:8000 --ipc=host gpt-oss-vllm:latest
#
# Note: v0.10.2 is used because v0.11.0 has critical tool calling bugs (#26480)
#       that cause ~50% of queries to hang indefinitely.
#
# Model memory requirements (MXFP4 native):
#   - gpt-oss-120b: ~63GB weights (fits on A100-80GB, H100-80GB, H200)
#   - gpt-oss-20b:  ~16GB weights (fits on L40S-48GB, RTX 4090-24GB)
#
# GPU-specific notes:
#   - A100: Requires TRITON_ATTN backend (FA3 sinks not supported, issue #22290)
#   - H100/H200: Use FLASH_ATTN with FA3 for optimal performance
#   - L40S/A100: No native MXFP4 - uses software emulation via Triton kernels

# Use vLLM v0.10.2 (stable for tool calling)
FROM vllm/vllm-openai:v0.10.2

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# Install SSH server for tunnel access (bypasses Cloudflare timeouts on RunPod)
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/run/sshd \
    && echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config \
    && echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config \
    && echo 'PermitEmptyPasswords no' >> /etc/ssh/sshd_config

# Multi-architecture CUDA support: L40S (sm_89), A100 (sm_80), H100/H200 (sm_90)
ENV TORCH_CUDA_ARCH_LIST="8.0;8.9;9.0+PTX"

# vLLM attention backend - set dynamically at runtime by entrypoint.sh
# based on GPU detection (Ampere/Ada need TRITON_ATTN, Hopper uses FLASH_ATTN)
# Do NOT set VLLM_ATTENTION_BACKEND here - let entrypoint handle it
ENV VLLM_USE_TRITON_FLASH_ATTN=1

WORKDIR /app

# Install harmony format support (REQUIRED for gpt-oss tool calling)
# Pin version for reproducibility
RUN pip install --no-cache-dir openai-harmony==0.0.8

# Triton 3.4.0 required for gpt-oss on Ampere GPUs (A100)
# - Enables TRITON_ATTN_VLLM_V1 backend which supports attention sinks
# - v3.5.0+ deprecated `routing` module which breaks vLLM's MoE kernels
RUN pip install --no-cache-dir triton==3.4.0

# Verify installation
RUN python3 -c "import vllm; print(f'vLLM version: {vllm.__version__}')" \
    && python3 -c "import openai_harmony; print('openai-harmony installed')" \
    && python3 -c "import triton; print(f'Triton version: {triton.__version__}')"

# Copy entrypoint script
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000 22

ENTRYPOINT ["/app/entrypoint.sh"]
