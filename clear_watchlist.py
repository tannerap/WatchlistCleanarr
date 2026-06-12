#!/usr/bin/env python3
"""CLI to clear the Plex watchlist for a single server user."""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from config_store import init_config
from plex_api import PlexApiClient
from plex_watchlist import create_service_from_env

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clear the Plex watchlist for one user on the configured media server.",
    )
    parser.add_argument(
        "user",
        nargs="?",
        help="Plex username, display name, or numeric user ID (e.g. micha.65)",
    )
    parser.add_argument(
        "--list-users",
        action="store_true",
        help="List Plex users with access to this server and exit",
    )
    parser.add_argument(
        "--movies-only",
        action="store_true",
        help="Remove movies only",
    )
    parser.add_argument(
        "--shows-only",
        action="store_true",
        help="Remove TV shows only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List items that would be removed without deleting them",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation and delete immediately",
    )
    return parser


def _estimate_write_access(service, user) -> str:
    if user.source in ("admin", "home"):
        return "plexapi (read+write)"
    token_lookup = service._client._build_user_token_lookup()
    user_keys = PlexApiClient._name_lookup_keys(user.name, str(user.user_id))
    if user_keys.intersection(token_lookup):
        return "plexapi (read+write)"
    return "read-only (add token to user_tokens.env)"


def _list_users() -> int:
    service = create_service_from_env()
    users = service._client.discover_server_users(
        service._get_machine_id(),
        resolve_home_tokens_for=set(),
        apply_friend_uuids=False,
    )
    if not users:
        logger.error("No Plex users found on this server")
        return 1

    print(f"Found {len(users)} user(s):")
    for user in users:
        access = _estimate_write_access(service, user)
        print(f"  - {user.name} (id={user.user_id}, {user.source}, {access})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.list_users and args.user is None:
        parser.print_help()
        return 0

    init_config()

    if args.list_users:
        return _list_users()

    if not args.user:
        parser.error("user is required unless --list-users is used")

    if args.movies_only and args.shows_only:
        parser.error("use only one of --movies-only or --shows-only")

    movies = not args.shows_only
    shows = not args.movies_only

    try:
        service = create_service_from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    user = service.resolve_user(args.user)
    if user is None:
        logger.error("No Plex user matched '%s'. Run with --list-users.", args.user)
        return 1

    dry_run = args.dry_run or not args.yes

    try:
        movies_removed, shows_removed = service.clear_user_watchlist(
            args.user,
            movies=movies,
            shows=shows,
            dry_run=dry_run,
            user=user,
        )
    except (ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        return 1

    total = movies_removed + shows_removed
    if not args.dry_run and not args.yes:
        print(
            f"Would remove {total} item(s) from '{user.name}' "
            f"({movies_removed} movie(s), {shows_removed} show(s))."
        )
        print("Re-run with --yes to delete, or --dry-run to preview in the logs.")
        return 0

    if args.yes or args.dry_run:
        action = "Would remove" if dry_run else "Removed"
        logger.info(
            "%s %d item(s) from '%s' (%d movie(s), %d show(s))",
            action,
            total,
            user.name,
            movies_removed,
            shows_removed,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
