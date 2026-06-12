FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY app.py auth.py background_tasks.py clear_watchlist.py config_store.py plex_api.py plex_watchlist.py webhook_payload.py ./

RUN mkdir -p /data && chmod 700 /data

EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://127.0.0.1:8788/ping || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:8788", "--workers", "1", "--threads", "2", "--timeout", "120", "app:app"]
