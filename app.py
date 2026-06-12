"""Flask webhook service: Radarr/Sonarr deletion -> Plex watchlist cleanup."""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv
from flask import Flask, request

from auth import get_expected_api_key, is_authorized
from config_store import init_config
from plex_watchlist import (
    RADARR_DELETE_EVENTS,
    SONARR_DELETE_EVENTS,
    PlexWatchlistService,
    create_service_from_env,
)

load_dotenv()
init_config()

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


def _require_api_key() -> tuple[dict, int] | None:
    if is_authorized(request):
        return None
    logger.warning("Rejected webhook request: invalid or missing API key")
    return {"status": "unauthorized", "message": "Invalid or missing API key"}, 401


def _parse_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@app.route("/health", methods=["GET"])
def health() -> tuple[dict, int]:
    return {"status": "ok"}, 200


@app.route("/webhook/radarr", methods=["POST"])
def radarr_webhook() -> tuple[dict, int]:
    auth_error = _require_api_key()
    if auth_error:
        return auth_error

    payload = request.get_json(silent=True)
    if not payload:
        logger.warning("Received Radarr webhook without JSON payload")
        return {"status": "ignored", "reason": "invalid payload"}, 400

    event_type = payload.get("eventType", "")
    logger.info("Received Radarr webhook: eventType=%s", event_type)

    if event_type not in RADARR_DELETE_EVENTS:
        return {"status": "ignored", "eventType": event_type}, 200

    movie = payload.get("movie") or {}
    tmdb_id = _parse_int(movie.get("tmdbId"))
    imdb_id = movie.get("imdbId")
    title = movie.get("title")

    if movie.get("tmdbId") is not None and tmdb_id is None:
        logger.warning("Invalid tmdbId in Radarr payload: %s", movie.get("tmdbId"))

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


@app.route("/webhook/sonarr", methods=["POST"])
def sonarr_webhook() -> tuple[dict, int]:
    auth_error = _require_api_key()
    if auth_error:
        return auth_error

    payload = request.get_json(silent=True)
    if not payload:
        logger.warning("Received Sonarr webhook without JSON payload")
        return {"status": "ignored", "reason": "invalid payload"}, 400

    event_type = payload.get("eventType", "")
    logger.info("Received Sonarr webhook: eventType=%s", event_type)

    if event_type not in SONARR_DELETE_EVENTS:
        return {"status": "ignored", "eventType": event_type}, 200

    series = payload.get("series") or {}
    tvdb_id = _parse_int(series.get("tvdbId"))
    tmdb_id = _parse_int(series.get("tmdbId"))
    imdb_id = series.get("imdbId")
    title = series.get("title")

    if series.get("tvdbId") is not None and tvdb_id is None:
        logger.warning("Invalid tvdbId in Sonarr payload: %s", series.get("tvdbId"))
    if series.get("tmdbId") is not None and tmdb_id is None:
        logger.warning("Invalid tmdbId in Sonarr payload: %s", series.get("tmdbId"))

    logger.info(
        "Processing series deletion: title=%s tvdbId=%s tmdbId=%s imdbId=%s",
        title,
        tvdb_id,
        tmdb_id,
        imdb_id,
    )

    try:
        removed_count = get_plex_service().remove_show_from_all_watchlists(
            tvdb_id=tvdb_id,
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
        logger.error(
            "PLEX_TOKEN is not set. Provide it once in docker-compose.yml or set it in %s.",
            os.environ.get("CONFIG_DIR", "/data") + "/config.env",
        )
        return

    if not get_expected_api_key():
        logger.warning(
            "WEBHOOK_API_KEY is not set. Webhook endpoints are not protected."
        )
    else:
        logger.info("Webhook API key protection is enabled")

    try:
        get_plex_service().verify_connection()
    except Exception as exc:
        logger.error("Startup verification failed: %s", exc)


_startup_verification()


if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
