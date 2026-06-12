"""Persist Plex credentials on first start so compose secrets can be removed."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PERSISTED_KEYS = ("PLEX_URL", "PLEX_TOKEN", "PLEX_HOME_USER_PIN", "WEBHOOK_API_KEY")


def _config_file() -> Path:
    config_dir = os.environ.get("CONFIG_DIR", "/data")
    return Path(config_dir) / "config.env"


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
