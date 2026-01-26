FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/knaeckebrothero/Uni-Projekt-Graph-RAG"
LABEL org.opencontainers.image.description="Debug Cockpit MCP Server"

WORKDIR /app

# Install dependencies first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Default API URL (overridden in docker-compose)
ENV COCKPIT_API_URL=http://localhost:8085

# Run as non-root user
RUN useradd --create-home appuser
USER appuser

# Entry point for stdio transport
CMD ["python", "run.py"]
