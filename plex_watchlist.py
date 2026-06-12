"""Plex watchlist cleanup for all users on a media server."""

from __future__ import annotations

import logging
import os
import re

import requests
from plexapi.server import PlexServer

from plex_api import PlexApiClient, ServerUser, WatchlistItem

logger = logging.getLogger(__name__)

RADARR_DELETE_EVENTS = {"MovieDelete", "MovieDeleted"}
SONARR_DELETE_EVENTS = {"SeriesDelete", "SeriesDeleted"}


class PlexWatchlistService:
    """Remove movies and shows from every Plex user watchlist on the configured server."""

    def __init__(self, plex_token: str, plex_url: str, home_user_pin: str | None = None) -> None:
        self.plex_token = plex_token
        self.plex_url = plex_url.rstrip("/")
        self.home_user_pin = home_user_pin
        self._client = PlexApiClient(plex_token, home_user_pin=home_user_pin)
        self._machine_id: str | None = None

    def verify_connection(self) -> None:
        try:
            PlexServer(self.plex_url, self.plex_token, timeout=10)
            self._machine_id = self._client.get_machine_identifier(self.plex_url)
            logger.info(
                "Plex server reachable at %s (machineId=%s)",
                self.plex_url,
                self._machine_id,
            )
        except Exception as exc:
            logger.warning("Could not reach Plex server at %s: %s", self.plex_url, exc)

        try:
            account = self._client.get_account()
            logger.info(
                "Plex account authenticated as %s",
                account.get("title") or account.get("username"),
            )
            self._client.ping_token()
        except Exception as exc:
            logger.error("Plex account authentication failed: %s", exc)

        try:
            users = self._get_server_users()
            logger.info("Found %d Plex user(s) with access to this server", len(users))
            for user in users:
                access = "uuid+graphql" if user.uuid else ("token" if user.token else "no access")
                logger.info("  - %s (%s, %s)", user.name, user.source, access)
        except Exception as exc:
            logger.error("Failed to enumerate Plex server users: %s", exc)

    def _get_machine_id(self) -> str:
        if self._machine_id is None:
            self._machine_id = self._client.get_machine_identifier(self.plex_url)
        return self._machine_id

    def _get_server_users(self) -> list[ServerUser]:
        return self._client.discover_server_users(self._get_machine_id())

    def remove_movie_from_all_watchlists(
        self,
        *,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        title: str | None = None,
    ) -> int:
        if not tmdb_id and not imdb_id and not title:
            logger.warning("Skipping watchlist cleanup: no TMDB, IMDb ID or title in webhook payload")
            return 0

        return self._remove_from_all_watchlists(
            libtype="movie",
            title=title or "movie",
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
        )

    def remove_show_from_all_watchlists(
        self,
        *,
        tvdb_id: int | None = None,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        title: str | None = None,
    ) -> int:
        if not tvdb_id and not tmdb_id and not imdb_id and not title:
            logger.warning(
                "Skipping watchlist cleanup for '%s': no TVDB, TMDB, IMDb ID or title in webhook payload",
                title or "unknown series",
            )
            return 0

        return self._remove_from_all_watchlists(
            libtype="show",
            title=title or "series",
            tvdb_id=tvdb_id,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
        )

    def _remove_from_all_watchlists(
        self,
        *,
        libtype: str,
        title: str,
        tvdb_id: int | None = None,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
    ) -> int:
        removed_count = 0
        for user in self._get_server_users():
            try:
                removed_count += self._remove_from_user_watchlist(
                    user,
                    libtype=libtype,
                    tvdb_id=tvdb_id,
                    tmdb_id=tmdb_id,
                    imdb_id=imdb_id,
                    title=title,
                )
            except Exception as exc:
                logger.error(
                    "Unexpected error while processing watchlist for '%s': %s",
                    user.name,
                    exc,
                    exc_info=True,
                )
        return removed_count

    def _remove_from_user_watchlist(
        self,
        user: ServerUser,
        *,
        libtype: str,
        tvdb_id: int | None,
        tmdb_id: int | None,
        imdb_id: str | None,
        title: str,
    ) -> int:
        if not user.uuid and not user.token:
            logger.warning(
                "Skipping user '%s' (%s): no UUID or Plex.tv token for watchlist access",
                user.name,
                user.source,
            )
            return 0

        try:
            watchlist = self._client.fetch_watchlist_items(user, libtype)
        except requests.RequestException as exc:
            logger.error("Failed to fetch watchlist for '%s': %s", user.name, exc)
            return 0

        matches = [
            item
            for item in watchlist
            if _item_matches(
                item,
                tvdb_id=tvdb_id,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                title=title,
            )
        ]
        if not matches:
            _log_no_match(user.name, title, watchlist)
            return 0

        removed = 0
        for item in matches:
            try:
                if self._client.remove_from_watchlist(user, item.rating_key):
                    removed += 1
                    logger.info(
                        "Removed '%s' from %s's watchlist",
                        item.title or title,
                        user.name,
                    )
            except requests.RequestException as exc:
                logger.error(
                    "Network error removing '%s' from %s's watchlist: %s",
                    item.title or title,
                    user.name,
                    exc,
                )
        return removed


def _normalize_title(value: str) -> str:
    without_year = re.sub(r"\s*[\(\[]\d{4}[\)\]]\s*", "", value)
    return re.sub(r"[^a-z0-9]", "", without_year.lower())


def _log_no_match(user_name: str, title: str, watchlist: list[WatchlistItem]) -> None:
    if not watchlist:
        logger.info("No watchlist match for '%s' (user: %s): watchlist is empty", title, user_name)
        return

    preview = ", ".join(
        f"{item.title} [{'; '.join(item.guids) or 'no guids'}]" for item in watchlist[:8]
    )
    logger.info(
        "No watchlist match for '%s' (user: %s, %d items: %s)",
        title,
        user_name,
        len(watchlist),
        preview,
    )


def _item_matches(
    item: WatchlistItem,
    *,
    tvdb_id: int | None = None,
    tmdb_id: int | None = None,
    imdb_id: str | None = None,
    title: str | None = None,
) -> bool:
    guids = set(item.guids)

    if tvdb_id is not None:
        tvdb_id_str = str(tvdb_id)
        tvdb_variants = {f"tvdb://{tvdb_id_str}", f"thetvdb://{tvdb_id_str}"}
        if guids.intersection(tvdb_variants):
            return True
        if any(tvdb_id_str in guid for guid in guids):
            return True

    if tmdb_id is not None:
        tmdb_id_str = str(tmdb_id)
        tmdb_variants = {f"tmdb://{tmdb_id_str}", f"themoviedb://{tmdb_id_str}"}
        if guids.intersection(tmdb_variants):
            return True
        if any(tmdb_id_str in guid for guid in guids):
            return True

    if imdb_id:
        normalized = imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"
        imdb_variants = {
            f"imdb://{normalized}",
            f"imdb://{normalized.lstrip('tt')}",
        }
        if guids.intersection(imdb_variants):
            return True
        if any(normalized in guid for guid in guids):
            return True

    if title and item.title:
        if _normalize_title(item.title) == _normalize_title(title):
            return True

    return False


def create_service_from_env() -> PlexWatchlistService:
    plex_token = os.environ.get("PLEX_TOKEN", "")
    plex_url = os.environ.get("PLEX_URL", "http://localhost:32400")
    home_user_pin = os.environ.get("PLEX_HOME_USER_PIN")

    if not plex_token:
        raise ValueError("PLEX_TOKEN environment variable is required")

    return PlexWatchlistService(
        plex_token=plex_token,
        plex_url=plex_url,
        home_user_pin=home_user_pin,
    )
