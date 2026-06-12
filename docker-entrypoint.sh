#!/bin/sh
set -e

PORT="${WEBHOOK_PORT:-8788}"
export WEBHOOK_PORT="${PORT}"

echo "WatchlistCleanarr starting gunicorn on 0.0.0.0:${PORT}"

exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers 1 \
  --threads 2 \
  --timeout 120 \
  app:app
