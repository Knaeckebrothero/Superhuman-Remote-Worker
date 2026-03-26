FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller.py headscale_client.py ./

# Non-root user — K8s RBAC handles authorization
RUN useradd --create-home --no-log-init controller
USER controller

ENTRYPOINT ["python", "-u", "controller.py"]
