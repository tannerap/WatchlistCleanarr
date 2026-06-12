"""Low-level Plex.tv and Discover API helpers."""

from __future__ import annotations

import logging
import uuid as uuid_lib
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

WATCHLIST_GRAPHQL = """
query GetWatchlistHub($uuid: ID!, $first: PaginationInt!, $after: String) {
  user(id: $uuid) {
    watchlist(first: $first, after: $after) {
      nodes {
        id
        title
        type
        guid
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""


@dataclass(frozen=True)
class ServerUser:
    """A Plex user with access to the target media server."""

    user_id: int
    name: str
    uuid: str | None
    token: str | None
    source: str  # admin, home, shared


@dataclass(frozen=True)
class WatchlistItem:
    rating_key: str
    title: str
    guids: tuple[str, ...]
    item_type: str  # movie or show


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

    def _graphql_headers(self) -> dict[str, str]:
        return {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "X-Plex-Token": self.token,
            "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
            "X-Plex-Product": "Plex Web",
            "X-Plex-Version": "4.145.1",
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
        admin_id: int | None = None

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
            if admin_id is not None and user_id == admin_id:
                continue
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
            if admin_id is not None and user_id == admin_id:
                continue
            name = api_user_names.get(user_id) or shared_user.get("name") or str(user_id)
            users[user_id] = ServerUser(
                user_id=user_id,
                name=name,
                uuid=None,
                token=None,
                source="shared",
            )

        self._apply_friend_uuids(users)
        return list(users.values())

    def _get_home_users(self, home_user_pin: str | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        try:
            response = self._session.get(
                f"{PLEX_TV_BASE}/api/home/users",
                headers={**self._default_headers(), "Accept": "application/xml"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)

            for user_elem in root.iter("User"):
                user_id_raw = user_elem.attrib.get("id")
                if not user_id_raw:
                    continue

                user_id = int(user_id_raw)
                name = (
                    user_elem.attrib.get("title")
                    or user_elem.attrib.get("username")
                    or str(user_id)
                )
                results.append(
                    {
                        "id": user_id,
                        "name": name,
                        "uuid": user_elem.attrib.get("uuid"),
                        "token": self._switch_home_user_token(user_id, home_user_pin, name),
                    }
                )
        except Exception as exc:
            logger.error("Failed to list Plex Home users: %s", exc)
        return results

    def _switch_home_user_token(
        self,
        user_id: int,
        home_user_pin: str | None,
        name: str,
    ) -> str | None:
        try:
            params: dict[str, str] = {}
            if home_user_pin:
                params["pin"] = home_user_pin
            response = self._session.post(
                f"{PLEX_TV_BASE}/api/home/users/{user_id}/switch",
                params=params,
                headers={**self._default_headers(), "Accept": "application/xml"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            token = root.attrib.get("authenticationToken")
            if not token:
                for elem in root.iter("User"):
                    token = elem.attrib.get("authenticationToken")
                    if token:
                        break
            return token
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (401, 403):
                logger.warning(
                    "Plex Home user '%s' requires a PIN or cannot be switched. "
                    "Set PLEX_HOME_USER_PIN if needed.",
                    name,
                )
            else:
                logger.warning("Could not switch to Plex Home user '%s': %s", name, exc)
            return None
        except Exception as exc:
            logger.warning("Could not switch to Plex Home user '%s': %s", name, exc)
            return None

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
        uuid_by_name: dict[str, str] = {}
        try:
            response = self._session.post(
                COMMUNITY_GRAPHQL,
                headers=self._graphql_headers(),
                json={
                    "query": """
                        query GetAllFriends {
                            allFriendsV2 {
                                user {
                                    id
                                    username
                                    displayName
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
                user_uuid = user.get("id")
                if not user_uuid:
                    continue
                for key in (user.get("username"), user.get("displayName")):
                    if key:
                        uuid_by_name[key.lower()] = user_uuid
        except Exception as exc:
            logger.warning("Could not load Plex friend UUIDs: %s", exc)

        for user_id, user in list(users.items()):
            if user.uuid:
                continue
            matched_uuid = uuid_by_name.get(user.name.lower())
            if matched_uuid:
                users[user_id] = ServerUser(
                    user_id=user.user_id,
                    name=user.name,
                    uuid=matched_uuid,
                    token=user.token,
                    source=user.source,
                )
            elif user.source == "shared":
                logger.warning(
                    "No Plex UUID for shared user '%s'. "
                    "Ensure they are friends with the admin and watchlist visibility "
                    "is set to 'Friends' or 'Anyone'.",
                    user.name,
                )

    def fetch_watchlist_items(self, user: ServerUser, libtype: str) -> list[WatchlistItem]:
        if user.uuid:
            try:
                return self._fetch_watchlist_graphql(user.uuid, libtype)
            except requests.RequestException as exc:
                logger.warning(
                    "GraphQL watchlist fetch failed for '%s': %s",
                    user.name,
                    exc,
                )

        if user.token:
            try:
                return self._fetch_watchlist_rest(user.token, libtype)
            except requests.RequestException as exc:
                logger.warning(
                    "REST watchlist fetch failed for '%s': %s",
                    user.name,
                    exc,
                )

        raise requests.RequestException(
            f"No watchlist access for user '{user.name}' (missing uuid/token)"
        )

    def _fetch_watchlist_graphql(self, user_uuid: str, libtype: str) -> list[WatchlistItem]:
        items: list[WatchlistItem] = []
        after: str | None = None

        while True:
            variables: dict[str, Any] = {
                "uuid": user_uuid,
                "first": WATCHLIST_PAGE_SIZE,
            }
            if after:
                variables["after"] = after

            response = self._session.post(
                COMMUNITY_GRAPHQL,
                headers=self._graphql_headers(),
                json={"query": WATCHLIST_GRAPHQL, "variables": variables},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()

            if payload.get("errors"):
                raise requests.RequestException(str(payload["errors"]))

            watchlist = payload.get("data", {}).get("user", {}).get("watchlist")
            if watchlist is None:
                raise requests.RequestException(f"No watchlist access for uuid {user_uuid}")

            for node in watchlist.get("nodes", []):
                if node.get("type") != libtype:
                    continue
                rating_key = str(node.get("id", ""))
                title = node.get("title", "unknown")
                guids = [node["guid"]] if node.get("guid") else []
                if not guids and rating_key:
                    guids = self._fetch_metadata_guids(rating_key, self.token)
                items.append(
                    WatchlistItem(
                        rating_key=rating_key,
                        title=title,
                        guids=tuple(guids),
                        item_type=libtype,
                    )
                )

            page_info = watchlist.get("pageInfo", {})
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                after = page_info["endCursor"]
            else:
                break

        return items

    def _fetch_watchlist_rest(self, user_token: str, libtype: str) -> list[WatchlistItem]:
        items: list[WatchlistItem] = []
        start = 0
        total = 1

        while start < total:
            response = self._session.get(
                f"{DISCOVER_BASE}/library/sections/watchlist/all",
                params={
                    "X-Plex-Token": user_token,
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": WATCHLIST_PAGE_SIZE,
                },
                headers={
                    "Accept": "application/json",
                    "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            container = payload.get("MediaContainer", {})
            total = int(container.get("totalSize", 0))
            start += int(container.get("size", 0))

            for item in container.get("Metadata", []):
                if item.get("type") != libtype:
                    continue
                rating_key = str(item.get("ratingKey", ""))
                title = item.get("title", "unknown")
                guids = self._extract_guids_from_item(item)
                if not guids and rating_key:
                    guids = self._fetch_metadata_guids(rating_key, user_token)
                items.append(
                    WatchlistItem(
                        rating_key=rating_key,
                        title=title,
                        guids=tuple(guids),
                        item_type=libtype,
                    )
                )

        return items

    def _fetch_metadata_guids(self, rating_key: str, user_token: str) -> list[str]:
        try:
            response = self._session.get(
                f"{DISCOVER_BASE}/library/metadata/{rating_key}",
                params={"X-Plex-Token": user_token},
                headers={
                    "Accept": "application/json",
                    "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
                },
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

    def remove_from_watchlist(self, user: ServerUser, rating_key: str) -> bool:
        tokens: list[str] = []
        if user.token:
            tokens.append(user.token)
        if self.token not in tokens:
            tokens.append(self.token)

        for token in tokens:
            if self._remove_from_watchlist_rest(token, rating_key):
                return True
        return False

    def _remove_from_watchlist_rest(self, user_token: str, rating_key: str) -> bool:
        response = self._session.put(
            f"{DISCOVER_BASE}/actions/removeFromWatchlist",
            params={"ratingKey": rating_key, "X-Plex-Token": user_token},
            headers={
                "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
                "Accept": "application/json",
            },
            data={"ratingKey": rating_key},
            timeout=self.timeout,
        )
        if response.status_code == 200:
            return True
        logger.debug(
            "removeFromWatchlist failed for ratingKey=%s (status=%s)",
            rating_key,
            response.status_code,
        )
        return False

    def ping_token(self) -> None:
        response = self._session.get(
            f"{PLEX_TV_BASE}/api/v2/ping",
            params={"X-Plex-Client-Identifier": str(uuid_lib.uuid4())},
            timeout=self.timeout,
        )
        response.raise_for_status()
