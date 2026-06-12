"""Persist Plex credentials on first start so compose secrets can be removed."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PERSISTED_KEYS = ("PLEX_URL", "PLEX_TOKEN", "PLEX_HOME_USER_PIN", "WEBHOOK_API_KEY")
USER_TOKENS_ENV_KEY = "PLEX_USER_TOKENS"


def _config_dir() -> Path:
    return Path(os.environ.get("CONFIG_DIR", "/data"))


def _config_file() -> Path:
    return _config_dir() / "config.env"


def _user_tokens_file() -> Path:
    return _config_dir() / "user_tokens.env"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [
        "# WatchlistCleanarr – persisted on first container start.",
        "# You can remove PLEX_TOKEN and WEBHOOK_API_KEY from docker-compose.yml after this file exists.",
        "",
    ]
    for key in PERSISTED_KEYS:
        if key in values and values[key]:
            lines.append(f'{key}={values[key]}')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        logger.debug("Could not set restrictive permissions on %s", path)


def _write_user_tokens_file(path: Path, values: dict[str, str]) -> None:
    lines = [
        "# Optional per-user Plex.tv X-Plex-Token values for friends with library shares.",
        "# Keys: Plex username, display name, or numeric user ID (case-insensitive).",
        "# Example:",
        "# micha.65=their-plex-token",
        "# noemi.92=their-plex-token",
        "",
    ]
    for key in sorted(values):
        if values[key]:
            lines.append(f"{key}={values[key]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        logger.debug("Could not set restrictive permissions on %s", path)


def _parse_user_tokens_json(raw: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid %s JSON, ignoring: %s", USER_TOKENS_ENV_KEY, exc)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("%s must be a JSON object, ignoring", USER_TOKENS_ENV_KEY)
        return {}
    return {str(key).strip(): str(value).strip() for key, value in parsed.items() if value}


def load_user_tokens() -> dict[str, str]:
    """Load optional per-user Plex.tv tokens from file and environment."""
    tokens: dict[str, str] = {}
    tokens_file = _user_tokens_file()
    if tokens_file.exists():
        tokens.update(_parse_env_file(tokens_file))

    env_tokens = os.environ.get(USER_TOKENS_ENV_KEY)
    if env_tokens:
        tokens.update(_parse_user_tokens_json(env_tokens))

    return {key: value for key, value in tokens.items() if key and value}


def init_user_tokens() -> dict[str, str]:
    """Persist PLEX_USER_TOKENS from env and return the merged token map."""
    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    tokens_file = _user_tokens_file()

    stored = _parse_env_file(tokens_file) if tokens_file.exists() else {}
    env_tokens = os.environ.get(USER_TOKENS_ENV_KEY)
    if env_tokens:
        stored.update(_parse_user_tokens_json(env_tokens))

    if stored:
        _write_user_tokens_file(tokens_file, stored)
        if env_tokens:
            logger.info(
                "Saved per-user Plex tokens to %s. You can remove %s from docker-compose.yml now.",
                tokens_file,
                USER_TOKENS_ENV_KEY,
            )

    return stored


def init_config() -> None:
    """Load persisted config and save values supplied on first deployment."""
    config_file = _config_file()
    config_file.parent.mkdir(parents=True, exist_ok=True)

    stored = _parse_env_file(config_file) if config_file.exists() else {}

    for key, value in stored.items():
        if key in PERSISTED_KEYS and value and not os.environ.get(key):
            os.environ[key] = value

    updates: dict[str, str] = {}
    for key in PERSISTED_KEYS:
        value = os.environ.get(key)
        if value:
            updates[key] = value

    if updates:
        merged = {**stored, **updates}
        _write_env_file(config_file, merged)
        if config_file.exists() and not stored:
            logger.info(
                "Saved configuration to %s. You can remove secrets from docker-compose.yml now.",
                config_file,
            )
        elif updates != {k: stored.get(k) for k in updates}:
            logger.info("Updated persisted configuration at %s", config_file)

    if os.environ.get("PLEX_TOKEN") and config_file.exists():
        source = "environment" if updates.get("PLEX_TOKEN") else "persisted file"
        logger.debug("PLEX_TOKEN loaded from %s", source)

    init_user_tokens()
