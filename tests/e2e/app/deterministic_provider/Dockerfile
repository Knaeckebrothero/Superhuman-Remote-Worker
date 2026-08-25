FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt \
    && groupadd --gid 65532 fixture \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin fixture

COPY __init__.py provider.py run.py ./

USER 65532:65532
EXPOSE 8000 8001

ENTRYPOINT ["python", "run.py"]
