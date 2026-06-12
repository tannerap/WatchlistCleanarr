"""Flask webhook service: Radarr movie deletion -> Plex watchlist cleanup."""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv
from flask import Flask, request

from plex_watchlist import DELETE_EVENT_TYPES, PlexWatchlistService, create_service_from_env

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
plex_service: PlexWatchlistService | None = None


def get_plex_service() -> PlexWatchlistService:
    global plex_service
    if plex_service is None:
        plex_service = create_service_from_env()
    return plex_service


@app.route("/health", methods=["GET"])
def health() -> tuple[dict, int]:
    return {"status": "ok"}, 200


@app.route("/webhook/radarr", methods=["POST"])
def radarr_webhook() -> tuple[dict, int]:
    payload = request.get_json(silent=True)
    if not payload:
        logger.warning("Received webhook without JSON payload")
        return {"status": "ignored", "reason": "invalid payload"}, 400

    event_type = payload.get("eventType", "")
    logger.info("Received Radarr webhook: eventType=%s", event_type)

    if event_type not in DELETE_EVENT_TYPES:
        return {"status": "ignored", "eventType": event_type}, 200

    movie = payload.get("movie") or {}
    tmdb_id = movie.get("tmdbId")
    imdb_id = movie.get("imdbId")
    title = movie.get("title")

    if tmdb_id is not None:
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            logger.warning("Invalid tmdbId in payload: %s", tmdb_id)
            tmdb_id = None

    logger.info(
        "Processing movie deletion: title=%s tmdbId=%s imdbId=%s",
        title,
        tmdb_id,
        imdb_id,
    )

    try:
        removed_count = get_plex_service().remove_movie_from_all_watchlists(
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            title=title,
        )
    except Exception as exc:
        logger.error("Watchlist cleanup failed: %s", exc, exc_info=True)
        return {"status": "error", "message": str(exc)}, 500

    return {
        "status": "ok",
        "eventType": event_type,
        "title": title,
        "removedFromWatchlists": removed_count,
    }, 200


def _startup_verification() -> None:
    if not os.environ.get("PLEX_TOKEN"):
        logger.error("PLEX_TOKEN is not set. Configure it in docker-compose.yml.")
        return
    try:
        get_plex_service().verify_connection()
    except Exception as exc:
        logger.error("Startup verification failed: %s", exc)


_startup_verification()


if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
