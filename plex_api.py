"""Low-level Plex.tv and Discover API helpers."""

from __future__ import annotations

import logging
import time
import uuid as uuid_lib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

PLEX_TV_BASE = "https://plex.tv"
DISCOVER_BASE = "https://discover.provider.plex.tv"
COMMUNITY_GRAPHQL = "https://community.plex.tv/api"
WATCHLIST_GRAPHQL_PAGE_SIZE = 100
WATCHLIST_REST_PAGE_SIZE = 100
CLIENT_IDENTIFIER = "watchlist-cleanarr"
HOME_USER_SWITCH_RETRIES = 3
HOME_USER_SWITCH_RETRY_DELAY_SEC = 2.0

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
        self._home_user_token_cache: dict[int, str] = {}
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

        home_users = self._get_home_users()
        for home_user in home_users:
            user_id = home_user["id"]
            if admin_id is not None and user_id == admin_id:
                continue
            users[user_id] = ServerUser(
                user_id=user_id,
                name=home_user["name"],
                uuid=home_user.get("uuid"),
                token=None,
                source="home",
            )

        api_user_details = self._get_api_user_details()
        for shared_user in self._get_shared_server_users(machine_id):
            user_id = shared_user["id"]
            if admin_id is not None and user_id == admin_id:
                continue
            details = api_user_details.get(user_id, {})
            name = (
                details.get("title")
                or details.get("username")
                or shared_user.get("name")
                or str(user_id)
            )
            users[user_id] = ServerUser(
                user_id=user_id,
                name=name,
                uuid=details.get("uuid"),
                token=shared_user.get("token"),
                source="shared",
            )

        self._apply_friend_uuids(users, api_user_details)
        self._resolve_home_user_tokens(users, home_users)
        return list(users.values())

    def _get_home_users(self) -> list[dict[str, Any]]:
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
                    }
                )
        except Exception as exc:
            logger.error("Failed to list Plex Home users: %s", exc)
        return results

    def _resolve_home_user_tokens(
        self,
        users: dict[int, ServerUser],
        home_users: list[dict[str, Any]],
    ) -> None:
        """Switch into managed home users only when GraphQL UUID access is unavailable."""
        home_user_ids = {home_user["id"] for home_user in home_users}
        for user_id in home_user_ids:
            user = users.get(user_id)
            if user is None or user.uuid or user.token:
                continue
            token = self._switch_home_user_token(user_id, self.home_user_pin, user.name)
            if token:
                users[user_id] = ServerUser(
                    user_id=user.user_id,
                    name=user.name,
                    uuid=user.uuid,
                    token=token,
                    source=user.source,
                )

    def _switch_home_user_token(
        self,
        user_id: int,
        home_user_pin: str | None,
        name: str,
    ) -> str | None:
        cached = self._home_user_token_cache.get(user_id)
        if cached:
            return cached

        pin_attempts: list[dict[str, str] | None] = [None]
        if home_user_pin:
            pin_attempts.append({"pin": home_user_pin})

        for pin_params in pin_attempts:
            for attempt in range(HOME_USER_SWITCH_RETRIES):
                try:
                    response = self._session.post(
                        f"{PLEX_TV_BASE}/api/home/users/{user_id}/switch",
                        params=pin_params or {},
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
                    if token:
                        self._home_user_token_cache[user_id] = token
                    return token
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    if status == 429 and attempt < HOME_USER_SWITCH_RETRIES - 1:
                        delay = HOME_USER_SWITCH_RETRY_DELAY_SEC * (attempt + 1)
                        logger.info(
                            "Rate limited switching to Plex Home user '%s', retrying in %.0fs",
                            name,
                            delay,
                        )
                        time.sleep(delay)
                        continue
                    if status in (401, 403) and pin_params is None and home_user_pin:
                        break
                    if status in (401, 403):
                        logger.warning(
                            "Plex Home user '%s' is PIN-protected and could not be switched. "
                            "Remove the profile PIN in Plex Home settings or set PLEX_HOME_USER_PIN "
                            "to that profile's PIN.",
                            name,
                        )
                    else:
                        logger.warning("Could not switch to Plex Home user '%s': %s", name, exc)
                    return None
                except Exception as exc:
                    logger.warning("Could not switch to Plex Home user '%s': %s", name, exc)
                    return None
        return None

    def _get_api_user_details(self) -> dict[int, dict[str, str]]:
        details: dict[int, dict[str, str]] = {}
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
                details[user_id] = {
                    "title": user_elem.attrib.get("title", ""),
                    "username": user_elem.attrib.get("username", ""),
                    "uuid": user_elem.attrib.get("uuid", ""),
                    "email": user_elem.attrib.get("email", ""),
                }
        except Exception as exc:
            logger.error("Failed to list Plex shared users: %s", exc)
        return details

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
                        "token": shared.attrib.get("accessToken") or None,
                    }
                )
        except Exception as exc:
            logger.error(
                "Failed to list users with shared access to server %s: %s",
                machine_id,
                exc,
            )
        return users

    @staticmethod
    def _name_lookup_keys(*names: str | None) -> set[str]:
        keys: set[str] = set()
        for name in names:
            if not name:
                continue
            lowered = name.lower().strip()
            keys.add(lowered)
            keys.add(lowered.replace(".", "").replace("_", "").replace("-", ""))
        return keys

    def _apply_friend_uuids(
        self,
        users: dict[int, ServerUser],
        api_user_details: dict[int, dict[str, str]],
    ) -> None:
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
                for key in self._name_lookup_keys(
                    user.get("username"),
                    user.get("displayName"),
                ):
                    uuid_by_name[key] = user_uuid
        except Exception as exc:
            logger.warning("Could not load Plex friend UUIDs: %s", exc)

        for user_id, user in list(users.items()):
            details = api_user_details.get(user_id, {})
            resolved_uuid = user.uuid or details.get("uuid") or None
            if resolved_uuid:
                users[user_id] = ServerUser(
                    user_id=user.user_id,
                    name=user.name,
                    uuid=resolved_uuid,
                    token=user.token,
                    source=user.source,
                )
                continue

            names_to_try = self._name_lookup_keys(
                user.name,
                details.get("title"),
                details.get("username"),
                details.get("email", "").split("@")[0] if details.get("email") else None,
            )

            matched_uuid = None
            for name in names_to_try:
                if name in uuid_by_name:
                    matched_uuid = uuid_by_name[name]
                    break

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
        errors: list[str] = []

        if user.uuid:
            try:
                return self._fetch_watchlist_graphql(user.uuid, libtype)
            except requests.RequestException as exc:
                message = f"GraphQL: {exc}"
                errors.append(message)
                logger.warning("Watchlist fetch via GraphQL failed for '%s': %s", user.name, exc)

        if user.token:
            try:
                return self._fetch_watchlist_rest(user.token, libtype)
            except requests.RequestException as exc:
                message = f"REST: {exc}"
                errors.append(message)
                logger.warning("Watchlist fetch via REST failed for '%s': %s", user.name, exc)

        if not user.uuid and not user.token:
            raise requests.RequestException(
                f"No watchlist access for user '{user.name}' (missing uuid and token)"
            )

        raise requests.RequestException(
            f"Could not fetch watchlist for user '{user.name}': {'; '.join(errors)}"
        )

    def _fetch_watchlist_graphql(self, user_uuid: str, libtype: str) -> list[WatchlistItem]:
        items: list[WatchlistItem] = []
        after: str | None = None

        while True:
            variables: dict[str, Any] = {
                "uuid": user_uuid,
                "first": WATCHLIST_GRAPHQL_PAGE_SIZE,
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
                node_type = (node.get("type") or "").lower()
                if node_type != libtype.lower():
                    continue
                rating_key = str(node.get("id", ""))
                title = node.get("title", "unknown")
                guids = self._resolve_item_guids(rating_key, node.get("guid"))
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
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": WATCHLIST_REST_PAGE_SIZE,
                },
                headers={
                    "Accept": "application/json",
                    "X-Plex-Token": user_token,
                    "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
                    "X-Plex-Product": "WatchlistCleanarr",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            container = payload.get("MediaContainer", {})
            total = int(container.get("totalSize", 0))
            start += int(container.get("size", 0))

            for item in container.get("Metadata", []):
                item_type = (item.get("type") or "").lower()
                if item_type != libtype.lower():
                    continue
                rating_key = str(item.get("ratingKey", ""))
                title = item.get("title", "unknown")
                guids = self._extract_guids_from_item(item)
                if rating_key:
                    guids = list(dict.fromkeys(guids + self._fetch_metadata_guids(rating_key, user_token)))
                items.append(
                    WatchlistItem(
                        rating_key=rating_key,
                        title=title,
                        guids=tuple(guids),
                        item_type=libtype,
                    )
                )

        return items

    def _resolve_item_guids(self, rating_key: str, node_guid: str | None) -> list[str]:
        guids: list[str] = []
        if node_guid:
            guids.append(node_guid)
        if rating_key:
            guids.extend(self._fetch_metadata_guids(rating_key, self.token))
        return list(dict.fromkeys(guids))

    def _fetch_metadata_guids(self, rating_key: str, user_token: str) -> list[str]:
        try:
            response = self._session.get(
                f"{DISCOVER_BASE}/library/metadata/{rating_key}",
                headers={
                    "Accept": "application/json",
                    "X-Plex-Token": user_token,
                    "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
                    "X-Plex-Product": "WatchlistCleanarr",
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

    def ensure_user_token(self, user: ServerUser) -> ServerUser:
        """Obtain a user-specific Plex.tv token for watchlist writes."""
        if user.token:
            return user
        if user.source == "admin":
            return ServerUser(
                user_id=user.user_id,
                name=user.name,
                uuid=user.uuid,
                token=self.token,
                source=user.source,
            )
        if user.source == "home":
            token = self._switch_home_user_token(user.user_id, self.home_user_pin, user.name)
            if token:
                return ServerUser(
                    user_id=user.user_id,
                    name=user.name,
                    uuid=user.uuid,
                    token=token,
                    source=user.source,
                )
        return user

    @staticmethod
    def _rating_keys_for_removal(item: WatchlistItem) -> list[str]:
        """Rating keys accepted by discover.provider.plex.tv removeFromWatchlist."""
        keys: list[str] = []
        if item.rating_key:
            keys.append(item.rating_key)
        for guid in item.guids:
            if "/" not in guid:
                continue
            suffix = guid.rsplit("/", 1)[-1]
            if suffix and suffix not in keys:
                keys.append(suffix)
        return keys

    def remove_from_watchlist(self, user: ServerUser, item: WatchlistItem, libtype: str) -> bool:
        user = self.ensure_user_token(user)
        if user.source != "admin" and not user.token:
            if user.source == "home":
                logger.warning(
                    "Cannot remove '%s' from %s's watchlist: could not obtain a Plex Home "
                    "user token with the admin account. PIN-protected profiles must either have "
                    "their PIN removed in Plex or match PLEX_HOME_USER_PIN.",
                    item.title,
                    user.name,
                )
            else:
                logger.warning(
                    "Cannot remove '%s' from %s's watchlist: no Plex token for this shared user. "
                    "Ensure they still have library access on this server.",
                    item.title,
                    user.name,
                )
            return False

        token = user.token or self.token
        rating_keys = self._rating_keys_for_removal(item)
        if not rating_keys:
            logger.warning(
                "Cannot remove '%s' from %s's watchlist: no rating key available",
                item.title,
                user.name,
            )
            return False

        for rating_key in rating_keys:
            if not self._remove_from_watchlist_rest(token, rating_key):
                continue
            if self._verify_removed_from_watchlist(user, item, libtype):
                return True
            logger.debug(
                "removeFromWatchlist returned 200 but '%s' is still on %s's watchlist "
                "(ratingKey=%s)",
                item.title,
                user.name,
                rating_key,
            )

        logger.warning(
            "Failed to remove '%s' from %s's watchlist (tried %d rating key(s))",
            item.title,
            user.name,
            len(rating_keys),
        )
        return False

    def _verify_removed_from_watchlist(
        self,
        user: ServerUser,
        item: WatchlistItem,
        libtype: str,
    ) -> bool:
        try:
            remaining = self.fetch_watchlist_items(user, libtype)
        except requests.RequestException as exc:
            logger.debug(
                "Could not verify watchlist removal for '%s': %s",
                user.name,
                exc,
            )
            return False

        removed_keys = set(self._rating_keys_for_removal(item))
        removed_guids = set(item.guids)
        for remaining_item in remaining:
            if removed_keys.intersection(self._rating_keys_for_removal(remaining_item)):
                return False
            if removed_guids.intersection(remaining_item.guids):
                return False
        return True

    def _remove_from_watchlist_rest(self, user_token: str, rating_key: str) -> bool:
        response = self._session.put(
            f"{DISCOVER_BASE}/actions/removeFromWatchlist",
            params={"ratingKey": rating_key},
            headers={
                "Accept": "application/json",
                "X-Plex-Token": user_token,
                "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
                "X-Plex-Product": "WatchlistCleanarr",
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
