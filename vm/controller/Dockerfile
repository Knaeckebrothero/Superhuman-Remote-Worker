FROM python:3.12-slim

ARG SRW_SOURCE_REVISION=""
ARG SRW_SOURCE_URL="https://github.com/knaeckebrothero/Superhuman-Remote-Worker"
ARG SRW_RELEASE_VERSION=""
ARG SRW_DOCUMENTATION_URL="https://github.com/knaeckebrothero/Superhuman-Remote-Worker/tree/main/docs"

LABEL org.opencontainers.image.source="${SRW_SOURCE_URL}"
LABEL org.opencontainers.image.revision="${SRW_SOURCE_REVISION}"
LABEL org.opencontainers.image.version="${SRW_RELEASE_VERSION}"
LABEL org.opencontainers.image.documentation="${SRW_DOCUMENTATION_URL}"
LABEL io.srw.component="vm-controller"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller.py headscale_client.py lifecycle_auth.py ./

# Non-root user — K8s RBAC handles authorization
RUN useradd --create-home --no-log-init controller
USER controller

ENTRYPOINT ["python", "-u", "controller.py"]
