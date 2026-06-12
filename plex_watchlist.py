"""Plex watchlist cleanup for all users on a media server."""

from __future__ import annotations

import logging
import os
import re
import time

import requests
from plexapi.server import PlexServer

from config_store import load_user_tokens
from plex_api import PlexApiClient, ServerUser, WatchlistItem

logger = logging.getLogger(__name__)

RADARR_DELETE_EVENTS = {"MovieDelete", "MovieDeleted", "movieDelete", "movieDeleted"}
RADARR_FILE_DELETE_EVENTS = {
    "MovieFileDelete",
    "MovieFileDeleted",
    "movieFileDelete",
    "movieFileDeleted",
}
RADARR_WATCHLIST_CLEANUP_EVENTS = RADARR_DELETE_EVENTS | RADARR_FILE_DELETE_EVENTS
RADARR_FILE_DELETE_SKIP_REASONS = {"upgrade"}
SONARR_DELETE_EVENTS = {"SeriesDelete", "SeriesDeleted", "seriesDelete", "seriesDeleted"}
SONARR_FILE_DELETE_EVENTS = {
    "EpisodeFileDelete",
    "EpisodeFileDeleted",
    "episodeFileDelete",
    "episodeFileDeleted",
}
SONARR_WATCHLIST_CLEANUP_EVENTS = SONARR_DELETE_EVENTS | SONARR_FILE_DELETE_EVENTS
SONARR_FILE_DELETE_SKIP_REASONS = {"upgrade"}
SERVER_USERS_CACHE_TTL_SEC = 24 * 60 * 60


class PlexWatchlistService:
    """Remove movies and shows from every Plex user watchlist on the configured server."""

    def __init__(
        self,
        plex_token: str,
        plex_url: str,
        home_user_pin: str | None = None,
        user_tokens: dict[str, str] | None = None,
    ) -> None:
        self.plex_token = plex_token
        self.plex_url = plex_url.rstrip("/")
        self.home_user_pin = home_user_pin
        self.user_tokens = user_tokens or {}
        self._client = PlexApiClient(
            plex_token,
            home_user_pin=home_user_pin,
            user_tokens=self.user_tokens,
        )
        self._machine_id: str | None = None
        self._server_users_cache: tuple[float, list[ServerUser]] | None = None

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

        if self.user_tokens:
            logger.info("Loaded %d per-user Plex token(s) from configuration", len(self.user_tokens))

        try:
            users = self._refresh_server_users()
            logger.info("Found %d Plex user(s) with access to this server", len(users))
            for user in users:
                if self._client.get_myplex_account(user) is not None:
                    access = "plexapi (read+write)"
                elif user.uuid:
                    access = "graphql (read-only)"
                else:
                    access = "no access"
                logger.info("  - %s (%s, %s)", user.name, user.source, access)
        except Exception as exc:
            logger.error("Failed to enumerate Plex server users: %s", exc)

    def _get_machine_id(self) -> str:
        if self._machine_id is None:
            self._machine_id = self._client.get_machine_identifier(self.plex_url)
        return self._machine_id

    def _refresh_server_users(self) -> list[ServerUser]:
        logger.info("Discovering Plex server users")
        users = self._client.discover_server_users(self._get_machine_id())
        self._server_users_cache = (time.monotonic(), users)
        return users

    def _get_server_users(self) -> list[ServerUser]:
        now = time.monotonic()
        if self._server_users_cache is not None:
            cached_at, cached_users = self._server_users_cache
            age_sec = now - cached_at
            if age_sec < SERVER_USERS_CACHE_TTL_SEC:
                logger.debug(
                    "Using cached Plex server users (%d user(s), age %.0fs)",
                    len(cached_users),
                    age_sec,
                )
                return cached_users

        return self._refresh_server_users()

    def list_server_users(self) -> list[ServerUser]:
        """Return all Plex users with access to the configured server."""
        return self._refresh_server_users()

    def resolve_user(self, identifier: str) -> ServerUser | None:
        """Match one Plex user and prepare write access without touching other accounts."""
        users = self._client.discover_server_users(
            self._get_machine_id(),
            resolve_home_tokens_for=set(),
            apply_friend_uuids=False,
        )
        matched = _match_user_identifier(users, identifier)
        if matched is None:
            return None
        return self._client.prepare_user_for_watchlist_write(matched)

    def clear_user_watchlist(
        self,
        user_identifier: str,
        *,
        movies: bool = True,
        shows: bool = True,
        dry_run: bool = False,
        user: ServerUser | None = None,
    ) -> tuple[int, int]:
        """Remove all watchlist items for one user. Returns (movies_removed, shows_removed)."""
        resolved = user or self.resolve_user(user_identifier)
        if resolved is None:
            raise ValueError(f"No Plex user matched '{user_identifier}'")
        user = resolved

        movies_removed = 0
        shows_removed = 0
        if movies:
            movies_removed = self._clear_user_watchlist_libtype(user, "movie", dry_run=dry_run)
        if shows:
            shows_removed = self._clear_user_watchlist_libtype(user, "show", dry_run=dry_run)
        return movies_removed, shows_removed

    def _clear_user_watchlist_libtype(
        self,
        user: ServerUser,
        libtype: str,
        *,
        dry_run: bool,
    ) -> int:
        account = self._client.get_myplex_account(user)
        if account is None:
            raise ValueError(
                f"Cannot modify {libtype} watchlist for '{user.name}' ({user.source}): "
                f"no Plex account write access. For shared users, add their Plex.tv token to "
                f"{os.environ.get('CONFIG_DIR', '/data')}/user_tokens.env"
            )

        try:
            plex_items = self._client.fetch_plexapi_watchlist(account, libtype)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to fetch {libtype} watchlist for '{user.name}': {exc}"
            ) from exc

        if not plex_items:
            logger.info("No %s watchlist items for '%s'", libtype, user.name)
            return 0

        removed = 0
        for item in plex_items:
            title = getattr(item, "title", "unknown") or "unknown"
            if dry_run:
                logger.info(
                    "Would remove %s '%s' from %s's watchlist",
                    libtype,
                    title,
                    user.name,
                )
                removed += 1
                continue

            if self._client.remove_plexapi_watchlist_item(account, item):
                removed += 1
                logger.info(
                    "Removed %s '%s' from %s's watchlist",
                    libtype,
                    title,
                    user.name,
                )
            else:
                logger.warning(
                    "Could not remove %s '%s' from %s's watchlist",
                    libtype,
                    title,
                    user.name,
                )
        return removed

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
        account = self._client.get_myplex_account(user)
        if account is not None:
            return self._remove_via_plexapi(
                account,
                user.name,
                libtype=libtype,
                tvdb_id=tvdb_id,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                title=title,
            )

        if not user.uuid:
            logger.warning(
                "Skipping user '%s' (%s): no Plex account access for watchlist changes",
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

        logger.warning(
            "Found '%s' on %s's watchlist but cannot remove it: add their Plex.tv token to "
            "%s/user_tokens.env or set PLEX_USER_TOKENS",
            title,
            user.name,
            os.environ.get("CONFIG_DIR", "/data"),
        )
        return 0

    def _remove_via_plexapi(
        self,
        account,
        user_name: str,
        *,
        libtype: str,
        tvdb_id: int | None,
        tmdb_id: int | None,
        imdb_id: str | None,
        title: str,
    ) -> int:
        try:
            plex_items = self._client.fetch_plexapi_watchlist(account, libtype)
        except Exception as exc:
            logger.error("Failed to fetch watchlist for '%s' via plexapi: %s", user_name, exc)
            return 0

        watchlist = [
            self._client.watchlist_item_from_plexapi(item, libtype) for item in plex_items
        ]
        matches = [
            (plex_item, watchlist_item)
            for plex_item, watchlist_item in zip(plex_items, watchlist)
            if _item_matches(
                watchlist_item,
                tvdb_id=tvdb_id,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
                title=title,
            )
        ]
        if not matches:
            _log_no_match(user_name, title, watchlist)
            return 0

        removed = 0
        for plex_item, watchlist_item in matches:
            if self._client.remove_plexapi_watchlist_item(account, plex_item):
                removed += 1
                logger.info(
                    "Removed '%s' from %s's watchlist",
                    watchlist_item.title or title,
                    user_name,
                )
            else:
                logger.warning(
                    "Could not remove '%s' from %s's watchlist",
                    watchlist_item.title or title,
                    user_name,
                )
        return removed


def _match_user_identifier(users: list[ServerUser], identifier: str) -> ServerUser | None:
    lookup = PlexApiClient._name_lookup_keys(identifier)
    if identifier.isdigit():
        lookup.add(identifier)

    for user in users:
        user_keys = PlexApiClient._name_lookup_keys(user.name, str(user.user_id))
        if lookup.intersection(user_keys):
            return user
    return None


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


def _guid_matches_id(guids: set[str], variants: set[str]) -> bool:
    """Match only exact external-ID GUIDs, never substrings inside Plex GUIDs."""
    if guids.intersection(variants):
        return True
    for guid in guids:
        for variant in variants:
            if guid == variant or guid.endswith(f"/{variant.split('://', 1)[-1]}"):
                return True
    return False


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
        if _guid_matches_id(
            guids,
            {f"tvdb://{tvdb_id_str}", f"thetvdb://{tvdb_id_str}"},
        ):
            return True

    if tmdb_id is not None:
        tmdb_id_str = str(tmdb_id)
        if _guid_matches_id(
            guids,
            {f"tmdb://{tmdb_id_str}", f"themoviedb://{tmdb_id_str}"},
        ):
            return True

    if imdb_id:
        normalized = imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"
        if _guid_matches_id(
            guids,
            {f"imdb://{normalized}", f"imdb://{normalized.lstrip('tt')}"},
        ):
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
        user_tokens=load_user_tokens(),
    )
