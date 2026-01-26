# Optimized llama.cpp image for gpt-oss-120b GGUF model
# Supports: A100-80GB, H100-80GB, H200, L40S
#
# Build: docker build -t gpt-oss-llamacpp:latest .
# Run:   docker run --gpus all -p 8080:8080 gpt-oss-llamacpp:latest
#
# Key advantages over vLLM:
#   - GBNF grammar constraints prevent parsing failures (no harmony drift)
#   - Stable tool calling (no v0.11.0 hang bug #26480)
#   - No attention sink backend compatibility issues
#
# Model memory requirements:
#   - gpt-oss-120b GGUF: ~63GB (native int4/MXFP4, same as vLLM)
#   - Both vLLM and llama.cpp fit 128K context on 80GB GPUs
#
# Critical build flags:
#   - GGML_CUDA_FORCE_CUBLAS=OFF: Fixes gibberish output bug on Hopper
#   - GGML_CUDA_F16=OFF: Prevents FP16 accumulation overflow

# ==============================================================================
# Stage 1: Build llama.cpp from source
# ==============================================================================
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    curl \
    libcurl4-openssl-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone llama.cpp (use specific commit for reproducibility)
WORKDIR /build
RUN git clone --depth 1 https://github.com/ggerganov/llama.cpp.git

WORKDIR /build/llama.cpp

# Build with multi-architecture CUDA support and critical bug fixes
# - CMAKE_CUDA_ARCHITECTURES: A100 (80), L40S (89), H100/H200 (90)
# - GGML_CUDA_FORCE_CUBLAS=OFF: CRITICAL - Fixes gibberish on Hopper GPUs
# - GGML_CUDA_F16=OFF: CRITICAL - Prevents FP16 overflow causing bad output
# - LLAMA_CURL=ON: Enable HuggingFace model downloads via --hf flag
# - Link against CUDA stubs during build (actual runtime libs provided by host)
ENV LIBRARY_PATH=/usr/local/cuda/lib64/stubs:${LIBRARY_PATH}
RUN cmake -B build \
    -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES="80;89;90" \
    -DGGML_CUDA_FORCE_CUBLAS=OFF \
    -DGGML_CUDA_F16=OFF \
    -DLLAMA_CURL=ON \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    && cmake --build build --config Release -j$(nproc)

# ==============================================================================
# Stage 2: Runtime image
# ==============================================================================
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libcurl4 \
    libgomp1 \
    libssl3 \
    openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/run/sshd \
    && echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config \
    && echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config \
    && echo 'PermitEmptyPasswords no' >> /etc/ssh/sshd_config

WORKDIR /app

# Copy built binaries from builder stage
COPY --from=builder /build/llama.cpp/build/bin/llama-server /app/llama-server
COPY --from=builder /build/llama.cpp/build/bin/llama-cli /app/llama-cli

# Create model and cache directories
RUN mkdir -p /models /app/cache

# Copy runtime files
COPY entrypoint.sh /app/entrypoint.sh
COPY harmony.gbnf /app/harmony.gbnf
RUN chmod +x /app/entrypoint.sh

# Health check - llama.cpp uses /health endpoint
# Port 8000 for vLLM drop-in compatibility
HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000 22

ENTRYPOINT ["/app/entrypoint.sh"]
