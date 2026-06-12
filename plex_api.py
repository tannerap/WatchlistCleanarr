"""Low-level Plex.tv and Discover API helpers."""

from __future__ import annotations

import logging
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

PLEX_TV_BASE = "https://plex.tv"
DISCOVER_BASE = "https://discover.provider.plex.tv"
COMMUNITY_GRAPHQL = "https://community.plex.tv/api"
WATCHLIST_PAGE_SIZE = 200
CLIENT_IDENTIFIER = "watchlist-cleanarr"


@dataclass(frozen=True)
class ServerUser:
    """A Plex user with access to the target media server."""

    user_id: int
    name: str
    uuid: str | None
    token: str | None
    source: str  # admin, home, shared


@dataclass(frozen=True)
class WatchlistMovie:
    rating_key: str
    title: str
    guids: tuple[str, ...]


class PlexApiClient:
    def __init__(
        self,
        token: str,
        timeout: int = 30,
        home_user_pin: str | None = None,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.home_user_pin = home_user_pin
        self._session = requests.Session()
        self._session.headers.update(self._default_headers())

    def _default_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Plex-Token": self.token,
            "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
            "X-Plex-Product": "WatchlistCleanarr",
            "X-Plex-Version": "1.0.0",
        }

    def get_account(self) -> dict[str, Any]:
        response = self._session.get(
            f"{PLEX_TV_BASE}/users/account.json",
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["user"]

    def get_machine_identifier(self, plex_url: str) -> str:
        from plexapi.server import PlexServer

        server = PlexServer(plex_url.rstrip("/"), self.token, timeout=self.timeout)
        machine_id = server.machineIdentifier
        if not machine_id:
            raise ValueError("Plex server response did not include machineIdentifier")
        return machine_id

    def discover_server_users(self, machine_id: str) -> list[ServerUser]:
        users: dict[int, ServerUser] = {}

        try:
            account = self.get_account()
            admin_id = int(account["id"])
            users[admin_id] = ServerUser(
                user_id=admin_id,
                name=account.get("title") or account.get("username") or "admin",
                uuid=account.get("uuid"),
                token=self.token,
                source="admin",
            )
        except Exception as exc:
            logger.error("Failed to load Plex admin account: %s", exc)

        for home_user in self._get_home_users(self.home_user_pin):
            user_id = home_user["id"]
            users[user_id] = ServerUser(
                user_id=user_id,
                name=home_user["name"],
                uuid=home_user.get("uuid"),
                token=home_user.get("token"),
                source="home",
            )

        api_user_names = self._get_api_user_names()
        for shared_user in self._get_shared_server_users(machine_id):
            user_id = shared_user["id"]
            name = api_user_names.get(user_id) or shared_user.get("name") or str(user_id)
            existing = users.get(user_id)
            if existing:
                if existing.token is None and shared_user.get("token"):
                    users[user_id] = ServerUser(
                        user_id=existing.user_id,
                        name=existing.name or name,
                        uuid=existing.uuid or shared_user.get("uuid"),
                        token=shared_user["token"],
                        source=existing.source,
                    )
            else:
                users[user_id] = ServerUser(
                    user_id=user_id,
                    name=name,
                    uuid=shared_user.get("uuid"),
                    token=shared_user.get("token"),
                    source="shared",
                )

        self._apply_friend_uuids(users)
        return list(users.values())

    def _get_home_users(self, home_user_pin: str | None = None) -> list[dict[str, Any]]:
        from plexapi.exceptions import PlexApiException
        from plexapi.myplex import MyPlexAccount

        results: list[dict[str, Any]] = []
        try:
            account = MyPlexAccount(token=self.token)
            for home_user in account.homeUsers():
                name = home_user.title or home_user.username or str(home_user.id)
                entry: dict[str, Any] = {
                    "id": int(home_user.id),
                    "name": name,
                    "uuid": getattr(home_user, "uuid", None),
                    "token": None,
                }
                try:
                    if home_user_pin:
                        switched = account.switchHomeUser(home_user, pin=home_user_pin)
                    else:
                        switched = account.switchHomeUser(home_user)
                    entry["token"] = switched.token
                except PlexApiException as exc:
                    if "pin" in str(exc).lower():
                        logger.warning(
                            "Plex Home user '%s' requires a PIN. "
                            "Set PLEX_HOME_USER_PIN in docker-compose.yml.",
                            name,
                        )
                    else:
                        logger.warning(
                            "Could not switch to Plex Home user '%s': %s",
                            name,
                            exc,
                        )
                results.append(entry)
        except Exception as exc:
            logger.error("Failed to list Plex Home users: %s", exc)
        return results

    def _get_api_user_names(self) -> dict[int, str]:
        names: dict[int, str] = {}
        try:
            response = self._session.get(
                f"{PLEX_TV_BASE}/api/users",
                headers={**self._default_headers(), "Accept": "application/xml"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            for user_elem in root.findall("User"):
                user_id = int(user_elem.attrib["id"])
                names[user_id] = (
                    user_elem.attrib.get("title")
                    or user_elem.attrib.get("username")
                    or str(user_id)
                )
        except Exception as exc:
            logger.error("Failed to list Plex shared users: %s", exc)
        return names

    def _get_shared_server_users(self, machine_id: str) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        try:
            response = self._session.get(
                f"{PLEX_TV_BASE}/api/servers/{machine_id}/shared_servers",
                headers={**self._default_headers(), "Accept": "application/xml"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            for shared in root.findall("SharedServer"):
                user_id = int(shared.attrib["userID"])
                users.append(
                    {
                        "id": user_id,
                        "name": shared.attrib.get("username") or shared.attrib.get("title"),
                        "token": shared.attrib.get("accessToken"),
                        "uuid": None,
                    }
                )
        except Exception as exc:
            logger.error(
                "Failed to list users with shared access to server %s: %s",
                machine_id,
                exc,
            )
        return users

    def _apply_friend_uuids(self, users: dict[int, ServerUser]) -> None:
        friend_uuid_by_name: dict[str, str] = {}
        try:
            response = self._session.post(
                COMMUNITY_GRAPHQL,
                json={
                    "query": """
                        query GetAllFriends {
                            allFriendsV2 {
                                user {
                                    id
                                    username
                                }
                            }
                        }
                    """,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            for entry in payload.get("data", {}).get("allFriendsV2", []):
                user = entry.get("user", {})
                username = user.get("username")
                user_uuid = user.get("id")
                if username and user_uuid:
                    friend_uuid_by_name[username.lower()] = user_uuid
        except Exception as exc:
            logger.warning("Could not load Plex friend UUIDs: %s", exc)

        for user_id, user in list(users.items()):
            if user.uuid:
                continue
            matched_uuid = friend_uuid_by_name.get(user.name.lower())
            if matched_uuid:
                users[user_id] = ServerUser(
                    user_id=user.user_id,
                    name=user.name,
                    uuid=matched_uuid,
                    token=user.token,
                    source=user.source,
                )

    def fetch_watchlist_movies(self, user_token: str) -> list[WatchlistMovie]:
        movies: list[WatchlistMovie] = []
        start = 0
        total = 1

        headers = {
            **self._default_headers(),
            "X-Plex-Token": user_token,
        }

        while start < total:
            response = self._session.get(
                f"{DISCOVER_BASE}/library/sections/watchlist/all",
                params={
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": WATCHLIST_PAGE_SIZE,
                },
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            container = payload.get("MediaContainer", {})
            total = int(container.get("totalSize", 0))
            start += int(container.get("size", 0))

            for item in container.get("Metadata", []):
                if item.get("type") != "movie":
                    continue
                movies.append(self._to_watchlist_movie(item, user_token, headers))

        return movies

    def _to_watchlist_movie(
        self,
        item: dict[str, Any],
        user_token: str,
        headers: dict[str, str],
    ) -> WatchlistMovie:
        rating_key = str(item.get("ratingKey", ""))
        title = item.get("title", "unknown")
        guids = self._extract_guids_from_item(item)

        if not guids and rating_key:
            guids = self._fetch_metadata_guids(rating_key, user_token, headers)

        return WatchlistMovie(rating_key=rating_key, title=title, guids=tuple(guids))

    def _fetch_metadata_guids(
        self,
        rating_key: str,
        user_token: str,
        headers: dict[str, str],
    ) -> list[str]:
        try:
            response = self._session.get(
                f"{DISCOVER_BASE}/library/metadata/{rating_key}",
                headers={**headers, "X-Plex-Token": user_token},
                timeout=self.timeout,
            )
            response.raise_for_status()
            metadata = response.json().get("MediaContainer", {}).get("Metadata", [])
            if metadata:
                return self._extract_guids_from_item(metadata[0])
        except Exception as exc:
            logger.debug("Could not load metadata for ratingKey %s: %s", rating_key, exc)
        return []

    @staticmethod
    def _extract_guids_from_item(item: dict[str, Any]) -> list[str]:
        guids: list[str] = []
        if item.get("guid"):
            guids.append(item["guid"])
        for guid in item.get("Guid", []):
            guid_value = guid.get("id") if isinstance(guid, dict) else str(guid)
            if guid_value:
                guids.append(guid_value)
        return guids

    def remove_from_watchlist(self, user_token: str, rating_key: str) -> bool:
        response = self._session.put(
            f"{DISCOVER_BASE}/actions/removeFromWatchlist",
            params={"ratingKey": rating_key},
            headers={**self._default_headers(), "X-Plex-Token": user_token},
            data={"ratingKey": rating_key},
            timeout=self.timeout,
        )
        if response.status_code == 200:
            return True
        logger.warning(
            "removeFromWatchlist failed for ratingKey=%s (status=%s): %s",
            rating_key,
            response.status_code,
            response.text[:300],
        )
        return False

    def ping_token(self) -> None:
        response = self._session.get(
            f"{PLEX_TV_BASE}/api/v2/ping",
            params={"X-Plex-Client-Identifier": str(uuid.uuid4())},
            timeout=self.timeout,
        )
        response.raise_for_status()
