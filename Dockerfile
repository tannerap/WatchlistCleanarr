FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBHOOK_PORT=8788

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY app.py auth.py config_store.py plex_api.py plex_watchlist.py webhook_payload.py ./
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN mkdir -p /data && chmod 700 /data \
    && chmod +x /docker-entrypoint.sh

EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD sh -c 'curl -f "http://127.0.0.1:${WEBHOOK_PORT:-8788}/ping" || exit 1'

ENTRYPOINT ["/docker-entrypoint.sh"]
