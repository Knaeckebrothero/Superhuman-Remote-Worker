# Local standalone/stdio convenience image only. Production and CI use
# ../../docker/Dockerfile.mcp, which also bakes and verifies tools/list.
#
# Build from the REPO ROOT — the server imports the shared
# src/shared/orch_surface package (client + formatters), so the build
# context must contain both trees:
#   docker build -t srw-mcp-stdio -f src/mcp_server/Dockerfile .
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/Knaeckebrothero/Superhuman-Remote-Worker"
LABEL org.opencontainers.image.description="Debug Cockpit MCP Server"

ENV PYTHONSAFEPATH=1

WORKDIR /app

# Install dependencies first for caching
COPY src/mcp_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application and shared helpers as installed packages.
COPY src/mcp_server/ ./src/mcp_server/
COPY src/shared/ ./src/shared/
COPY pyproject.toml ./
RUN pip install --no-cache-dir --no-deps -e .

# Default API URL (overridden by the Helm chart's COCKPIT_API_URL)
ENV COCKPIT_API_URL=http://localhost:8085

# Run as non-root user
RUN useradd --create-home appuser
USER appuser

# Entry point for stdio transport
CMD ["python", "-m", "mcp_server"]
