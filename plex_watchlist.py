"""Plex watchlist operations using the administrator token."""

from __future__ import annotations

import logging
import os
from typing import Iterator

import requests
from plexapi.exceptions import BadRequest, PlexApiException
from plexapi.myplex import MyPlexAccount, MyPlexUser
from plexapi.server import PlexServer

logger = logging.getLogger(__name__)

WATCHLIST_PAGE_SIZE = 200
DELETE_EVENT_TYPES = {"MovieDelete", "MovieDeleted"}


class PlexWatchlistService:
    """Remove movies from all Plex Home user watchlists via the admin token."""

    def __init__(self, plex_token: str, plex_url: str, home_user_pin: str | None = None) -> None:
        self.plex_token = plex_token
        self.plex_url = plex_url.rstrip("/")
        self.home_user_pin = home_user_pin
        self._admin_account: MyPlexAccount | None = None

    def verify_connection(self) -> None:
        """Validate Plex server and account access on startup."""
        try:
            PlexServer(self.plex_url, self.plex_token, timeout=10)
            logger.info("Plex server reachable at %s", self.plex_url)
        except Exception as exc:
            logger.warning("Could not reach Plex server at %s: %s", self.plex_url, exc)

        try:
            account = self._get_admin_account()
            logger.info(
                "Plex account authenticated as %s",
                account.title or account.username,
            )
        except Exception as exc:
            logger.error("Plex account authentication failed: %s", exc)

    def _get_admin_account(self) -> MyPlexAccount:
        if self._admin_account is None:
            self._admin_account = MyPlexAccount(token=self.plex_token)
        return self._admin_account

    def iter_user_accounts(self) -> Iterator[tuple[str, MyPlexAccount]]:
        """Yield (username, account) for the admin and all Plex Home users."""
        admin = self._get_admin_account()
        admin_name = admin.title or admin.username or "admin"
        yield admin_name, admin

        try:
            home_users = admin.homeUsers()
        except PlexApiException as exc:
            logger.error("Failed to list Plex Home users: %s", exc)
            return

        for home_user in home_users:
            name = home_user.title or home_user.username or str(home_user.id)
            try:
                user_account = self._switch_to_home_user(admin, home_user, name)
                if user_account is not None:
                    yield name, user_account
            except Exception as exc:
                logger.error("Could not switch to Plex Home user '%s': %s", name, exc)

    def _switch_to_home_user(
        self,
        admin: MyPlexAccount,
        home_user: MyPlexUser,
        name: str,
    ) -> MyPlexAccount | None:
        try:
            if self.home_user_pin:
                return admin.switchHomeUser(home_user, pin=self.home_user_pin)
            return admin.switchHomeUser(home_user)
        except PlexApiException as exc:
            if "pin" in str(exc).lower():
                logger.warning(
                    "Plex Home user '%s' requires a PIN. "
                    "Set PLEX_HOME_USER_PIN in docker-compose.yml if needed.",
                    name,
                )
            else:
                logger.error("Failed to switch to Plex Home user '%s': %s", name, exc)
            return None

    def remove_movie_from_all_watchlists(
        self,
        *,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        title: str | None = None,
    ) -> int:
        """Remove a movie from every user watchlist. Returns number of removals."""
        if not tmdb_id and not imdb_id:
            logger.warning(
                "Skipping watchlist cleanup for '%s': no TMDB or IMDb ID in webhook payload",
                title or "unknown movie",
            )
            return 0

        removed_count = 0
        for user_name, account in self.iter_user_accounts():
            try:
                removed_count += self._remove_from_user_watchlist(
                    account,
                    user_name,
                    tmdb_id=tmdb_id,
                    imdb_id=imdb_id,
                    title=title,
                )
            except Exception as exc:
                logger.error(
                    "Unexpected error while processing watchlist for '%s': %s",
                    user_name,
                    exc,
                    exc_info=True,
                )
        return removed_count

    def _remove_from_user_watchlist(
        self,
        account: MyPlexAccount,
        user_name: str,
        *,
        tmdb_id: int | None,
        imdb_id: str | None,
        title: str | None,
    ) -> int:
        watchlist_items = self._fetch_watchlist(account, user_name)
        matches = [
            item
            for item in watchlist_items
            if item.type == "movie" and _movie_matches(item, tmdb_id=tmdb_id, imdb_id=imdb_id)
        ]

        if not matches:
            logger.info(
                "No watchlist match for '%s' (user: %s)",
                title or "movie",
                user_name,
            )
            return 0

        removed = 0
        for item in matches:
            try:
                account.removeFromWatchlist(item)
                removed += 1
                logger.info(
                    "Removed '%s' from %s's watchlist",
                    getattr(item, "title", title or "movie"),
                    user_name,
                )
            except BadRequest as exc:
                logger.warning(
                    "Could not remove '%s' from %s's watchlist: %s",
                    getattr(item, "title", title or "movie"),
                    user_name,
                    exc,
                )
            except PlexApiException as exc:
                logger.error(
                    "Plex API error removing '%s' from %s's watchlist: %s",
                    getattr(item, "title", title or "movie"),
                    user_name,
                    exc,
                )
            except requests.RequestException as exc:
                logger.error(
                    "Network error removing '%s' from %s's watchlist: %s",
                    getattr(item, "title", title or "movie"),
                    user_name,
                    exc,
                )
        return removed

    def _fetch_watchlist(self, account: MyPlexAccount, user_name: str) -> list:
        try:
            return account.watchlist(libtype="movie")
        except PlexApiException as exc:
            logger.error("Failed to fetch watchlist for '%s': %s", user_name, exc)
            return []
        except requests.RequestException as exc:
            logger.error(
                "Network error fetching watchlist for '%s': %s",
                user_name,
                exc,
            )
            return []


def _movie_matches(item, *, tmdb_id: int | None, imdb_id: str | None) -> bool:
    guids = _extract_guids(item)

    if tmdb_id is not None:
        tmdb_id_str = str(tmdb_id)
        tmdb_variants = {
            f"tmdb://{tmdb_id_str}",
            f"themoviedb://{tmdb_id_str}",
        }
        if guids.intersection(tmdb_variants):
            return True
        if any(guid.endswith(f"/{tmdb_id_str}") for guid in guids):
            return True

    if imdb_id:
        normalized = imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"
        imdb_variants = {
            f"imdb://{normalized}",
            f"imdb://{normalized.lstrip('tt')}",
        }
        if guids.intersection(imdb_variants):
            return True

    return False


def _extract_guids(item) -> set[str]:
    guids: set[str] = set()
    if getattr(item, "guid", None):
        guids.add(item.guid)

    for guid_obj in getattr(item, "guids", []) or []:
        guid_value = getattr(guid_obj, "id", None) or str(guid_obj)
        guids.add(guid_value)

    return guids


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
