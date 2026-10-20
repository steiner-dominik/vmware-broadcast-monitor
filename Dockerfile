FROM python:3.12-slim

# Links the GHCR package to the repository (visibility, README, permissions).
LABEL org.opencontainers.image.source="https://github.com/steiner-dominik/vmware-broadcast-monitor" \
      org.opencontainers.image.description="Detects broadcast storms and in-guest bridging loops on vSphere and alerts via Telegram/Slack" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 10001 --no-create-home app

COPY monitor.py .

USER app
ENTRYPOINT ["python", "/app/monitor.py"]
