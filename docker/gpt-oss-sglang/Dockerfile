# SGLang for gpt-oss models (120b and 20b)
# RadixAttention provides state-aware prefix caching - ideal for Harmony format
#
# Build: docker build -t gpt-oss-sglang:latest .
# Run:   docker run --gpus all -p 8000:8000 --ipc=host gpt-oss-sglang:latest
#
# Why SGLang over vLLM for gpt-oss?
#   - RadixAttention maintains stateful prefix trees (vs vLLM's stateless hash cache)
#   - Native Harmony format support with --dyn-tool-call-parser harmony
#   - No "State-Cache Impedance Mismatch" that causes token leakage in vLLM
#   - Day 0 gpt-oss support (Issue #8833)
#
# Reference: https://docs.nvidia.com/dynamo/latest/backends/sglang/gpt-oss.html

# Use v0.5.0rc2 for Hopper GPUs (H100/H200) - has latest gpt-oss optimizations
# For Blackwell: use lmsysorg/sglang:v0.5.0rc2-cu128-b200
# For DGX Spark: use lmsysorg/sglang:spark
FROM lmsysorg/sglang:v0.5.0rc2-cu126

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# Install SSH server for tunnel access (RunPod)
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/run/sshd \
    && echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config \
    && echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config \
    && echo 'PermitEmptyPasswords no' >> /etc/ssh/sshd_config

# Install openai-harmony for format support (may be needed for client-side parsing)
RUN pip install --no-cache-dir openai-harmony==0.0.8

WORKDIR /app

# Copy entrypoint script
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Health check - SGLang uses /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000 22

ENTRYPOINT ["/app/entrypoint.sh"]
